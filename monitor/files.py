from __future__ import annotations

import mimetypes
import posixpath
import stat
import tempfile
import zipfile
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Callable, Iterable

import paramiko

from .ssh_client import SSHClient


class FileManagerError(ValueError):
    pass


class SFTPFileService:
    CHUNK_SIZE = 64 * 1024

    def __init__(self, secret_box: Any, settings: Any, max_bytes: int = 512 * 1024 * 1024):
        self.secret_box = secret_box
        self.settings = settings
        self.max_bytes = max(1, int(max_bytes))

    @staticmethod
    def normalize_path(value: str | None, *, allow_root: bool = True) -> str:
        if not isinstance(value, str) or "\x00" in value:
            raise FileManagerError("文件路径无效")
        path = value.strip() or "/"
        if not path.startswith("/"):
            path = "/" + path
        normalized = posixpath.normpath(path)
        if not allow_root and normalized == "/":
            raise FileManagerError("根目录不能执行该操作")
        return normalized

    def _open(self, host: dict[str, Any]) -> tuple[SSHClient, paramiko.SFTPClient]:
        client = SSHClient(host, self.secret_box, self.settings)
        try:
            return client, client.open_sftp()
        except Exception:
            client.close()
            raise

    @staticmethod
    def _entry(path: str, attrs: Any) -> dict[str, Any]:
        mode = attrs.st_mode
        entry_type = "directory" if stat.S_ISDIR(mode) else "symlink" if stat.S_ISLNK(mode) else "file"
        return {
            "name": PurePosixPath(path).name,
            "path": path,
            "type": entry_type,
            "size": int(attrs.st_size or 0),
            "modified_at": int(attrs.st_mtime or 0),
            "mode": stat.filemode(mode),
        }

    @staticmethod
    def _child_path(parent: str, filename: str) -> str:
        name = str(filename)
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise FileManagerError("远端目录返回了无效文件名")
        return posixpath.join(parent, name)

    def list_directory(self, host: dict[str, Any], path: str) -> dict[str, Any]:
        normalized = self.normalize_path(path)
        client, sftp = self._open(host)
        try:
            attrs = self._stat(sftp, normalized)
            if not stat.S_ISDIR(attrs.st_mode):
                raise FileManagerError("目标不是目录")
            items = [self._entry(self._child_path(normalized, item.filename), item) for item in sftp.listdir_attr(normalized)]
            items.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
            parent = posixpath.dirname(normalized.rstrip("/")) or "/"
            return {"path": normalized, "parent": None if normalized == "/" else parent, "items": items}
        except OSError as exc:
            raise FileManagerError(f"无法读取目录: {exc}") from exc
        finally:
            sftp.close()
            client.close()

    def _stat(self, sftp: paramiko.SFTPClient, path: str) -> Any:
        try:
            # lstat prevents a link to a directory from turning a delete or
            # recursive copy into an operation on the link target.
            return sftp.lstat(path)
        except OSError as exc:
            raise FileManagerError(f"文件不存在或不可访问: {path}") from exc

    def _walk(self, sftp: paramiko.SFTPClient, path: str, relative: str = "") -> Iterable[tuple[str, str, Any]]:
        attrs = self._stat(sftp, path)
        if not stat.S_ISDIR(attrs.st_mode):
            yield path, relative or PurePosixPath(path).name, attrs
            return
        for item in sftp.listdir_attr(path):
            child = self._child_path(path, item.filename)
            child_relative = self._child_path(relative or "/", item.filename).lstrip("/")
            yield child, child_relative, item
            if stat.S_ISDIR(item.st_mode):
                yield from self._walk(sftp, child, child_relative)

    def download(self, host: dict[str, Any], path: str) -> tuple[Iterable[bytes], str, str, Callable[[], None]]:
        normalized = self.normalize_path(path)
        client, sftp = self._open(host)
        archive: BinaryIO | None = None
        try:
            attrs = self._stat(sftp, normalized)
            if stat.S_ISLNK(attrs.st_mode):
                raise FileManagerError("符号链接不能直接下载")
            if stat.S_ISDIR(attrs.st_mode):
                archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
                total = 0
                base_name = PurePosixPath(normalized).name or "root"
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                    for child, relative, child_attrs in self._walk(sftp, normalized, base_name):
                        if stat.S_ISDIR(child_attrs.st_mode):
                            bundle.writestr(relative.rstrip("/") + "/", b"")
                            continue
                        if stat.S_ISLNK(child_attrs.st_mode):
                            raise FileManagerError("目录包含符号链接，拒绝打包下载")
                        total += int(child_attrs.st_size or 0)
                        if total > self.max_bytes:
                            raise FileManagerError("目录总大小超过下载限制")
                        with sftp.open(child, "rb") as remote, bundle.open(relative, "w") as target:
                            while True:
                                chunk = remote.read(self.CHUNK_SIZE)
                                if not chunk:
                                    break
                                target.write(chunk)
                archive.seek(0)
                return self._iter_file(archive, f"{base_name}.zip", "application/zip", client, sftp)
            if int(attrs.st_size or 0) > self.max_bytes:
                raise FileManagerError("文件大小超过下载限制")
            remote = sftp.open(normalized, "rb")
            content_type = mimetypes.guess_type(normalized)[0] or "application/octet-stream"
            return self._iter_file(remote, PurePosixPath(normalized).name or "download", content_type, client, sftp)
        except Exception:
            if archive is not None:
                archive.close()
            sftp.close()
            client.close()
            raise

    def _iter_file(self, stream: BinaryIO, filename: str, content_type: str, client: SSHClient, sftp: paramiko.SFTPClient):
        def iterator():
            try:
                while True:
                    chunk = stream.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            finally:
                stream.close()
                sftp.close()
                client.close()

        return iterator(), filename, content_type, lambda: None

    def upload(self, host: dict[str, Any], directory: str, files: Iterable[Any]) -> list[dict[str, Any]]:
        normalized = self.normalize_path(directory)
        client, sftp = self._open(host)
        results: list[dict[str, Any]] = []
        created_files: list[str] = []
        created_directories: list[str] = []
        total = 0
        try:
            attrs = self._stat(sftp, normalized)
            if not stat.S_ISDIR(attrs.st_mode):
                raise FileManagerError("上传目标不是目录")
            for uploaded in files:
                raw_name = str(getattr(uploaded, "filename", "")).replace("\\", "/")
                relative = PurePosixPath(raw_name)
                if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                    raise FileManagerError("上传文件名无效")
                filename = relative.name
                destination = posixpath.join(normalized, *relative.parts)
                created_directories.extend(self._mkdir_parents(sftp, posixpath.dirname(destination)))
                try:
                    sftp.stat(destination)
                except OSError:
                    pass
                else:
                    raise FileManagerError(f"目标已存在，拒绝覆盖: {destination}")
                remote = sftp.open(destination, "wb")
                created_files.append(destination)
                size = 0
                try:
                    while True:
                        chunk = uploaded.stream.read(self.CHUNK_SIZE)
                        if not chunk:
                            break
                        size += len(chunk)
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise FileManagerError("本次上传总大小超过限制")
                        remote.write(chunk)
                finally:
                    remote.close()
                results.append({"name": filename, "relative_path": str(relative), "path": destination, "size": size})
            return results
        except OSError as exc:
            self._cleanup_created(sftp, created_files, created_directories)
            raise FileManagerError(f"上传失败: {exc}") from exc
        except Exception:
            self._cleanup_created(sftp, created_files, created_directories)
            raise
        finally:
            sftp.close()
            client.close()

    def mkdir(self, host: dict[str, Any], path: str) -> None:
        normalized = self.normalize_path(path, allow_root=False)
        client, sftp = self._open(host)
        try:
            sftp.mkdir(normalized)
        except OSError as exc:
            raise FileManagerError(f"新建目录失败: {exc}") from exc
        finally:
            sftp.close()
            client.close()

    def rename(self, host: dict[str, Any], source: str, destination: str) -> None:
        source_path = self.normalize_path(source, allow_root=False)
        destination_path = self.normalize_path(destination, allow_root=False)
        client, sftp = self._open(host)
        try:
            source_attrs = self._stat(sftp, source_path)
            if stat.S_ISDIR(source_attrs.st_mode) and self._is_same_or_descendant(source_path, destination_path):
                raise FileManagerError("不能将目录移动到自身或其子目录")
            try:
                sftp.stat(destination_path)
            except OSError:
                pass
            else:
                raise FileManagerError("目标路径已存在，拒绝覆盖")
            sftp.rename(source_path, destination_path)
        except OSError as exc:
            raise FileManagerError(f"重命名失败: {exc}") from exc
        finally:
            sftp.close()
            client.close()

    def copy(self, host: dict[str, Any], source: str, destination: str) -> None:
        source_path = self.normalize_path(source, allow_root=False)
        destination_path = self.normalize_path(destination, allow_root=False)
        client, sftp = self._open(host)
        created_files: list[str] = []
        created_directories: list[str] = []
        try:
            try:
                sftp.stat(destination_path)
            except OSError:
                pass
            else:
                raise FileManagerError("目标路径已存在，拒绝覆盖")
            total = 0
            attrs = self._stat(sftp, source_path)
            if stat.S_ISLNK(attrs.st_mode):
                raise FileManagerError("符号链接不能复制")
            if stat.S_ISDIR(attrs.st_mode):
                if self._is_same_or_descendant(source_path, destination_path):
                    raise FileManagerError("不能将目录复制到自身或其子目录")
                sftp.mkdir(destination_path)
                created_directories.append(destination_path)
                for child, relative, child_attrs in self._walk(sftp, source_path):
                    child_destination = posixpath.join(destination_path, relative)
                    if stat.S_ISDIR(child_attrs.st_mode):
                        sftp.mkdir(child_destination)
                        created_directories.append(child_destination)
                        continue
                    if stat.S_ISLNK(child_attrs.st_mode):
                        raise FileManagerError("目录包含符号链接，拒绝复制")
                    parent = posixpath.dirname(child_destination)
                    created_directories.extend(self._mkdir_parents(sftp, parent))
                    total += int(child_attrs.st_size or 0)
                    if total > self.max_bytes:
                        raise FileManagerError("目录总大小超过复制限制")
                    created_files.append(child_destination)
                    self._copy_file(sftp, child, child_destination)
            else:
                if int(attrs.st_size or 0) > self.max_bytes:
                    raise FileManagerError("文件大小超过复制限制")
                created_files.append(destination_path)
                self._copy_file(sftp, source_path, destination_path)
        except OSError as exc:
            self._cleanup_created(sftp, created_files, created_directories)
            raise FileManagerError(f"复制失败: {exc}") from exc
        except Exception:
            self._cleanup_created(sftp, created_files, created_directories)
            raise
        finally:
            sftp.close()
            client.close()

    def _copy_file(self, sftp: paramiko.SFTPClient, source: str, destination: str) -> None:
        with sftp.open(source, "rb") as source_file, sftp.open(destination, "wb") as target_file:
            while True:
                chunk = source_file.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                target_file.write(chunk)

    @staticmethod
    def _mkdir_parents(sftp: paramiko.SFTPClient, path: str) -> list[str]:
        created: list[str] = []
        if not path or path == "/":
            return created
        parts = path.strip("/").split("/")
        current = ""
        for part in parts:
            current += "/" + part
            try:
                sftp.stat(current)
            except OSError:
                sftp.mkdir(current)
                created.append(current)
        return created

    @staticmethod
    def _is_same_or_descendant(source: str, destination: str) -> bool:
        return destination == source or destination.startswith(source.rstrip("/") + "/")

    @staticmethod
    def _cleanup_created(sftp: paramiko.SFTPClient, files: list[str], directories: list[str]) -> None:
        for path in reversed(files):
            try:
                sftp.remove(path)
            except OSError:
                pass
        for path in sorted(set(directories), key=len, reverse=True):
            try:
                sftp.rmdir(path)
            except OSError:
                pass

    def delete(self, host: dict[str, Any], path: str) -> None:
        normalized = self.normalize_path(path, allow_root=False)
        client, sftp = self._open(host)
        try:
            attrs = self._stat(sftp, normalized)
            if stat.S_ISDIR(attrs.st_mode):
                for child, _relative, child_attrs in reversed(list(self._walk(sftp, normalized))):
                    try:
                        (sftp.rmdir if stat.S_ISDIR(child_attrs.st_mode) else sftp.remove)(child)
                    except OSError as exc:
                        raise FileManagerError(f"删除失败: {exc}") from exc
                sftp.rmdir(normalized)
            else:
                sftp.remove(normalized)
        except OSError as exc:
            raise FileManagerError(f"删除失败: {exc}") from exc
        finally:
            sftp.close()
            client.close()

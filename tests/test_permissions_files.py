from __future__ import annotations

import io
import stat
import zipfile
from pathlib import PurePosixPath

from monitor.files import FileManagerError

from .conftest import csrf
from .test_hosts_history import host_payload


class FakeAttr:
    def __init__(self, filename: str, mode: int, size: int = 0):
        self.filename = filename
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = 1710000000


class FakeRemote:
    def __init__(self, sftp, path: str, mode: str):
        self.sftp = sftp
        self.path = path
        self.mode = mode
        self.buffer = io.BytesIO(sftp.files.get(path, b""))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def read(self, size: int = -1):
        return self.buffer.read(size)

    def write(self, value: bytes):
        self.buffer.write(value)
        return len(value)

    def close(self):
        if "w" in self.mode:
            self.sftp.files[self.path] = self.buffer.getvalue()


class FakeSFTP:
    def __init__(self):
        self.files = {"/tmp/readme.txt": b"hello sftp"}
        self.directories = {"/", "/tmp", "/tmp/empty"}
        self.symlinks = {}

    def close(self):
        pass

    def stat(self, path):
        path = str(PurePosixPath(path))
        if path in self.directories:
            return FakeAttr(PurePosixPath(path).name, stat.S_IFDIR | 0o755)
        if path in self.files:
            return FakeAttr(PurePosixPath(path).name, stat.S_IFREG | 0o644, len(self.files[path]))
        raise OSError("not found")

    def lstat(self, path):
        path = str(PurePosixPath(path))
        if path in self.symlinks:
            return FakeAttr(PurePosixPath(path).name, stat.S_IFLNK | 0o777)
        return self.stat(path)

    def listdir_attr(self, path):
        path = str(PurePosixPath(path))
        children = {}
        for directory in self.directories:
            if directory == path or PurePosixPath(directory).parent.as_posix() != path:
                continue
            children[PurePosixPath(directory).name] = FakeAttr(PurePosixPath(directory).name, stat.S_IFDIR | 0o755)
        for filename, data in self.files.items():
            if PurePosixPath(filename).parent.as_posix() == path:
                children[PurePosixPath(filename).name] = FakeAttr(PurePosixPath(filename).name, stat.S_IFREG | 0o644, len(data))
        return list(children.values())

    def open(self, path, mode):
        return FakeRemote(self, str(PurePosixPath(path)), mode)

    def mkdir(self, path):
        path = str(PurePosixPath(path))
        if path in self.directories or path in self.files:
            raise OSError("exists")
        parent = PurePosixPath(path).parent.as_posix()
        if parent not in self.directories:
            raise OSError("parent missing")
        self.directories.add(path)

    def rename(self, source, destination):
        source, destination = str(PurePosixPath(source)), str(PurePosixPath(destination))
        if source in self.files:
            self.files[destination] = self.files.pop(source)
            return
        if source in self.directories:
            self.directories.remove(source)
            self.directories.add(destination)
            for filename in list(self.files):
                if filename.startswith(source + "/"):
                    self.files[destination + filename[len(source):]] = self.files.pop(filename)
            return
        raise OSError("not found")

    def remove(self, path):
        normalized = str(PurePosixPath(path))
        if normalized in self.symlinks:
            self.symlinks.pop(normalized)
            return
        self.files.pop(normalized)

    def rmdir(self, path):
        self.directories.remove(str(PurePosixPath(path)))


class FakeSSH:
    def close(self):
        pass


def login_viewer(app, password="ViewerPass456", first_time=True):
    client = app.test_client()
    if first_time:
        first = client.post("/api/auth/login", json={"username": "viewer", "password": "ViewerPass123"})
        assert first.status_code == 200, first.get_json()
        first_user = first.get_json()["user"]
        changed = client.post(
            "/api/auth/change-password",
            json={"current_password": "ViewerPass123", "new_password": password},
            headers=csrf(first_user),
        )
        assert changed.status_code == 200, changed.get_json()
    response = client.post("/api/auth/login", json={"username": "viewer", "password": password})
    assert response.status_code == 200, response.get_json()
    return client, response.get_json()["user"]


def test_admin_permission_matrix_grants_and_revokes_backend_access(client, app, admin):
    created = client.post(
        "/api/users",
        json={"username": "viewer", "password": "ViewerPass123", "role": "viewer"},
        headers=csrf(admin),
    )
    assert created.status_code == 201
    viewer_client, viewer = login_viewer(app)
    assert viewer_client.get("/api/dashboard").status_code == 200
    assert viewer_client.get("/api/settings").status_code == 403
    assert viewer_client.get("/api/file-manager/hosts").status_code == 403

    elevated = client.post("/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin))
    assert elevated.status_code == 200
    matrix = client.get("/api/permissions")
    assert matrix.status_code == 200
    viewer_id = created.get_json()["id"]
    granted = client.put(
        f"/api/users/{viewer_id}/permissions",
        json={"permissions": ["page.dashboard", "page.settings", "settings.manage"]},
        headers=csrf(admin),
    )
    assert granted.status_code == 200, granted.get_json()

    viewer_client, viewer = login_viewer(app, first_time=False)
    assert viewer_client.get("/api/settings").status_code == 200
    hidden = viewer_client.patch("/api/profile/permissions", json={"visible_pages": []}, headers=csrf(viewer))
    assert hidden.status_code == 200
    assert viewer_client.get("/api/auth/me").get_json()["user"]["visible_pages"] == []

    elevated = client.post("/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin))
    assert elevated.status_code == 200
    revoked = client.put(
        f"/api/users/{viewer_id}/permissions",
        json={"permissions": []},
        headers=csrf(admin),
    )
    assert revoked.status_code == 200
    viewer_client, _viewer = login_viewer(app, first_time=False)
    assert viewer_client.get("/api/dashboard").status_code == 403
    # Explicit denials must not be mistaken for an uninitialized user after a restart.
    app.extensions["permissions"].ensure_defaults()
    assert app.extensions["permissions"].grants(viewer_id) == set()


def test_sftp_file_api_supports_files_directories_and_zip(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    fake_sftp = FakeSFTP()
    monkeypatch.setattr(app.extensions["files"], "_open", lambda _host: (FakeSSH(), fake_sftp))

    listing = client.get(f"/api/hosts/{host['id']}/files?path=/tmp")
    assert listing.status_code == 200
    assert listing.get_json()["items"][0]["name"] in {"empty", "readme.txt"}

    download = client.get(f"/api/hosts/{host['id']}/files/download?path=/tmp/readme.txt")
    assert download.status_code == 200
    assert download.data == b"hello sftp"
    assert "readme.txt" in download.headers["Content-Disposition"]

    upload = client.post(
        f"/api/hosts/{host['id']}/files/upload",
        data={"path": "/tmp", "files": (io.BytesIO(b"nested"), "folder/nested.txt")},
        headers=csrf(admin),
        content_type="multipart/form-data",
    )
    assert upload.status_code == 201, upload.get_json()
    assert fake_sftp.files["/tmp/folder/nested.txt"] == b"nested"

    mkdir = client.post(f"/api/hosts/{host['id']}/files/directories", json={"path": "/tmp/copied"}, headers=csrf(admin))
    assert mkdir.status_code == 201
    copied = client.post(f"/api/hosts/{host['id']}/files/copy", json={"source": "/tmp/readme.txt", "destination": "/tmp/copied/readme.txt"}, headers=csrf(admin))
    assert copied.status_code == 200
    moved = client.patch(f"/api/hosts/{host['id']}/files", json={"source": "/tmp/copied/readme.txt", "destination": "/tmp/copied/renamed.txt"}, headers=csrf(admin))
    assert moved.status_code == 200

    folder_download = client.get(f"/api/hosts/{host['id']}/files/download?path=/tmp/copied")
    assert folder_download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(folder_download.data)) as archive:
        assert "copied/renamed.txt" in archive.namelist()

    elevated = client.post("/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin))
    assert elevated.status_code == 200
    deleted = client.delete(f"/api/hosts/{host['id']}/files", json={"path": "/tmp/copied"}, headers=csrf(admin))
    assert deleted.status_code == 200
    assert "/tmp/copied/renamed.txt" not in fake_sftp.files


def test_sftp_path_and_overwrite_guards(app):
    service = app.extensions["files"]
    assert service.normalize_path("/tmp/../var") == "/var"
    try:
        service.normalize_path("/tmp/\x00bad")
    except FileManagerError:
        pass
    else:
        raise AssertionError("NUL path must be rejected")


def test_sftp_rejects_self_referential_directory_operations(app, monkeypatch):
    service = app.extensions["files"]
    fake_sftp = FakeSFTP()
    fake_sftp.directories.add("/tmp/source")
    fake_sftp.files["/tmp/source/item.txt"] = b"source"
    monkeypatch.setattr(service, "_open", lambda _host: (FakeSSH(), fake_sftp))

    for operation in (service.copy, service.rename):
        try:
            operation({}, "/tmp/source", "/tmp/source/child")
        except FileManagerError as exc:
            assert "自身或其子目录" in str(exc)
        else:
            raise AssertionError("self-referential directory operation must be rejected")
    assert fake_sftp.files["/tmp/source/item.txt"] == b"source"


def test_sftp_symlink_and_upload_failure_guards(app, monkeypatch):
    service = app.extensions["files"]
    fake_sftp = FakeSFTP()
    fake_sftp.directories.add("/tmp/source")
    fake_sftp.files["/tmp/source/item.txt"] = b"source"
    fake_sftp.symlinks["/tmp/link"] = "/tmp/source"
    monkeypatch.setattr(service, "_open", lambda _host: (FakeSSH(), fake_sftp))

    for operation, args in ((service.download, ("/tmp/link",)), (service.copy, ("/tmp/link", "/tmp/link-copy"))):
        try:
            operation({}, *args)
        except FileManagerError as exc:
            assert "符号链接" in str(exc)
        else:
            raise AssertionError("symbolic link operation must be rejected")
    service.delete({}, "/tmp/link")
    assert "/tmp/link" not in fake_sftp.symlinks
    assert fake_sftp.files["/tmp/source/item.txt"] == b"source"

    limited = type("Upload", (), {"filename": "too-large.txt", "stream": io.BytesIO(b"1234")})()
    service.max_bytes = 3
    try:
        service.upload({}, "/tmp", [limited])
    except FileManagerError as exc:
        assert "超过限制" in str(exc)
    else:
        raise AssertionError("oversized upload must be rejected")
    assert "/tmp/too-large.txt" not in fake_sftp.files

from __future__ import annotations

import shlex
from urllib.parse import quote

from flask import Response, jsonify, request, stream_with_context

from ..files import FileManagerError
from ..web import WebContext


def register_file_routes(context: WebContext) -> None:
    app = context.app
    audit_action = context.audit_action
    body = context.body
    config = context.config
    development = context.development
    file_host = context.file_host
    files = context.files
    hosts = context.hosts
    operations = context.operations
    login_required = context.login_required

    @app.get("/api/hosts/<int:host_id>/files/usage")
    @login_required(permission="storage.scan")
    def directory_usage(host_id: int):
        settings = config.all()
        return jsonify(development.directory_usage(
            file_host(host_id), request.args.get("path", ""),
            request.args.get("timeout_seconds", settings["scan_timeout_seconds"]),
        ))

    @app.get("/api/hosts/<int:host_id>/files/large-files")
    @login_required(permission="storage.scan")
    def large_files(host_id: int):
        settings = config.all()
        return jsonify(development.large_files(
            file_host(host_id), request.args.get("path", ""),
            request.args.get("minimum_bytes", settings["scan_minimum_mib"] * 1024 * 1024),
            request.args.get("limit", settings["scan_result_limit"]),
            request.args.get("max_depth", settings["scan_max_depth"]),
            request.args.get("timeout_seconds", settings["scan_timeout_seconds"]),
        ))

    @app.get("/api/file-manager/hosts")
    @login_required(permission="files.browse")
    def file_manager_hosts():
        items = hosts.list()
        return jsonify(items=[{"id": item["id"], "name": item["name"], "address": item["address"], "username": item["username"], "status": item.get("status")} for item in items])

    @app.get("/api/hosts/<int:host_id>/files")
    @login_required(permission="files.browse")
    def list_files(host_id: int):
        return jsonify(files.list_directory(file_host(host_id), request.args.get("path", "/")))

    @app.get("/api/hosts/<int:host_id>/files/download")
    @login_required(permission="files.download")
    def download_file(host_id: int):
        path = request.args.get("path", "")
        iterator, filename, content_type, _cleanup = files.download(file_host(host_id), path)
        audit_action("file_downloaded", target_type="host", target_id=host_id, summary=f"下载远端路径 {path}")
        return Response(
            stream_with_context(iterator),
            content_type=content_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )

    @app.get("/api/hosts/<int:host_id>/files/preview")
    @login_required(permission="files.download")
    def preview_file(host_id: int):
        result = files.preview(file_host(host_id), request.args.get("path", ""))
        return jsonify(result)

    @app.post("/api/hosts/<int:host_id>/files/batch-download-script")
    @login_required(permission="files.download", write=True)
    def batch_download_script(host_id: int):
        payload = body()
        result = files.batch_archive_script(
            file_host(host_id),
            payload.get("paths"),
            str(payload.get("local_path", "./selected-files.tar.gz")),
        )
        audit_action("files_batch_download_script", target_type="host", target_id=host_id, summary=f"生成批量文件打包脚本（{len(result['paths'])} 项）")
        return jsonify(result)

    @app.post("/api/hosts/<int:host_id>/files/batch-delete")
    @login_required(permission="files.delete", write=True, elevated=True)
    def batch_delete_files(host_id: int):
        payload = body()
        deleted = files.delete_many(file_host(host_id), payload.get("paths"))
        audit_action("files_batch_deleted", target_type="host", target_id=host_id, summary=f"批量删除远端路径（{len(deleted)} 项）")
        return jsonify(deleted=deleted)

    @app.post("/api/hosts/<int:host_id>/files/diff")
    @login_required(permission="files.download", write=True)
    def diff_files(host_id: int):
        payload = body()
        first, second = str(payload.get("left", "")), str(payload.get("right", ""))
        left = files.normalize_path(first, allow_root=False)
        right = files.normalize_path(second, allow_root=False)
        if left == right:
            raise FileManagerError("请选择两个不同的文件")
        allowed = {".txt", ".log", ".py", ".json", ".yaml", ".yml", ".sh", ".md", ".csv", ".ini", ".conf"}
        from pathlib import PurePosixPath
        if PurePosixPath(left).suffix.lower() not in allowed or PurePosixPath(right).suffix.lower() not in allowed:
            raise FileManagerError("文件对比仅支持常见文本文件")
        host = file_host(host_id)
        command = (
            "set -eu; for file in %s %s; do test -f -- \"$file\" || { echo '文件不存在: '$file >&2; exit 2; }; "
            "size=$(wc -c < \"$file\"); [ \"$size\" -le 1048576 ] || { echo '文件过大，禁止在线对比: '$file >&2; exit 3; }; done; "
            "LC_ALL=C diff -u -- %s %s | head -c 262144"
        ) % (shlex.quote(left), shlex.quote(right), shlex.quote(left), shlex.quote(right))
        result = operations.run(host, command, config.all()["collection_timeout"], 262144)
        if result.exit_code not in {0, 1}:
            raise FileManagerError((result.stderr or "文件对比失败").strip())
        return jsonify(left=left, right=right, diff=result.stdout, truncated=result.stdout_truncated)

    @app.post("/api/hosts/<int:host_id>/files/search")
    @login_required(permission="files.browse", write=True)
    def search_files(host_id: int):
        payload = body()
        root = files.normalize_path(str(payload.get("path", "/")))
        pattern = str(payload.get("pattern", "")).strip()
        if not pattern or len(pattern) > 255 or any(char in pattern for char in "\x00\r\n/"):
            raise FileManagerError("文件名过滤条件无效")
        try:
            limit = max(1, min(500, int(payload.get("limit", 100))))
        except (TypeError, ValueError) as exc:
            raise FileManagerError("搜索结果数量无效") from exc
        command = f"find {shlex.quote(root)} -maxdepth 12 -type f -name {shlex.quote(pattern)} -print 2>/dev/null | head -n {limit}"
        result = operations.run(file_host(host_id), command, config.all()["scan_timeout_seconds"], 128 * 1024)
        if result.exit_code != 0:
            raise FileManagerError((result.stderr or "远程搜索失败").strip())
        return jsonify(path=root, pattern=pattern, items=[line for line in result.stdout.splitlines() if line], truncated=result.stdout_truncated)

    @app.post("/api/hosts/<int:host_id>/files/permission-script")
    @login_required(permission="files.manage", write=True)
    def permission_script(host_id: int):
        payload = body()
        result = files.permission_script(file_host(host_id), str(payload.get("path", "")), payload.get("mode"), payload.get("owner"), payload.get("group"))
        audit_action("file_permission_script_generated", target_type="host", target_id=host_id, summary=f"生成远端权限脚本 {payload.get('path')}")
        return jsonify(result)

    @app.post("/api/hosts/<int:host_id>/files/transfer-script")
    @login_required(permission="files.browse", write=True)
    def transfer_script(host_id: int):
        payload = body()
        result = files.transfer_script(file_host(host_id), str(payload.get("path", "")), direction=str(payload.get("direction", "download")), local_path=str(payload.get("local_path", ".")))
        audit_action("file_transfer_script_generated", target_type="host", target_id=host_id, summary=f"生成大文件传输脚本 {payload.get('path')}")
        return jsonify(result)

    @app.post("/api/hosts/<int:host_id>/files/upload")
    @login_required(permission="files.upload", write=True)
    def upload_files(host_id: int):
        uploaded = request.files.getlist("files")
        if not uploaded:
            raise FileManagerError("请选择要上传的文件或文件夹")
        directory = request.form.get("path", "/")
        result = files.upload(file_host(host_id), directory, uploaded)
        audit_action("files_uploaded", target_type="host", target_id=host_id, summary=f"上传 {len(result)} 个文件到 {directory}")
        return jsonify(items=result), 201

    @app.post("/api/hosts/<int:host_id>/files/directories")
    @login_required(permission="files.manage", write=True)
    def create_directory(host_id: int):
        path = str(body().get("path", ""))
        files.mkdir(file_host(host_id), path)
        audit_action("directory_created", target_type="host", target_id=host_id, summary=f"新建远端目录 {path}")
        return jsonify(ok=True), 201

    @app.post("/api/hosts/<int:host_id>/files/copy")
    @login_required(permission="files.manage", write=True)
    def copy_file(host_id: int):
        payload = body()
        source, destination = str(payload.get("source", "")), str(payload.get("destination", ""))
        files.copy(file_host(host_id), source, destination)
        audit_action("file_copied", target_type="host", target_id=host_id, summary=f"复制远端路径 {source} 到 {destination}")
        return jsonify(ok=True)

    @app.patch("/api/hosts/<int:host_id>/files")
    @login_required(permission="files.manage", write=True)
    def move_file(host_id: int):
        payload = body()
        source, destination = str(payload.get("source", "")), str(payload.get("destination", ""))
        files.rename(file_host(host_id), source, destination)
        audit_action("file_moved", target_type="host", target_id=host_id, summary=f"移动或重命名远端路径 {source} 到 {destination}")
        return jsonify(ok=True)

    @app.delete("/api/hosts/<int:host_id>/files")
    @login_required(permission="files.delete", write=True, elevated=True)
    def delete_file(host_id: int):
        path = str(body().get("path", ""))
        files.delete(file_host(host_id), path)
        audit_action("file_deleted", target_type="host", target_id=host_id, summary=f"删除远端路径 {path}")
        return jsonify(ok=True)

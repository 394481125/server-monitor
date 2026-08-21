from __future__ import annotations

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

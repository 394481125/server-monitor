from __future__ import annotations

import atexit
import codecs
from datetime import timedelta
import json
import queue
import secrets
import shlex
import logging
import os
import shutil
import threading
import time
import uuid
import weakref
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from flask import Flask, Response, g, jsonify, render_template, request, stream_with_context
from flask_sock import Sock
from werkzeug.exceptions import RequestEntityTooLarge

from .audit import AuditService
from .auth import AuthError, AuthService, LoginLocked
from .background import BackgroundService
from .collector import Collector
from .config import ConfigError, ConfigStore
from .db import Database
from .development import DevelopmentService
from .files import FileManagerError, SFTPFileService
from .gpu_scheduler import GPUScheduler
from .operations import OperationError, OperationService
from .notifications import NotificationService
from .permissions import PermissionService
from .security import PasswordService, ProcessLock, SecretBox, redact
from .services import (
    AlertService,
    BackupService,
    HistoryService,
    HostService,
    ServiceError,
    compact_collection_result,
    export_csv,
    host_transfer_rows,
    parse_host_import,
)
from .ssh_client import SSHClient, SSHError, SSHFingerprintError
from .utils import clamp_page, clamp_page_size, json_dump, json_load, paged, parse_utc, utc_iso, utc_now


LOGGER = logging.getLogger("server_monitor")
COOKIE_NAME = "server_monitor_session"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _tmux_attach_command(name: str) -> str:
    return (
        "if locale -a 2>/dev/null | grep -Eiq '^(C\\.UTF-8|C\\.utf8|en_US\\.UTF-8|en_US\\.utf8)$'; "
        "then export LANG=C.UTF-8 LC_ALL=C.UTF-8; fi; "
        f"tmux attach-session -t {shlex.quote(name)}\n"
    )


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    root = Path(__file__).resolve().parent.parent
    app.config.from_mapping(
        DATA_DIR=os.environ.get("SERVER_MONITOR_DATA_DIR", str(root / "data")),
        DATABASE=os.environ.get("SERVER_MONITOR_DATABASE"),
        MASTER_KEY=os.environ.get("SERVER_MONITOR_MASTER_KEY"),
        # Production deployments must explicitly choose the one-time bootstrap password.
        # Tests can still provide their fixture password through TESTING/test_config.
        INITIAL_ADMIN_PASSWORD=os.environ.get("SERVER_MONITOR_INITIAL_PASSWORD"),
        START_BACKGROUND=True,
        ACQUIRE_PROCESS_LOCK=True,
        SESSION_COOKIE_SECURE=os.environ.get("SERVER_MONITOR_HTTPS") == "1",
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=int(os.environ.get("SERVER_MONITOR_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024))),
        FILE_TRANSFER_LIMIT=int(os.environ.get("SERVER_MONITOR_FILE_TRANSFER_LIMIT", str(512 * 1024 * 1024))),
    )
    if test_config:
        app.config.update(test_config)
    if app.config["INITIAL_ADMIN_PASSWORD"] is None and app.config.get("TESTING"):
        app.config["INITIAL_ADMIN_PASSWORD"] = "qwer1234"
    data_dir = Path(app.config["DATA_DIR"])
    started_monotonic = time.monotonic()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(data_dir, 0o700)
    if data_dir.stat().st_mode & 0o077:
        raise RuntimeError("数据目录权限过宽，必须设置为 0700")
    process_lock = None
    if app.config["ACQUIRE_PROCESS_LOCK"]:
        process_lock = ProcessLock(data_dir / "server-monitor.lock")
        process_lock.acquire()
        weakref.finalize(app, process_lock.release)
    database = Database(app.config["DATABASE"] or data_dir / "server-monitor.sqlite3")
    database.initialize()
    if not database.query_one("SELECT id FROM users LIMIT 1"):
        if app.config["INITIAL_ADMIN_PASSWORD"] is None:
            raise RuntimeError("首次启动必须设置 SERVER_MONITOR_INITIAL_PASSWORD（至少 8 个字符）")
        PasswordService.validate_initial(app.config["INITIAL_ADMIN_PASSWORD"])
    secret_box = SecretBox(app.config["MASTER_KEY"] or data_dir / "master.key")
    config = ConfigStore(database)
    permission_service = PermissionService(database)
    permission_service.ensure_defaults()
    audit = AuditService(database)
    auth = AuthService(database, config)
    hosts = HostService(database, secret_box, config)
    alerts = AlertService(database, config)
    notifications = NotificationService(database, config, secret_box)
    alerts.notifier = notifications
    scheduler = GPUScheduler(database, config, audit, alerts)
    database.execute(
        "UPDATE gpu_runtime SET state='unknown',idle_seconds_accum=0,last_valid_at=NULL,attempts=0,"
        "retry_at=NULL,cooldown_until=NULL,frozen_until=NULL,last_error='应用重启，重新开始空闲计时',updated_at=?",
        (utc_iso(),),
    )
    operations = OperationService(secret_box, config, database)
    development = DevelopmentService(operations, config)
    files = SFTPFileService(secret_box, config.all(), app.config["FILE_TRANSFER_LIMIT"])
    backups = BackupService(database, data_dir)
    generated = auth.ensure_initial_admin(app.config.get("INITIAL_ADMIN_PASSWORD"))
    permission_service.ensure_defaults()
    if generated:
        LOGGER.warning("首次管理员账号已创建，首次登录必须修改密码")

    app.extensions.update(
        database=database,
        secret_box=secret_box,
        monitor_config=config,
        auth_service=auth,
        audit=audit,
        hosts=hosts,
        alerts=alerts,
        gpu_scheduler=scheduler,
        operations=operations,
        development=development,
        backups=backups,
        notifications=notifications,
        permissions=permission_service,
        files=files,
    )

    background = None
    if process_lock:
        app.extensions["process_lock"] = process_lock
    if app.config["START_BACKGROUND"]:
        background = BackgroundService(hosts, config, secret_box, database, scheduler, alerts, audit, backups)
        background.start()
        app.extensions["background"] = background

    closed = threading.Event()

    def shutdown() -> None:
        if closed.is_set():
            return
        closed.set()
        if background:
            background.stop()
        notifications.close()
        if process_lock:
            process_lock.release()

    atexit.register(shutdown)
    app.extensions["shutdown"] = shutdown

    @app.before_request
    def load_user() -> None:
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        g.csp_style_nonce = secrets.token_urlsafe(16)
        g.session_token = request.cookies.get(COOKIE_NAME)
        g.user = permission_service.decorate(auth.authenticate(g.session_token))

    @app.after_request
    def response_headers(response: Response) -> Response:
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        style_nonce = getattr(g, "csp_style_nonce", "")
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; "
            f"style-src-elem 'self' 'nonce-{style_nonce}'; style-src-attr 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self' ws: wss:"
        )
        response.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
        return response

    @app.errorhandler(AuthError)
    @app.errorhandler(ServiceError)
    @app.errorhandler(OperationError)
    @app.errorhandler(SSHError)
    @app.errorhandler(ConfigError)
    @app.errorhandler(ValueError)
    def known_error(error: Exception):
        return jsonify(error=str(error), request_id=getattr(g, "request_id", None)), 400

    @app.errorhandler(404)
    def not_found(_error: Exception):
        return jsonify(error="资源不存在", request_id=getattr(g, "request_id", None)), 404

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error: RequestEntityTooLarge):
        limit = app.config.get("MAX_CONTENT_LENGTH")
        message = f"上传内容超过限制 {limit} 字节" if limit else "上传内容超过限制"
        return jsonify(error=message, request_id=getattr(g, "request_id", None)), 413

    @app.errorhandler(500)
    def internal_error(error: Exception):
        LOGGER.exception("request_id=%s unexpected error", getattr(g, "request_id", None), exc_info=error)
        return jsonify(error="服务器内部错误", request_id=getattr(g, "request_id", None)), 500

    def login_required(admin: bool = False, permission: str | None = None, write: bool = False, elevated: bool = False):
        def decorator(function: Callable[..., Any]):
            @wraps(function)
            def wrapped(*args: Any, **kwargs: Any):
                if not g.user:
                    return jsonify(error="请先登录"), 401
                if admin and g.user["role"] != "admin":
                    return jsonify(error="权限不足"), 403
                if permission and not permission_service.allowed(g.user, permission):
                    return jsonify(error="当前账户未获得该功能权限", permission=permission), 403
                if write or request.method in WRITE_METHODS:
                    try:
                        auth.require_csrf(g.user, request.headers.get("X-CSRF-Token"))
                    except AuthError as exc:
                        return jsonify(error=str(exc)), 403
                    if g.user["must_change_password"] and request.endpoint not in {"change_password", "logout"}:
                        return jsonify(error="首次登录或密码重置后必须先修改密码", must_change_password=True), 403
                if elevated and not auth.is_elevated(g.user):
                    return jsonify(error="该操作需要重新验证当前密码", requires_elevation=True), 403
                return function(*args, **kwargs)

            return wrapped

        return decorator

    def body() -> dict[str, Any]:
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return value

    def source_ip() -> str:
        # The app does not trust forwarding headers unless a deployment explicitly adds ProxyFix.
        return request.remote_addr or "unknown"

    def audit_action(action: str, *, target_type: str | None = None, target_id: Any = None, success: bool = True, summary: str = "", error: str | None = None) -> None:
        audit.write(action, actor=g.user, source_ip=source_ip(), target_type=target_type, target_id=target_id, request_id=g.request_id, success=success, summary=summary, error=error)

    @app.get("/")
    def index():
        return render_template("index.html", csp_style_nonce=g.csp_style_nonce)

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    @app.get("/health")
    def health():
        database.query_one("SELECT 1")
        return jsonify(status="ok", background=bool(background and background._thread and background._thread.is_alive()))

    @app.get("/api/platform-status")
    @login_required()
    def platform_status():
        memory_total = None
        memory_available = None
        system_uptime_seconds = None
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition(":")
                if separator and key in {"MemTotal", "MemAvailable"}:
                    parsed = value.strip().split()[0]
                    if parsed.isdigit():
                        if key == "MemTotal":
                            memory_total = int(parsed) * 1024
                        else:
                            memory_available = int(parsed) * 1024
        except OSError:
            pass
        try:
            system_uptime_seconds = max(0, int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])))
        except (OSError, ValueError, IndexError):
            pass
        disk = shutil.disk_usage(data_dir)
        try:
            load_one = round(os.getloadavg()[0], 2)
        except OSError:
            load_one = None
        managed = hosts.list()
        reachable = sum(1 for item in managed if item.get("status") in {"online", "busy", "degraded"})
        storage = database.storage_info()
        memory_used = memory_total - memory_available if memory_total is not None and memory_available is not None else None
        return jsonify(
            hostname=os.uname().nodename,
            uptime_seconds=system_uptime_seconds if system_uptime_seconds is not None else max(0, int(time.monotonic() - started_monotonic)),
            application_uptime_seconds=max(0, int(time.monotonic() - started_monotonic)),
            load_one=load_one,
            memory_used_bytes=memory_used,
            memory_total_bytes=memory_total,
            memory_usage_percent=round(memory_used / memory_total * 100, 1) if memory_used is not None and memory_total else None,
            disk_used_bytes=disk.used,
            disk_total_bytes=disk.total,
            disk_usage_percent=round(disk.used / disk.total * 100, 1) if disk.total else None,
            database_bytes=storage["database_total_bytes"],
            background_running=bool(background and background._thread and background._thread.is_alive()),
            managed_hosts=len(managed),
            reachable_hosts=reachable,
        )

    @app.post("/api/auth/login")
    def login():
        payload = body()
        try:
            token, user = auth.login(str(payload.get("username", "")), str(payload.get("password", "")), source_ip())
        except (AuthError, LoginLocked) as exc:
            audit.write("login_failed", source_ip=source_ip(), summary=f"登录失败: {payload.get('username', '')}", success=False, error=str(exc), request_id=g.request_id)
            return jsonify(error=str(exc)), 429 if isinstance(exc, LoginLocked) else 401
        user = permission_service.decorate(user)
        audit.write("login_success", actor=user, source_ip=source_ip(), summary="登录成功", request_id=g.request_id)
        response = jsonify(user=user)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Lax", secure=app.config["SESSION_COOKIE_SECURE"], path="/")
        return response

    @app.get("/api/auth/me")
    @login_required()
    def me():
        return jsonify(user=g.user)

    @app.post("/api/auth/logout")
    @login_required(write=True)
    def logout():
        audit_action("logout", summary="退出登录")
        auth.logout(g.session_token)
        response = jsonify(ok=True)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.post("/api/auth/elevate")
    @login_required(write=True)
    def elevate():
        until = auth.elevate(g.user, str(body().get("password", "")))
        audit_action("session_elevated", summary="危险操作再认证成功")
        return jsonify(elevated_until=until)

    @app.post("/api/auth/change-password")
    @login_required(write=True)
    def change_password():
        payload = body()
        auth.change_password(g.user["id"], str(payload.get("current_password", "")), str(payload.get("new_password", "")))
        audit_action("password_changed", target_type="user", target_id=g.user["id"], summary="用户修改密码")
        response = jsonify(ok=True, relogin_required=True)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/api/users")
    @login_required(admin=True)
    def list_users():
        rows = database.query_all("SELECT id,username,role,active,must_change_password,theme,created_at,updated_at FROM users ORDER BY username COLLATE NOCASE")
        return jsonify(items=[{**dict(row), "active": bool(row["active"]), "must_change_password": bool(row["must_change_password"])} for row in rows])

    @app.get("/api/permissions")
    @login_required(admin=True)
    def list_permission_matrix():
        rows = database.query_all("SELECT id,username,role,active,created_at,updated_at FROM users ORDER BY username COLLATE NOCASE")
        users = []
        for row in rows:
            item = dict(row)
            item["active"] = bool(item["active"])
            item.update(permission_service.user_permissions(item["id"]))
            if item["role"] == "admin":
                item["granted"] = sorted(entry["key"] for entry in permission_service.catalog())
                item["visible_pages"] = sorted(entry["key"] for entry in permission_service.catalog() if entry["kind"] == "page")
            users.append(item)
        return jsonify(catalog=permission_service.catalog(), users=users)

    @app.put("/api/users/<int:user_id>/permissions")
    @login_required(admin=True, write=True, elevated=True)
    def update_user_permissions(user_id: int):
        granted = permission_service.set_grants(user_id, body().get("permissions"))
        database.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        audit_action("user_permissions_updated", target_type="user", target_id=user_id, summary="管理员修改用户功能权限")
        return jsonify(granted=granted)

    @app.get("/api/profile/permissions")
    @login_required()
    def profile_permissions():
        values = permission_service.user_permissions(g.user["id"])
        if g.user["role"] == "admin":
            values["granted"] = sorted(item["key"] for item in permission_service.catalog())
            values["visible_pages"] = sorted(item["key"] for item in permission_service.catalog() if item["kind"] == "page")
        return jsonify(catalog=permission_service.catalog(), **values)

    @app.patch("/api/profile/permissions")
    @login_required(write=True)
    def update_profile_permissions():
        visible_pages = permission_service.set_visibility(g.user["id"], body().get("visible_pages"))
        audit_action("profile_visibility_updated", target_type="user", target_id=g.user["id"], summary="用户更新页面显示偏好")
        return jsonify(visible_pages=visible_pages)

    @app.post("/api/users")
    @login_required(admin=True, write=True)
    def create_user():
        payload = body()
        username = str(payload.get("username", "")).strip()
        role = payload.get("role", "viewer")
        if not username or len(username) > 64 or role not in {"admin", "viewer"}:
            raise ValueError("用户名或角色无效")
        if database.query_one("SELECT id FROM users WHERE username=? COLLATE NOCASE", (username,)):
            raise ValueError("用户名已存在")
        password = str(payload.get("password", ""))
        PasswordService.validate(password)
        now = utc_iso()
        user_id = database.execute("INSERT INTO users(username,password_hash,role,active,must_change_password,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (username, PasswordService.hash(password), role, 1, 1, now, now))
        permission_service.initialize_user(user_id, role)
        audit_action("user_created", target_type="user", target_id=user_id, summary=f"创建用户 {username}")
        return jsonify(id=user_id), 201

    def ensure_not_last_admin(user_id: int, role: str | None = None, active: bool | None = None, deleting: bool = False) -> None:
        target = database.query_one("SELECT role,active FROM users WHERE id=?", (user_id,))
        if not target:
            raise ValueError("用户不存在")
        removes_admin = target["role"] == "admin" and target["active"] and (deleting or role == "viewer" or active is False)
        if removes_admin and database.query_one("SELECT COUNT(*) count FROM users WHERE role='admin' AND active=1")["count"] <= 1:
            raise ValueError("不能删除、禁用或降级最后一个有效超级管理员")

    @app.patch("/api/users/<int:user_id>")
    @login_required(admin=True, write=True)
    def update_user(user_id: int):
        payload = body()
        role = payload.get("role")
        active = payload.get("active")
        if role is not None and role not in {"admin", "viewer"}:
            raise ValueError("角色无效")
        if active is not None and not isinstance(active, bool):
            raise ValueError("active 必须是布尔值")
        ensure_not_last_admin(user_id, role, active)
        updates: dict[str, Any] = {"updated_at": utc_iso()}
        if role is not None:
            updates["role"] = role
        if active is not None:
            updates["active"] = int(active)
        if "theme" in payload:
            if payload["theme"] not in {"light", "dark", "tech"}:
                raise ValueError("主题无效")
            updates["theme"] = payload["theme"]
        with database.transaction() as connection:
            connection.execute("UPDATE users SET " + ",".join(f"{key}=?" for key in updates) + " WHERE id=?", [*updates.values(), user_id])
            connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        if role is not None:
            permission_service.initialize_user(user_id, role)
        audit_action("user_updated", target_type="user", target_id=user_id, summary="用户状态或角色已修改")
        return jsonify(ok=True)

    @app.post("/api/users/<int:user_id>/reset-password")
    @login_required(admin=True, write=True, elevated=True)
    def reset_password(user_id: int):
        if not database.query_one("SELECT id FROM users WHERE id=?", (user_id,)):
            raise ValueError("用户不存在")
        password = str(body().get("password", ""))
        PasswordService.validate(password)
        with database.transaction() as connection:
            connection.execute("UPDATE users SET password_hash=?,must_change_password=1,updated_at=? WHERE id=?", (PasswordService.hash(password), utc_iso(), user_id))
            connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        audit_action("password_reset", target_type="user", target_id=user_id, summary="管理员重置用户密码")
        return jsonify(ok=True)

    @app.delete("/api/users/<int:user_id>")
    @login_required(admin=True, write=True, elevated=True)
    def delete_user(user_id: int):
        ensure_not_last_admin(user_id, deleting=True)
        database.execute("DELETE FROM users WHERE id=?", (user_id,))
        audit_action("user_deleted", target_type="user", target_id=user_id, summary="删除用户")
        return jsonify(ok=True)

    def connection_test(
        payload: dict[str, Any],
        current_host_id: int | None = None,
        confirmed_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if confirmed_fingerprint is not None and (
            not isinstance(confirmed_fingerprint, str)
            or not confirmed_fingerprint.startswith("SHA256:")
            or len(confirmed_fingerprint) > 128
        ):
            raise ValueError("确认的 SSH 主机指纹格式无效")
        current: dict[str, Any] | None = None
        if current_host_id is None:
            clean = hosts._validate(payload, partial=False)
            candidate = {"port": 22, "timeout_seconds": None, **clean}
        else:
            current = hosts.get(current_host_id, include_secrets=True)
            clean = hosts._validate(payload, partial=True)
            candidate = {**current, **clean}
        if confirmed_fingerprint:
            candidate["fingerprint"] = confirmed_fingerprint
        client = SSHClient(candidate, secret_box, config.all())
        try:
            fingerprint = client.connect()
            result = client.run("LC_ALL=C sh -c 'hostname; cat /etc/machine-id 2>/dev/null || true; command -v tmux || true; command -v nvidia-smi || true'", config.all()["collection_timeout"])
            if result.exit_code != 0:
                raise ValueError(redact(result.stderr) or "远端 Shell 不可用")
            lines = result.stdout.splitlines()
            machine_id = lines[1].strip() if len(lines) > 1 and len(lines[1].strip()) >= 8 else None
            physical_id, degraded = hosts.physical_id(fingerprint, machine_id)
            duplicate = database.query_one(
                "SELECT id,name FROM hosts WHERE physical_id=? AND deleted_at IS NULL"
                + (" AND id<>?" if current_host_id is not None else ""),
                (physical_id, current_host_id) if current_host_id is not None else (physical_id,),
            )
            return {
                "success": True,
                "fingerprint": fingerprint,
                "machine_id": machine_id,
                "physical_id": physical_id,
                "identity_degraded": degraded,
                "hostname": lines[0] if lines else None,
                "duplicate": dict(duplicate) if duplicate else None,
                "fingerprint_changed": bool(current and current.get("fingerprint") != fingerprint),
                "machine_id_changed": bool(current and current.get("machine_id") and machine_id and current.get("machine_id") != machine_id),
            }
        finally:
            client.close()

    def fingerprint_mismatch_response(error: SSHFingerprintError):
        return jsonify(
            error=str(error),
            fingerprint_mismatch=True,
            expected=error.expected,
            observed=error.observed,
            request_id=getattr(g, "request_id", None),
        ), 409

    @app.post("/api/hosts/test")
    @login_required(permission="host.manage", write=True)
    def test_host_connection():
        result = connection_test(body())
        audit_action("host_connection_test", target_type="host", summary="SSH 连接测试成功")
        return jsonify(result)

    @app.post("/api/hosts/<int:host_id>/test")
    @login_required(permission="host.manage", write=True, elevated=True)
    def test_existing_host_connection(host_id: int):
        payload = body()
        confirmed_fingerprint = payload.pop("confirmed_fingerprint", None)
        payload.pop("confirmed_physical_replacement", None)
        try:
            result = connection_test(payload, host_id, confirmed_fingerprint)
        except SSHFingerprintError as error:
            audit_action("host_fingerprint_mismatch", target_type="host", target_id=host_id, success=False, summary="SSH 连接测试发现主机指纹变化", error=str(error))
            return fingerprint_mismatch_response(error)
        audit_action("host_connection_test", target_type="host", target_id=host_id, summary="SSH 连接测试成功")
        return jsonify(result)

    @app.get("/api/hosts")
    @login_required(permission="page.hosts")
    def list_hosts():
        return jsonify(items=hosts.list(search=request.args.get("search"), status=request.args.get("status")))

    def host_transfer_response(format_name: str, items: list[dict[str, Any]], *, template: bool = False) -> Response:
        stamp = utc_now().strftime("%Y%m%d_%H%M%S")
        if format_name == "json":
            transfer_rows = host_transfer_rows(items)
            if template:
                for row in transfer_rows:
                    row.update({"auth_secret": "", "private_key": "", "private_key_passphrase": "", "sudo_password": ""})
            content = json.dumps(
                {
                    "format_version": 1,
                    "credentials_included": False,
                    "hosts": transfer_rows,
                },
                ensure_ascii=False,
                indent=2,
            )
            filename = "主机导入模板.json" if template else f"主机配置_{stamp}.json"
            return Response(content, content_type="application/json; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})
        if format_name != "csv":
            raise ValueError("导出格式仅支持 json 或 csv")
        rows = host_transfer_rows(items, csv_mode=True)
        filename, content = export_csv(rows, "主机导入模板" if template else "主机配置")
        return Response(content, content_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    @app.get("/api/hosts/export")
    @login_required(permission="host.manage")
    def export_hosts():
        format_name = request.args.get("format", "json").lower()
        response = host_transfer_response(format_name, hosts.list())
        audit_action("hosts_exported", target_type="host", summary=f"导出 {len(hosts.list())} 台主机的非敏感配置")
        return response

    @app.get("/api/hosts/import-template")
    @login_required(permission="host.manage")
    def host_import_template():
        format_name = request.args.get("format", "csv").lower()
        sample = {
            "name": "gpu-node-01", "address": "10.0.0.10", "port": 22,
            "username": "monitor", "auth_type": "password", "auth_secret": "在导入前填写",
            "private_key": "", "private_key_passphrase": "", "sudo_password": "",
            "tags": ["GPU", "测试"], "notes": "示例主机", "enabled": True,
            "docker_enabled": True, "allow_tmux": True, "allow_terminal": True,
            "allow_process": True, "allow_install": False, "allow_stress": False,
            "timeout_seconds": 15,
        }
        return host_transfer_response(format_name, [sample], template=True)

    @app.post("/api/hosts")
    @login_required(permission="host.manage", write=True)
    def create_host():
        payload = body()
        payload.pop("identity", None)
        identity = connection_test(payload)
        if identity.get("duplicate"):
            raise ValueError(f"该物理主机已被纳管: {identity['duplicate']['name']}")
        host = hosts.create(payload, fingerprint=identity["fingerprint"], machine_id=identity.get("machine_id"))
        audit_action("host_created", target_type="host", target_id=host["id"], summary=f"新增主机 {host['name']}")
        return jsonify(host=host), 201

    @app.post("/api/hosts/import")
    @login_required(permission="host.manage", write=True, elevated=True)
    def import_hosts():
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            raise ValueError("请选择 JSON 或 CSV 主机导入文件")
        rows = parse_host_import(uploaded.stream.read(2 * 1024 * 1024 + 1), uploaded.filename)
        results = []
        for index, payload in enumerate(rows, 1):
            label = str(payload.get("name") or payload.get("address") or f"第 {index} 条")
            try:
                identity = connection_test(payload)
                if identity.get("duplicate"):
                    raise ValueError(f"该物理主机已被纳管: {identity['duplicate']['name']}")
                host = hosts.create(payload, fingerprint=identity["fingerprint"], machine_id=identity.get("machine_id"))
                results.append({"row": index, "name": label, "success": True, "host_id": host["id"]})
            except Exception as exc:
                results.append({"row": index, "name": label, "success": False, "error": redact(str(exc))})
        success_count = sum(1 for item in results if item["success"])
        audit_action(
            "hosts_imported", target_type="host",
            success=success_count == len(results),
            summary=f"批量导入主机：成功 {success_count} 台，失败 {len(results) - success_count} 台",
        )
        return jsonify(results=results, success_count=success_count, failure_count=len(results) - success_count), 207 if success_count != len(results) else 201

    @app.post("/api/hosts/batch-test")
    @login_required(permission="host.manage", write=True, elevated=True)
    def batch_test_hosts():
        raw_ids = body().get("host_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("host_ids 必须是非空数组")
        if len(raw_ids) > 100:
            raise ValueError("单次最多重测 100 台主机")
        results = []
        for raw_id in dict.fromkeys(raw_ids):
            try:
                host_id = int(raw_id)
                host = hosts.get(host_id)
                identity = connection_test({}, host_id)
                if identity.get("duplicate"):
                    results.append({"host_id": host_id, "name": host["name"], "status": "duplicate", "duplicate": identity["duplicate"]})
                elif identity.get("machine_id_changed"):
                    results.append({"host_id": host_id, "name": host["name"], "status": "physical_identity_changed", "identity": identity})
                else:
                    results.append({"host_id": host_id, "name": host["name"], "status": "ok", "identity": identity})
            except SSHFingerprintError as exc:
                results.append({"host_id": raw_id, "status": "fingerprint_mismatch", "expected": exc.expected, "observed": exc.observed, "error": str(exc)})
            except Exception as exc:
                results.append({"host_id": raw_id, "status": "failed", "error": redact(str(exc))})
        failed = sum(1 for item in results if item["status"] != "ok")
        audit_action("hosts_batch_tested", target_type="host", success=failed == 0, summary=f"批量重测 SSH：正常 {len(results) - failed} 台，异常 {failed} 台")
        return jsonify(results=results, ok_count=len(results) - failed, attention_count=failed)

    @app.post("/api/hosts/batch-tags")
    @login_required(permission="host.manage", write=True)
    def batch_host_tags():
        payload = body()
        host_ids = payload.get("host_ids")
        add, remove = payload.get("add", []), payload.get("remove", [])
        if not isinstance(host_ids, list) or not isinstance(add, list) or not isinstance(remove, list):
            raise ValueError("host_ids、add 和 remove 必须是数组")
        results = []
        for raw_id in host_ids:
            try:
                host_id = int(raw_id)
                updated = hosts.update_tags(host_id, add, remove)
                results.append({"host_id": host_id, "success": True, "tags": updated["tags"]})
            except Exception as exc:
                results.append({"host_id": raw_id, "success": False, "error": str(exc)})
        audit_action("host_tags_batch_updated", target_type="host", summary=f"批量修改 {len(host_ids)} 台主机标签")
        return jsonify(results=results)

    @app.get("/api/hosts/<int:host_id>")
    @login_required(permission="page.hosts")
    def get_host(host_id: int):
        host = hosts.get(host_id)
        gpu_runtime = [dict(row) for row in database.query_all("SELECT * FROM gpu_runtime WHERE host_id=? ORDER BY gpu_uuid", (host_id,))]
        settings = config.all()
        return jsonify(
            host=host,
            latest=hosts.latest(host_id),
            gpu_runtime=gpu_runtime,
            mount_thresholds=hosts.mount_thresholds(host_id),
            thresholds={"filesystem_usage": settings["filesystem_usage_threshold"], "filesystem_inode": settings["filesystem_inode_threshold"]},
            retention_days=settings["metric_retention_days"],
        )

    @app.put("/api/hosts/<int:host_id>/mount-thresholds")
    @login_required(permission="host.manage", write=True)
    def replace_mount_thresholds(host_id: int):
        result = hosts.replace_mount_thresholds(host_id, body().get("rules"))
        audit_action("mount_thresholds_updated", target_type="host", target_id=host_id, summary=f"更新 {len(result)} 个挂载点告警覆盖规则")
        return jsonify(items=result)

    @app.patch("/api/hosts/<int:host_id>")
    @login_required(permission="host.manage", write=True)
    def update_host(host_id: int):
        payload = body()
        confirmed_fingerprint = payload.pop("confirmed_fingerprint", None)
        confirmed_physical_replacement = payload.pop("confirmed_physical_replacement", False)
        if not isinstance(confirmed_physical_replacement, bool):
            raise ValueError("物理节点替换确认值必须是布尔值")
        connection_fields = {"address", "port", "username", "auth_type", "auth_secret", "private_key", "private_key_passphrase"}
        sensitive = connection_fields | {"sudo_password", "schedule_command", "confirmed_fingerprint", "confirmed_physical_replacement"}
        if (sensitive & set(payload) or confirmed_fingerprint or confirmed_physical_replacement) and not auth.is_elevated(g.user):
            return jsonify(error="修改连接信息、凭据、SSH 指纹或调度命令需要重新验证当前密码", requires_elevation=True), 403
        payload.pop("identity", None)
        try:
            identity = connection_test(payload, host_id, confirmed_fingerprint) if connection_fields & set(payload) else None
        except SSHFingerprintError as error:
            audit_action("host_fingerprint_mismatch", target_type="host", target_id=host_id, success=False, summary="保存主机时发现 SSH 指纹变化", error=str(error))
            return fingerprint_mismatch_response(error)
        if identity and identity.get("duplicate"):
            raise ValueError(f"该物理主机已被纳管: {identity['duplicate']['name']}")
        if identity and identity["machine_id_changed"] and not confirmed_physical_replacement:
            return jsonify(
                error="远端 machine-id 也已变化，可能是另一台服务器。请确认沿用当前记录，或删除旧记录后重新添加",
                physical_identity_changed=True,
            ), 409
        if background and (connection_fields & set(payload) or payload.get("enabled") is False):
            reason = "主机连接配置已修改" if connection_fields & set(payload) else "主机采集已禁用"
            background.cancel_host(host_id, reason)
        host = hosts.update(host_id, payload, fingerprint=identity.get("fingerprint") if identity else None, machine_id=identity.get("machine_id") if identity else None)
        if identity and identity["fingerprint_changed"]:
            audit_action("host_fingerprint_updated", target_type="host", target_id=host_id, summary="管理员确认并更新 SSH 主机指纹")
        audit_action("host_updated", target_type="host", target_id=host_id, summary=f"修改主机 {host['name']}")
        return jsonify(host=host)

    @app.delete("/api/hosts/<int:host_id>")
    @login_required(permission="host.manage", write=True, elevated=True)
    def delete_host(host_id: int):
        host = hosts.get(host_id)
        if background:
            background.cancel_host(host_id, "主机已删除")
        hosts.soft_delete(host_id)
        audit_action("host_deleted", target_type="host", target_id=host_id, summary=f"软删除主机 {host['name']}")
        return jsonify(ok=True)

    @app.post("/api/hosts/<int:host_id>/refresh")
    @login_required(permission="host.refresh", write=True)
    def refresh_host(host_id: int):
        current_host = hosts.get(host_id)
        if not current_host["enabled"]:
            raise ServiceError("该主机已禁用采集")
        if background:
            task_id = background.submit_collection(host_id, manual=True, user_id=g.user["id"])
        else:
            task_id = str(uuid.uuid4())
            host = hosts.get(host_id, include_secrets=True)
            result = Collector(secret_box, config.all()).collect(host, (hosts.latest(host_id) or {}).get("data"))
            outcome = hosts.ingest_collection(host_id, result)
            database.execute(
                "INSERT INTO tasks(id,task_type,host_id,state,result_json,error,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, "collection", host_id, "success" if result.core_ok else "failed", json_dump(compact_collection_result(outcome)), result.error, utc_iso(), utc_iso()),
            )
        return jsonify(task_id=task_id), 202

    @app.get("/api/tasks/<task_id>")
    @login_required(permission="page.jobs")
    def get_task(task_id: str):
        row = database.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not row:
            return jsonify(error="任务不存在"), 404
        result = dict(row)
        result["result"] = json_load(result.pop("result_json"), None)
        return jsonify(task=result)

    @app.get("/api/dashboard")
    @login_required(permission="page.dashboard")
    def dashboard():
        items = []
        for host in hosts.list(search=request.args.get("search"), status=request.args.get("status")):
            items.append({"host": host, "latest": hosts.latest(host["id"])})
        return jsonify(items=items, settings={key: value for key, value in config.all().items() if key in {"frontend_refresh_interval", "green_threshold", "yellow_threshold", "gpu_util_threshold", "gpu_memory_threshold", "filesystem_usage_threshold", "filesystem_inode_threshold", "metric_retention_days", "timezone"}})

    @app.get("/api/hosts/<int:host_id>/history")
    @login_required(permission="page.hosts")
    def history(host_id: int):
        hosts.get(host_id)
        start = request.args.get("start") or utc_iso(utc_now() - timedelta(hours=1))
        end = request.args.get("end") or utc_iso()
        start_time, end_time = parse_utc(start), parse_utc(end)
        if not start_time or not end_time or start_time >= end_time:
            raise ValueError("历史时间范围无效")
        span = end_time - start_time
        if span > timedelta(days=config.all()["metric_retention_days"]):
            raise ValueError("请求范围超过历史数据保留期限")
        metric = request.args.get("metric", "cpu_usage")
        return jsonify(items=hosts.history(host_id, metric, request.args.get("object_key", ""), start, end), kind="adaptive")

    @app.get("/api/settings")
    @login_required(permission="page.settings")
    def get_settings():
        values = config.all()
        values["serverchan_sendkey"] = "configured" if values.get("serverchan_sendkey") else ""
        values.update(database.storage_info())
        return jsonify(settings=values)

    @app.get("/api/scan-settings")
    @login_required(permission="storage.scan")
    def get_scan_settings():
        values = config.all()
        return jsonify(settings={key: values[key] for key in (
            "scan_timeout_seconds", "scan_max_depth", "scan_result_limit",
            "scan_minimum_mib", "environment_inventory_timeout",
        )})

    @app.patch("/api/settings")
    @login_required(permission="settings.manage", write=True)
    def update_settings():
        payload = body()
        clear_sendkey = payload.pop("serverchan_sendkey_clear", False)
        if payload.get("serverchan_sendkey") == "configured":
            payload.pop("serverchan_sendkey")
        elif "serverchan_sendkey" in payload and payload["serverchan_sendkey"]:
            payload["serverchan_sendkey"] = secret_box.encrypt(payload["serverchan_sendkey"])
        elif "serverchan_sendkey" in payload:
            payload.pop("serverchan_sendkey")
        if clear_sendkey:
            payload["serverchan_sendkey"] = ""
        values = config.update(payload)
        audit_action("settings_updated", target_type="settings", summary="系统设置已更新")
        values["serverchan_sendkey"] = "configured" if values.get("serverchan_sendkey") else ""
        return jsonify(settings=values)

    @app.patch("/api/profile/theme")
    @login_required(write=True)
    def update_theme():
        theme = body().get("theme")
        if theme not in {"light", "dark", "tech"}:
            raise ValueError("主题无效")
        database.execute("UPDATE users SET theme=?,updated_at=? WHERE id=?", (theme, utc_iso(), g.user["id"]))
        return jsonify(theme=theme)

    @app.get("/api/alerts")
    @login_required(permission="page.alerts")
    def list_alerts():
        filters = {key: request.args.get(key) for key in ("host_id", "alert_type", "state", "severity", "search")}
        filters["include_cleared"] = request.args.get("include_cleared") == "1"
        if filters.get("host_id"):
            try:
                filters["host_id"] = int(filters["host_id"])
            except (TypeError, ValueError) as exc:
                raise ValueError("host_id 必须是整数") from exc
        for key in ("start", "end"):
            if request.args.get(key):
                parsed = parse_utc(request.args[key])
                if not parsed:
                    raise ValueError(f"{key} 时间格式无效")
                filters[key] = utc_iso(parsed)
        result = alerts.list(request.args.get("page", 1), request.args.get("page_size", 20), filters)
        result["toast_enabled"] = config.all()["toast_enabled"]
        return jsonify(result)

    @app.get("/api/alerts/export")
    @login_required(permission="page.alerts")
    def export_alerts():
        filters = {key: request.args.get(key) for key in ("host_id", "alert_type", "state", "severity", "search")}
        filters["include_cleared"] = request.args.get("include_cleared") == "1"
        if filters.get("host_id"):
            try:
                filters["host_id"] = int(filters["host_id"])
            except (TypeError, ValueError) as exc:
                raise ValueError("host_id 必须是整数") from exc
        for key in ("start", "end"):
            if request.args.get(key):
                parsed = parse_utc(request.args[key])
                if not parsed:
                    raise ValueError(f"{key} 时间格式无效")
                filters[key] = utc_iso(parsed)
        rows: list[dict[str, Any]] = []
        page = 1
        while len(rows) < 10000:
            result = alerts.list(page, 200, filters)
            rows.extend(result["items"])
            if page >= result["pages"] or not result["items"]:
                break
            page += 1
        filename, content = export_csv(rows[:10000], "告警事件")
        audit_action("alerts_exported", target_type="alert", summary=f"导出 {len(rows[:10000])} 条告警事件")
        return Response(content, content_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    @app.get("/api/faults")
    @login_required(permission="page.alerts")
    def list_faults():
        host_items = hosts.list()
        active = alerts.list(1, 200, {"state": "active"})["items"]
        by_host: dict[int, list[dict[str, Any]]] = {}
        for item in active:
            if item.get("host_id") is not None:
                by_host.setdefault(int(item["host_id"]), []).append(item)
        fault_items = []
        for host in host_items:
            issues = list(by_host.get(host["id"], []))
            if host.get("status") in {"offline", "fingerprint_error", "degraded", "busy"} and not issues:
                summary = {"offline": "主机离线", "fingerprint_error": "SSH 主机指纹异常", "degraded": "可选指标采集降级", "busy": "主机采集繁忙"}.get(host["status"], host["status"])
                issues.append({"alert_type": host["status"], "severity": "critical" if host["status"] in {"offline", "fingerprint_error"} else "warning", "summary": summary})
            if issues:
                fault_items.append({"host": host, "issues": issues})
        return jsonify(items=fault_items, total=len(fault_items))

    @app.post("/api/alerts/<int:alert_id>/acknowledge")
    @login_required(permission="alerts.manage", write=True)
    def acknowledge_alert(alert_id: int):
        result = alerts.acknowledge(alert_id, g.user["id"])
        audit_action("alert_acknowledged", target_type="alert", target_id=alert_id, summary="确认告警，停止重复提示")
        return jsonify(alert=result)

    @app.delete("/api/alerts/<int:alert_id>")
    @login_required(permission="alerts.manage", write=True)
    def clear_alert(alert_id: int):
        result = alerts.clear(alert_id)
        audit_action("alert_cleared", target_type="alert", target_id=alert_id, summary="软清理告警记录")
        return jsonify(alert=result)

    @app.get("/api/logs")
    @login_required(permission="page.logs")
    def logs():
        page, page_size = clamp_page(request.args.get("page")), clamp_page_size(request.args.get("page_size"))
        clauses, params = ["1=1"], []
        for field in ("action", "username", "success"):
            if request.args.get(field) is not None:
                clauses.append(f"{field}=?")
                params.append(request.args[field])
        search = request.args.get("search", "").strip()
        if search:
            clauses.append("(action LIKE ? OR username LIKE ? OR target_type LIKE ? OR target_id LIKE ? OR summary LIKE ? OR error LIKE ?)")
            params.extend([f"%{search}%"] * 6)
        total = database.query_one("SELECT COUNT(*) count FROM audit_logs WHERE " + " AND ".join(clauses), params)["count"]
        rows = database.query_all("SELECT * FROM audit_logs WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size])
        return jsonify(paged(total, page, page_size, [dict(row) for row in rows]))

    @app.get("/api/logs/export")
    @login_required(permission="logs.export")
    def export_logs():
        rows = [dict(row) for row in database.query_all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 10000")]
        filename, content = export_csv(rows, "操作日志")
        audit_action("logs_exported", target_type="audit_log", summary="导出操作日志")
        return Response(content, content_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})

    @app.get("/api/hosts/<int:host_id>/gpu/<path:gpu_uuid>")
    @login_required(permission="page.hosts")
    def gpu_config(host_id: int, gpu_uuid: str):
        return jsonify(config=scheduler.get_gpu_config(hosts.get(host_id), gpu_uuid))

    @app.patch("/api/hosts/<int:host_id>/gpu/<path:gpu_uuid>")
    @login_required(permission="gpu.manage", write=True, elevated=True)
    def update_gpu_config(host_id: int, gpu_uuid: str):
        host = hosts.get(host_id)
        result = scheduler.configure_gpu(host, gpu_uuid, body())
        audit_action("gpu_config_updated", target_type="gpu", target_id=gpu_uuid, summary="GPU 调度配置已修改")
        return jsonify(config=result)

    @app.get("/api/schedule-jobs")
    @login_required(permission="page.jobs")
    def schedule_jobs():
        page, page_size = clamp_page(request.args.get("page")), clamp_page_size(request.args.get("page_size"))
        clauses, params = ["1=1"], []
        for field in ("host_id", "state", "mode"):
            if request.args.get(field):
                clauses.append(f"{field}=?")
                params.append(request.args[field])
        if request.args.get("search"):
            clauses.append("(id LIKE ? OR gpu_uuid LIKE ? OR command_summary LIKE ?)")
            params.extend([f"%{request.args['search']}%"] * 3)
        where = " AND ".join(clauses)
        total = database.query_one(f"SELECT COUNT(*) count FROM schedule_jobs WHERE {where}", params)["count"]
        rows = database.query_all(f"SELECT * FROM schedule_jobs WHERE {where} ORDER BY started_at DESC LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size])
        return jsonify(paged(total, page, page_size, [dict(row) for row in rows]))

    @app.get("/api/schedule-jobs/export")
    @login_required(permission="jobs.export")
    def export_schedule_jobs():
        rows = [dict(row) for row in database.query_all("SELECT * FROM schedule_jobs ORDER BY started_at DESC LIMIT 10000")]
        filename, content = export_csv(rows, "GPU调度记录")
        audit_action("schedule_jobs_exported", target_type="schedule_job", summary="导出 GPU 调度记录")
        return Response(content, content_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})

    @app.get("/api/hosts/<int:host_id>/tmux")
    @login_required(permission="tmux.view")
    def list_tmux(host_id: int):
        return jsonify(items=operations.tmux_sessions(operation_host(host_id, "allow_tmux", " Tmux 操作")))

    @app.get("/api/hosts/<int:host_id>/tmux/<path:name>/snapshot")
    @login_required(permission="tmux.view")
    def tmux_snapshot(host_id: int, name: str):
        return jsonify(snapshot=operations.tmux_snapshot(operation_host(host_id, "allow_tmux", " Tmux 操作"), name))

    @app.post("/api/hosts/<int:host_id>/tmux")
    @login_required(permission="tmux.manage", write=True)
    def create_tmux(host_id: int):
        name = str(body().get("name", ""))
        operations.tmux_create(operation_host(host_id, "allow_tmux", " Tmux 操作"), name)
        audit_action("tmux_created", target_type="host", target_id=host_id, summary=f"创建 Tmux 会话 {name}")
        return jsonify(ok=True), 201

    @app.patch("/api/hosts/<int:host_id>/tmux/<path:name>")
    @login_required(permission="tmux.manage", write=True)
    def rename_tmux(host_id: int, name: str):
        new_name = str(body().get("name", ""))
        operations.tmux_rename(operation_host(host_id, "allow_tmux", " Tmux 操作"), name, new_name)
        audit_action("tmux_renamed", target_type="host", target_id=host_id, summary=f"重命名 Tmux 会话 {name}")
        return jsonify(ok=True)

    @app.delete("/api/hosts/<int:host_id>/tmux/<path:name>")
    @login_required(permission="tmux.manage", write=True, elevated=True)
    def delete_tmux(host_id: int, name: str):
        operations.tmux_kill(operation_host(host_id, "allow_tmux", " Tmux 操作"), name)
        audit_action("tmux_deleted", target_type="host", target_id=host_id, summary=f"删除 Tmux 会话 {name}")
        return jsonify(ok=True)

    @app.get("/api/hosts/<int:host_id>/processes")
    @login_required(permission="process.view")
    def processes(host_id: int):
        return jsonify(items=operations.processes(operation_host(host_id, "allow_process", "进程操作"), request.args.get("hide_kernel", "1") != "0"))

    @app.post("/api/hosts/<int:host_id>/processes/<int:pid>/terminate")
    @login_required(permission="process.terminate", write=True, elevated=True)
    def terminate_process(host_id: int, pid: int):
        payload = body()
        operations.terminate_process(operation_host(host_id, "allow_process", "进程操作"), pid, str(payload.get("started", "")), str(payload.get("signal", "TERM")))
        audit_action("process_terminated", target_type="process", target_id=pid, summary=f"发送 SIG{payload.get('signal', 'TERM')} 到进程")
        return jsonify(ok=True)

    @app.get("/api/hosts/<int:host_id>/tools")
    @login_required(permission="tools.view")
    def detect_tools(host_id: int):
        return jsonify(tools=operations.detect_tools(hosts.get(host_id, include_secrets=True)))

    @app.get("/api/development/hosts")
    @login_required(permission="page.environments")
    def development_hosts():
        items = hosts.list()
        return jsonify(items=[{
            "id": item["id"], "name": item["name"], "address": item["address"],
            "username": item["username"], "status": item.get("status"),
            "allow_install": item.get("allow_install", False),
        } for item in items])

    @app.get("/api/hosts/<int:host_id>/development/stack")
    @login_required(permission="development.view")
    def development_stack(host_id: int):
        return jsonify(stack=development.development_stack(hosts.get(host_id, include_secrets=True)))

    @app.get("/api/hosts/<int:host_id>/development/gpu-diagnostics")
    @login_required(permission="diagnostics.view")
    def gpu_diagnostics(host_id: int):
        return jsonify(diagnostics=development.gpu_diagnostics(hosts.get(host_id, include_secrets=True)))

    @app.get("/api/hosts/<int:host_id>/development/environments")
    @login_required(permission="development.view")
    def environment_inventory(host_id: int):
        host = hosts.get(host_id, include_secrets=True)
        root = request.args.get("root") or f"/home/{host['username']}"
        return jsonify(development.environment_inventory(host, root))

    @app.post("/api/hosts/<int:host_id>/development/environment-plan")
    @login_required(permission="development.plan", write=True)
    def environment_plan(host_id: int):
        plan = development.environment_plan(operation_host(host_id, "allow_install", "开发环境管理"), body())
        audit_action("environment_plan_generated", target_type="host", target_id=host_id, summary=f"生成虚拟环境 {plan['backend']} {plan['action']} 方案")
        return jsonify(plan=plan)

    @app.post("/api/hosts/<int:host_id>/development/environment-execute")
    @login_required(permission="development.execute", write=True, elevated=True)
    def execute_environment_plan(host_id: int):
        result = development.execute_environment_plan(operation_host(host_id, "allow_install", "开发环境管理"), body())
        audit_action(
            "environment_plan_executed", target_type="host", target_id=host_id, success=result["ok"],
            summary=f"网页执行虚拟环境 {result['plan']['backend']} {result['plan']['action']} 方案",
            error=None if result["ok"] else result["stderr"][:500],
        )
        return jsonify(result), 200 if result["ok"] else 409

    @app.get("/api/hosts/<int:host_id>/development/conda-export")
    @login_required(permission="development.view")
    def export_conda_environment(host_id: int):
        path = request.args.get("path", "")
        content = development.export_conda_environment(hosts.get(host_id, include_secrets=True), path)
        audit_action("conda_environment_exported", target_type="host", target_id=host_id, summary=f"导出 conda 环境 {path}")
        return Response(content, content_type="text/yaml; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''conda-environment-{host_id}.yml"})

    @app.post("/api/hosts/<int:host_id>/development/conda-yaml-plan")
    @login_required(permission="development.plan", write=True)
    def conda_yaml_plan(host_id: int):
        plan = development.conda_yaml_plan(operation_host(host_id, "allow_install", "开发环境管理"), body())
        audit_action("conda_yaml_plan_generated", target_type="host", target_id=host_id, summary=f"生成 conda YAML 重建方案 {plan['path']}")
        return jsonify(plan=plan)

    @app.post("/api/hosts/<int:host_id>/development/conda-yaml-execute")
    @login_required(permission="development.execute", write=True, elevated=True)
    def execute_conda_yaml_plan(host_id: int):
        result = development.execute_conda_yaml_plan(operation_host(host_id, "allow_install", "开发环境管理"), body())
        audit_action("conda_yaml_plan_executed", target_type="host", target_id=host_id, success=result["ok"], summary=f"网页执行 conda YAML 重建 {result['plan']['path']}")
        return jsonify(result), 200 if result["ok"] else 409

    @app.post("/api/hosts/<int:host_id>/development/system-plan")
    @login_required(permission="development.plan", write=True)
    def system_plan(host_id: int):
        payload = body()
        if payload.get("kind") == "apt" and not permission_service.allowed(g.user, "apt.plan"):
            return jsonify(error="当前账户未获得 APT 方案权限", permission="apt.plan"), 403
        plan = development.system_plan(operation_host(host_id, "allow_install", "开发环境管理"), payload)
        audit_action("system_plan_generated", target_type="host", target_id=host_id, summary=f"生成 {plan['title']} 脚本")
        return jsonify(plan=plan)

    @app.get("/api/hosts/<int:host_id>/development/apt-packages")
    @login_required(permission="development.view")
    def apt_packages(host_id: int):
        return jsonify(items=development.apt_packages(hosts.get(host_id, include_secrets=True), request.args.get("search", "")))

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

    @app.post("/api/hosts/<int:host_id>/tools/<tool>/install")
    @login_required(permission="tools.install", write=True, elevated=True)
    def install_tool(host_id: int, tool: str):
        command = operations.install_tool(operation_host(host_id, "allow_install", "工具安装"), tool)
        audit_action("tool_installed", target_type="host", target_id=host_id, summary=f"安装工具 {tool}: {command}")
        return jsonify(ok=True)

    @app.get("/api/hosts/<int:host_id>/tools/<tool>/install-plan")
    @login_required(permission="tools.install")
    def install_tool_plan(host_id: int, tool: str):
        host = operation_host(host_id, "allow_install", "工具安装")
        return jsonify(command=operations.installation_command(host, tool), tool=tool, sudo_password_configured=bool(host.get("sudo_password")))

    @app.post("/api/hosts/<int:host_id>/stress")
    @login_required(permission="stress.manage", write=True, elevated=True)
    def start_stress(host_id: int):
        payload = body()
        task_id = operations.start_stress(operation_host(host_id, "allow_stress", "压力测试"), int(payload.get("cpu_workers", 0)), int(payload.get("memory_workers", 0)), int(payload.get("memory_percent", 50)), int(payload.get("duration_minutes", 1)))
        audit_action("stress_started", target_type="host", target_id=host_id, summary=f"启动压力测试 {task_id}")
        return jsonify(task_id=task_id), 201

    @app.post("/api/hosts/<int:host_id>/stress/<task_id>/stop")
    @login_required(permission="stress.manage", write=True)
    def stop_stress(host_id: int, task_id: str):
        operations.stop_stress(operation_host(host_id, "allow_stress", "压力测试"), task_id)
        audit_action("stress_stopped", target_type="host", target_id=host_id, summary=f"停止压力测试 {task_id}")
        return jsonify(ok=True)

    @app.get("/api/hosts/<int:host_id>/stress/<task_id>")
    @login_required(permission="stress.view")
    def stress_status(host_id: int, task_id: str):
        return jsonify(task=operations.stress_status(operation_host(host_id, "allow_stress", "压力测试"), task_id))

    @app.post("/api/backups")
    @login_required(permission="backup.create", write=True)
    def create_backup():
        settings = config.all()
        path = backups.create(settings["backup_dir"], settings["backup_keep"])
        audit_action("backup_created", target_type="backup", target_id=str(path), summary="数据库备份成功")
        return jsonify(path=str(path)), 201

    @app.post("/api/maintenance/compact")
    @login_required(admin=True, write=True, elevated=True)
    def compact_database():
        settings = config.all()
        history_service = HistoryService(database)
        aggregate = history_service.aggregate(
            mid_seconds=settings["aggregation_mid_seconds"],
            long_seconds=settings["aggregation_long_seconds"],
            raw_retention_minutes=settings["metric_raw_retention_minutes"],
            mid_retention_hours=settings["metric_mid_retention_hours"],
        )
        cleanup = history_service.cleanup(
            metric_retention_days=settings["metric_retention_days"],
            log_retention_days=settings["log_retention_days"],
            collection_task_retention_minutes=settings["collection_task_retention_minutes"],
        )
        result = database.compact()
        audit_action(
            "database_compacted",
            target_type="database",
            summary=f"清理并压缩数据库，回收 {result['reclaimed_bytes']} 字节",
        )
        return jsonify(aggregate=aggregate, cleanup=cleanup, **result)

    @app.get("/api/file-manager/hosts")
    @login_required(permission="files.browse")
    def file_manager_hosts():
        items = hosts.list()
        return jsonify(items=[{"id": item["id"], "name": item["name"], "address": item["address"], "username": item["username"], "status": item.get("status")} for item in items])

    def file_host(host_id: int) -> dict[str, Any]:
        return hosts.get(host_id, include_secrets=True)

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

    sock = Sock(app)

    def operation_host(host_id: int, capability: str, label: str) -> dict[str, Any]:
        host = hosts.get(host_id, include_secrets=True)
        if not host.get(capability, False):
            raise ServiceError(f"该主机未允许{label}")
        return host

    interactive_lock = threading.RLock()
    interactive_total = 0
    interactive_by_user_host: dict[tuple[int, int], int] = {}

    def interactive_acquire(user_id: int, host_id: int) -> bool:
        nonlocal interactive_total
        key = (user_id, host_id)
        with interactive_lock:
            if interactive_total >= config.all()["interactive_ssh_limit"] or interactive_by_user_host.get(key, 0) >= 2:
                return False
            interactive_total += 1
            interactive_by_user_host[key] = interactive_by_user_host.get(key, 0) + 1
            return True

    def interactive_release(user_id: int, host_id: int) -> None:
        nonlocal interactive_total
        key = (user_id, host_id)
        with interactive_lock:
            interactive_total = max(0, interactive_total - 1)
            if interactive_by_user_host.get(key, 0) <= 1:
                interactive_by_user_host.pop(key, None)
            else:
                interactive_by_user_host[key] -= 1

    def interactive_socket(ws: Any, host_id: int, tmux_name: str | None = None) -> None:
        user = permission_service.decorate(auth.authenticate(request.cookies.get(COOKIE_NAME), touch=False))
        origin = request.headers.get("Origin")
        allowed_origins = {request.host_url.rstrip("/"), request.url_root.rstrip("/")}
        interactive_permission = "tmux.view" if tmux_name else "terminal.open"
        if not user or not permission_service.allowed(user, interactive_permission) or not AuthService.is_elevated(user) or (origin and origin not in allowed_origins):
            ws.close(reason=1008, message="unauthorized")
            return
        host = hosts.get(host_id, include_secrets=True)
        if tmux_name and not host["allow_tmux"]:
            ws.close(reason=1008, message="tmux disabled")
            return
        if not tmux_name and not host["allow_terminal"]:
            ws.close(reason=1008, message="terminal disabled")
            return
        if not interactive_acquire(user["id"], host_id):
            ws.close(reason=1013, message="interactive ssh limit")
            return
        client = SSHClient(host, secret_box, config.all())
        session_id = str(uuid.uuid4())
        channel = None
        output_queue: queue.Queue[bytes] = queue.Queue()
        output_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        output_size = 0
        output_lock = threading.Lock()
        overflow = threading.Event()
        stopped = threading.Event()
        last_activity = time.monotonic()
        reason = "client_closed"

        def read_output() -> None:
            nonlocal output_size
            try:
                while not stopped.is_set() and channel and not channel.closed:
                    if not channel.recv_ready():
                        time.sleep(0.01)
                        continue
                    chunk = channel.recv(65536)
                    with output_lock:
                        if output_size + len(chunk) > 1024 * 1024:
                            overflow.set()
                            return
                        output_size += len(chunk)
                    output_queue.put(chunk)
            except Exception:
                return

        reader = None
        try:
            channel = client.open_shell()
            if tmux_name:
                # Tmux uses the attaching client's locale to decide whether wide UTF-8
                # characters are supported. POSIX shells commonly lack a locale export.
                channel.send(_tmux_attach_command(tmux_name).encode("utf-8"))
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            audit.write("tmux_attached" if tmux_name else "terminal_connected", actor=user, source_ip=request.remote_addr, target_type="host", target_id=host_id, success=True, summary=f"交互会话 {session_id}")
            while True:
                current = permission_service.decorate(auth.authenticate(request.cookies.get(COOKIE_NAME), touch=False))
                if not current or not permission_service.allowed(current, interactive_permission):
                    reason = "session_invalidated"
                    break
                if overflow.is_set():
                    reason = "output_buffer_limit"
                    break
                if time.monotonic() - last_activity > config.all()["terminal_idle_seconds"]:
                    reason = "idle_timeout"
                    break
                try:
                    chunk = output_queue.get_nowait()
                except queue.Empty:
                    chunk = None
                if chunk is not None:
                    decoded = output_decoder.decode(chunk)
                    if decoded:
                        ws.send(decoded)
                    with output_lock:
                        output_size = max(0, output_size - len(chunk))
                    last_activity = time.monotonic()
                try:
                    message = ws.receive(timeout=0.05)
                except (TimeoutError, queue.Empty):
                    message = None
                if message:
                    last_activity = time.monotonic()
                    try:
                        parsed = json.loads(message)
                    except (TypeError, json.JSONDecodeError):
                        parsed = {"type": "input", "data": message}
                    if parsed.get("type") == "resize":
                        channel.resize_pty(width=max(20, min(500, int(parsed.get("cols", 120)))), height=max(5, min(200, int(parsed.get("rows", 32)))))
                    elif parsed.get("type") == "input":
                        channel.send(str(parsed.get("data", "")).encode("utf-8"))
                if channel.closed or channel.exit_status_ready():
                    reason = "remote_closed"
                    break
        except Exception as exc:
            reason = f"error:{type(exc).__name__}"
        finally:
            stopped.set()
            if channel:
                channel.close()
            client.close()
            interactive_release(user["id"], host_id)
            audit.write("tmux_detached" if tmux_name else "terminal_disconnected", actor=user, source_ip=request.remote_addr, target_type="host", target_id=host_id, success=True, summary=f"交互会话 {session_id} 断开: {reason}")
            try:
                ws.close(reason=1000, message=reason[:123])
            except Exception:
                pass

    @sock.route("/ws/terminal/<int:host_id>")
    def terminal(ws: Any, host_id: int) -> None:
        interactive_socket(ws, host_id)

    @sock.route("/ws/tmux/<int:host_id>/<path:tmux_name>")
    def tmux_terminal(ws: Any, host_id: int, tmux_name: str) -> None:
        interactive_socket(ws, host_id, tmux_name)

    return app

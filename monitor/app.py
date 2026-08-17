from __future__ import annotations

import atexit
from datetime import timedelta
import json
import secrets
import logging
import os
import shutil
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from flask import Flask, Response, g, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

from .audit import AuditService
from .auth import AuthError, AuthService, LoginLocked
from .background import BackgroundService
from .collector import Collector
from .config import ConfigError, ConfigStore, validate_settings
from .db import Database
from .development import DevelopmentService
from .files import SFTPFileService
from .gpu_scheduler import GPUScheduler
from .logging_config import configure_logging
from .operations import OperationError, OperationService
from .notifications import NotificationService
from .permissions import PermissionService
from .security import PasswordService, ProcessLock, SecretBox, redact
from .routes import (
    register_development_routes,
    register_file_routes,
    register_operation_routes,
    register_socket_routes,
)
from .routes.sockets import _tmux_attach_command
from .services import (
    AlertService,
    BackupService,
    HostService,
    ServiceError,
    compact_collection_result,
    export_csv,
    gpu_user_usage,
    hardware_asset_rows,
    host_transfer_rows,
    parse_host_import,
)
from .ssh_client import SSHClient, SSHConnectionPool, SSHError, SSHFingerprintError
from .utils import clamp_page, clamp_page_size, json_dump, json_load, paged, parse_utc, utc_iso, utc_now
from .web import COOKIE_NAME, WebContext


LOGGER = logging.getLogger("server_monitor")


def _mask_apprise_url(value: str) -> str:
    """Hide credentials and token-like query values while keeping URLs recognizable."""
    if not isinstance(value, str) or value.startswith("enc:"):
        return "已配置的通知 URL"
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"ntfy", "ntfys"}:
            return f"{parsed.scheme or 'apprise'}://***"
        netloc = parsed.netloc
        if parsed.username is not None or parsed.password is not None:
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parsed.port:
                host = f"{host}:{parsed.port}"
            user = parsed.username or ""
            netloc = f"{user}:***@{host}" if parsed.password is not None else f"{user}@{host}"
        sensitive = {"token", "key", "password", "pass", "secret", "apikey", "api_key", "access_token", "auth"}
        query = urlencode([(key, "***" if key.lower() in sensitive else item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)])
        fragment = "***" if parsed.fragment else ""
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))
    except (TypeError, ValueError):
        return "已配置的通知 URL"


def _resolve_apprise_markers(values: Any, current: list[str]) -> list[str]:
    if not isinstance(values, list):
        raise ConfigError("apprise_urls 必须是 URL 数组")
    resolved: list[str] = []
    for value in values:
        if isinstance(value, str) and value.startswith("configured:"):
            try:
                index = int(value.partition(":")[2])
            except ValueError as exc:
                raise ConfigError("通知 URL 配置引用无效") from exc
            if index < 0 or index >= len(current):
                raise ConfigError("通知 URL 配置引用已失效，请重新加载设置")
            value = current[index]
        resolved.append(value)
    return resolved


def _encode_apprise_urls(values: Any, current: list[str], secret_box: SecretBox) -> list[str]:
    resolved = _resolve_apprise_markers(values, current)
    cleaned = validate_settings({"apprise_urls": resolved})["apprise_urls"]
    encoded: list[str] = []
    for value in cleaned:
        encoded.append(value if value.startswith("enc:") else f"enc:{secret_box.encrypt(value)}")
    return encoded


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    configure_logging()
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
    config.remove_legacy_notification_settings()
    config.migrate_alert_defaults()
    ssh_pool = SSHConnectionPool(secret_box, config.all)
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
    operations = OperationService(secret_box, config, database, ssh_pool)
    development = DevelopmentService(operations, config)
    files = SFTPFileService(secret_box, config.all(), app.config["FILE_TRANSFER_LIMIT"], ssh_pool)
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
        ssh_pool=ssh_pool,
    )
    web_context = WebContext(
        app=app,
        database=database,
        secret_box=secret_box,
        config=config,
        auth=auth,
        audit=audit,
        hosts=hosts,
        operations=operations,
        development=development,
        backups=backups,
        permission_service=permission_service,
        files=files,
    )
    app.extensions["web_context"] = web_context

    background = None
    if process_lock:
        app.extensions["process_lock"] = process_lock
    if app.config["START_BACKGROUND"]:
        background = BackgroundService(hosts, config, secret_box, database, scheduler, alerts, audit, backups, ssh_pool)
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
        ssh_pool.close()
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

    login_required = web_context.login_required
    body = web_context.body
    source_ip = web_context.source_ip
    audit_action = web_context.audit_action
    diff_changes = web_context.diff_changes

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
        reachable = sum(1 for item in managed if item.get("status") in {"online", "busy", "degraded", "gpu_error"})
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

    @app.patch("/api/profile/local-overview")
    @login_required(permission="page.dashboard", write=True)
    def update_local_overview():
        enabled = body().get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        database.execute("UPDATE users SET show_local_overview=?,updated_at=? WHERE id=?", (int(enabled), utc_iso(), g.user["id"]))
        audit_action(
            "profile_local_overview_updated",
            target_type="user",
            target_id=g.user["id"],
            summary="显示本机 SSH 概览" if enabled else "隐藏本机 SSH 概览",
            changes={"show_local_overview": {"before": bool(g.user.get("show_local_overview")), "after": enabled}},
        )
        return jsonify(enabled=enabled)

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
            "tags": ["GPU", "测试"], "notes": "示例主机",
            "asset_location": "A 机房 / A03 机柜", "asset_owner": "运维负责人",
            "warranty_expires": "2028-12-31", "enabled": True,
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

    @app.get("/api/hosts/fingerprints")
    @login_required(permission="page.hosts")
    def list_host_fingerprints():
        return jsonify(items=[{
            "host_id": host["id"], "name": host["name"], "address": host["address"],
            "fingerprint": host.get("fingerprint"), "status": host.get("status"),
            "last_error": host.get("last_error"), "last_success_at": host.get("last_success_at"),
        } for host in hosts.list()])

    def confirm_host_fingerprint(host_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        observed = payload.get("observed")
        if not observed:
            raise ValueError("必须提交重测结果中的 observed 指纹")
        identity = connection_test({}, host_id, str(observed))
        if identity.get("duplicate"):
            raise ValueError(f"该 SSH 身份已由主机 {identity['duplicate']['name']} 管理")
        if identity.get("machine_id_changed") and payload.get("confirm_physical_replacement") is not True:
            raise ValueError("machine-id 同时变化，必须逐台确认物理节点替换")
        before = hosts.get(host_id)
        updated = hosts.update(host_id, {}, fingerprint=identity["fingerprint"], machine_id=identity.get("machine_id"))
        hosts.status(host_id, "unknown", error="SSH 指纹已更新，等待下一次采集", error_code=None)
        audit_action(
            "host_fingerprint_updated",
            target_type="host",
            target_id=host_id,
            summary="管理员重新连接并确认 SSH 主机指纹",
            changes={"fingerprint": {"before": before.get("fingerprint"), "after": updated.get("fingerprint")}},
        )
        return {"host_id": host_id, "name": updated["name"], "success": True, "fingerprint": updated["fingerprint"]}

    @app.post("/api/hosts/<int:host_id>/fingerprint/confirm")
    @login_required(permission="host.manage", write=True, elevated=True)
    def confirm_fingerprint(host_id: int):
        return jsonify(confirm_host_fingerprint(host_id, body()))

    @app.post("/api/hosts/fingerprints/confirm")
    @login_required(permission="host.manage", write=True, elevated=True)
    def confirm_fingerprints():
        items = body().get("items")
        if not isinstance(items, list) or not items or len(items) > 20:
            raise ValueError("items 必须是 1～20 条指纹确认记录")
        results = []
        for item in items:
            try:
                if not isinstance(item, dict):
                    raise ValueError("指纹确认记录必须是对象")
                results.append(confirm_host_fingerprint(int(item.get("host_id")), item))
            except Exception as exc:
                results.append({"host_id": item.get("host_id") if isinstance(item, dict) else None, "success": False, "error": redact(str(exc))})
        return jsonify(results=results, success_count=sum(1 for item in results if item["success"]), failure_count=sum(1 for item in results if not item["success"]))

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
        before_host = hosts.get(host_id)
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
            identity = connection_test(payload, host_id, confirmed_fingerprint) if connection_fields & set(payload) or confirmed_fingerprint else None
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
        audit_action(
            "host_updated",
            target_type="host",
            target_id=host_id,
            summary=f"修改主机 {host['name']}",
            changes=diff_changes(before_host, host, ignored={"auth_secret", "private_key", "private_key_passphrase", "sudo_password"}),
        )
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
            result = Collector(secret_box, config.all(), ssh_pool).collect(host, (hosts.latest(host_id) or {}).get("data"))
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
        all_items = []
        for host in hosts.list(search=request.args.get("search"), status=request.args.get("status")):
            all_items.append({"host": host, "latest": hosts.latest(host["id"])})
        show_local = bool(g.user.get("show_local_overview"))
        local_item = next((item for item in all_items if item["host"].get("is_local")), None)
        items = all_items if show_local else [item for item in all_items if not item["host"].get("is_local")]
        return jsonify(
            items=items,
            gpu_users=gpu_user_usage(items),
            local_configured=local_item is not None,
            local_host_id=local_item["host"]["id"] if local_item else None,
            show_local_overview=show_local,
            settings={key: value for key, value in config.all().items() if key in {"frontend_refresh_interval", "green_threshold", "yellow_threshold", "gpu_util_threshold", "gpu_memory_threshold", "filesystem_usage_threshold", "filesystem_inode_threshold", "swap_usage_threshold", "metric_retention_days", "timezone"}},
        )

    @app.get("/api/gpu-usage/users")
    @login_required(permission="page.dashboard")
    def gpu_usage_users():
        items = [{"host": host, "latest": hosts.latest(host["id"])} for host in hosts.list()]
        users = gpu_user_usage(items)
        username = request.args.get("username", "").strip()
        if username:
            users = [item for item in users if item["username"] == username]
        return jsonify(items=users)

    @app.get("/api/hardware-assets")
    @login_required(permission="page.hosts")
    def hardware_assets():
        items = [{"host": host, "latest": hosts.latest(host["id"])} for host in hosts.list()]
        return jsonify(items=hardware_asset_rows(items))

    @app.get("/api/hardware-assets/export")
    @login_required(permission="host.manage")
    def export_hardware_assets():
        items = [{"host": host, "latest": hosts.latest(host["id"])} for host in hosts.list()]
        rows = hardware_asset_rows(items)
        filename, content = export_csv(rows, "硬件资产清单")
        audit_action("hardware_assets_exported", target_type="host", summary=f"导出 {len(rows)} 台主机硬件资产")
        return Response(content, content_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    @app.get("/api/snapshots/current")
    @login_required(permission="page.hosts")
    def current_snapshot():
        snapshot = {
            "generated_at": utc_iso(),
            "schema": 6,
            "hosts": [{"host": host, "latest": hosts.latest(host["id"])} for host in hosts.list()],
        }
        content = json.dumps(snapshot, ensure_ascii=False, indent=2)
        audit_action("current_snapshot_exported", target_type="snapshot", summary=f"导出 {len(snapshot['hosts'])} 台主机当前快照")
        filename = f"server-monitor-snapshot-{utc_now().strftime('%Y%m%d_%H%M%S')}.json"
        return Response(content, content_type="application/json; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    @app.get("/api/saved-views")
    @login_required()
    def saved_views():
        page_name = request.args.get("page", "dashboard")
        if page_name not in {"dashboard", "hosts"}:
            raise ValueError("快捷视图页面无效")
        rows = database.query_all("SELECT id,page,name,filters_json,created_at,updated_at FROM saved_views WHERE user_id=? AND page=? ORDER BY name COLLATE NOCASE", (g.user["id"], page_name))
        return jsonify(items=[{key: value for key, value in {**dict(row), "filters": json_load(row["filters_json"], {})}.items() if key != "filters_json"} for row in rows])

    @app.post("/api/saved-views")
    @login_required(write=True)
    def create_saved_view():
        payload = body()
        page_name = payload.get("page", "dashboard")
        name = str(payload.get("name", "")).strip()
        filters = payload.get("filters")
        if page_name not in {"dashboard", "hosts"} or not name or len(name) > 64 or not isinstance(filters, dict):
            raise ValueError("快捷视图名称、页面或筛选条件无效")
        allowed = {"search", "status", "tags", "gpu_user"} if page_name == "dashboard" else {"search", "status", "sort"}
        if set(filters) - allowed or any(not isinstance(value, (str, list)) for value in filters.values()):
            raise ValueError("快捷视图包含不支持的筛选条件")
        if isinstance(filters.get("tags"), list) and (len(filters["tags"]) > 50 or any(not isinstance(tag, str) or len(tag) > 64 for tag in filters["tags"])):
            raise ValueError("快捷视图标签无效")
        count = database.query_one("SELECT COUNT(*) count FROM saved_views WHERE user_id=? AND page=?", (g.user["id"], page_name))["count"]
        existing = database.query_one("SELECT id FROM saved_views WHERE user_id=? AND page=? AND name=?", (g.user["id"], page_name, name))
        if count >= 20 and not existing:
            raise ValueError("每个页面最多保存 20 个快捷视图")
        now = utc_iso()
        database.execute(
            "INSERT INTO saved_views(user_id,page,name,filters_json,created_at,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user_id,page,name) DO UPDATE SET filters_json=excluded.filters_json,updated_at=excluded.updated_at",
            (g.user["id"], page_name, name, json_dump(filters), now, now),
        )
        audit_action("saved_view_updated", target_type="saved_view", summary=f"保存快捷视图 {name}")
        return jsonify(ok=True), 201

    @app.delete("/api/saved-views/<int:view_id>")
    @login_required(write=True)
    def delete_saved_view(view_id: int):
        row = database.query_one("SELECT id,name FROM saved_views WHERE id=? AND user_id=?", (view_id, g.user["id"]))
        if not row:
            return jsonify(error="快捷视图不存在"), 404
        database.execute("DELETE FROM saved_views WHERE id=? AND user_id=?", (view_id, g.user["id"]))
        audit_action("saved_view_deleted", target_type="saved_view", target_id=view_id, summary=f"删除快捷视图 {row['name']}")
        return jsonify(ok=True)

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
        values["apprise_urls"] = [_mask_apprise_url(value) for value in notifications._urls()]
        values["apprise_available"] = notifications.available
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
        before_settings = config.all()
        if "apprise_urls" in payload:
            payload["apprise_urls"] = _encode_apprise_urls(payload["apprise_urls"], before_settings.get("apprise_urls", []), secret_box)
        values = config.update(payload)
        audit_action(
            "settings_updated",
            target_type="settings",
            summary="系统设置已更新",
            changes=diff_changes(before_settings, values, ignored={"apprise_urls"}),
        )
        values["apprise_urls"] = [_mask_apprise_url(value) for value in notifications._urls()]
        values["apprise_available"] = notifications.available
        return jsonify(settings=values)

    @app.post("/api/notifications/test")
    @login_required(permission="settings.manage", write=True)
    def test_notifications():
        payload = body()
        current = config.all().get("apprise_urls", [])
        if "urls" in payload:
            urls = _resolve_apprise_markers(payload["urls"], current)
            urls = validate_settings({"apprise_urls": urls})["apprise_urls"]
        else:
            value = payload.get("url")
            if value is None:
                urls = None
            elif isinstance(value, str):
                urls = _resolve_apprise_markers([value], current)
                urls = validate_settings({"apprise_urls": urls})["apprise_urls"]
            else:
                raise ValueError("url 必须是字符串")
        return jsonify(notifications.test(urls))

    @app.patch("/api/profile/theme")
    @login_required(write=True)
    def update_theme():
        theme = body().get("theme")
        if theme not in {"light", "dark", "tech"}:
            raise ValueError("主题无效")
        database.execute("UPDATE users SET theme=?,updated_at=? WHERE id=?", (theme, utc_iso(), g.user["id"]))
        return jsonify(theme=theme)

    def parse_alert_filters(values: Any) -> dict[str, Any]:
        filters = {key: values.get(key) for key in ("host_id", "alert_type", "state", "severity", "search")}
        include_cleared = values.get("include_cleared")
        filters["include_cleared"] = include_cleared is True or str(include_cleared or "").lower() in {"1", "true"}
        if filters.get("host_id") not in {None, ""}:
            try:
                filters["host_id"] = int(filters["host_id"])
            except (TypeError, ValueError) as exc:
                raise ValueError("host_id 必须是整数") from exc
        for key in ("start", "end"):
            value = values.get(key)
            if value:
                parsed = parse_utc(value)
                if not parsed:
                    raise ValueError(f"{key} 时间格式无效")
                filters[key] = utc_iso(parsed)
        return filters

    @app.get("/api/alerts")
    @login_required(permission="page.alerts")
    def list_alerts():
        filters = parse_alert_filters(request.args)
        result = alerts.list(request.args.get("page", 1), request.args.get("page_size", 20), filters)
        result["toast_enabled"] = config.all()["toast_enabled"]
        return jsonify(result)

    @app.patch("/api/alerts/notification-setting")
    @app.post("/api/alerts/notification-setting")
    @login_required(permission="alerts.manage", write=True)
    def update_alert_notification_setting():
        enabled = body().get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        before = config.all()["toast_enabled"]
        config.update({"toast_enabled": enabled})
        audit_action(
            "alert_notification_setting_updated",
            target_type="settings",
            summary="开启告警提醒" if enabled else "关闭告警提醒",
            changes={"toast_enabled": {"before": before, "after": enabled}},
        )
        return jsonify(enabled=enabled)

    @app.get("/api/alerts/export")
    @login_required(permission="page.alerts")
    def export_alerts():
        filters = parse_alert_filters(request.args)
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
            if host.get("status") in {"offline", "ssh_unreachable", "auth_failed", "collection_timeout", "command_error", "fingerprint_error", "degraded", "gpu_error", "busy"} and not issues:
                summary = {
                    "offline": "主机连续采集失败",
                    "ssh_unreachable": "SSH 网络不可达",
                    "auth_failed": "SSH 认证失败",
                    "collection_timeout": "采集超时",
                    "command_error": "远端采集命令失败",
                    "fingerprint_error": "SSH 主机指纹异常",
                    "degraded": "可选指标采集降级",
                    "gpu_error": "GPU 指标采集失败",
                    "busy": "主机采集繁忙",
                }.get(host["status"], host["status"])
                issues.append({"alert_type": host["status"], "severity": "critical" if host["status"] in {"offline", "ssh_unreachable", "auth_failed", "fingerprint_error"} else "warning", "summary": summary, "error_code": host.get("error_code"), "last_error": host.get("last_error")})
            if issues:
                fault_items.append({"host": host, "issues": issues})
        return jsonify(items=fault_items, total=len(fault_items))

    @app.post("/api/alerts/<int:alert_id>/acknowledge")
    @login_required(permission="alerts.manage", write=True)
    def acknowledge_alert(alert_id: int):
        result = alerts.acknowledge(alert_id, g.user["id"])
        audit_action("alert_acknowledged", target_type="alert", target_id=alert_id, summary="确认告警，停止重复提示")
        return jsonify(alert=result)

    @app.post("/api/alerts/bulk-acknowledge")
    @login_required(permission="alerts.manage", write=True)
    def bulk_acknowledge_alerts():
        payload = body()
        raw_filters = payload.get("filters", payload)
        if not isinstance(raw_filters, dict):
            raise ValueError("filters 必须是对象")
        filters = parse_alert_filters(raw_filters)
        count = alerts.bulk_acknowledge(filters, g.user["id"])
        audit_action("alerts_bulk_acknowledged", target_type="alert", summary=f"按当前筛选条件忽略 {count} 条告警提示")
        return jsonify(count=count, limit=1000)

    @app.post("/api/alerts/bulk-clear")
    @login_required(permission="alerts.manage", write=True)
    def bulk_clear_alerts():
        payload = body()
        raw_filters = payload.get("filters", payload)
        if not isinstance(raw_filters, dict):
            raise ValueError("filters 必须是对象")
        filters = parse_alert_filters(raw_filters)
        count = alerts.bulk_clear(filters)
        audit_action("alerts_bulk_cleared", target_type="alert", summary=f"按当前筛选条件软清理 {count} 条告警")
        return jsonify(count=count, limit=1000)

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
        items = []
        for row in rows:
            item = dict(row)
            item["changes"] = json_load(item.pop("changes_json", None), None)
            items.append(item)
        return jsonify(paged(total, page, page_size, items))

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
        before_config = scheduler.get_gpu_config(host, gpu_uuid) or {}
        result = scheduler.configure_gpu(host, gpu_uuid, body())
        audit_action("gpu_config_updated", target_type="gpu", target_id=gpu_uuid, summary="GPU 调度配置已修改", changes=diff_changes(before_config, result or {}))
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

    register_operation_routes(web_context)
    register_development_routes(web_context)
    register_file_routes(web_context)
    register_socket_routes(web_context)

    return app

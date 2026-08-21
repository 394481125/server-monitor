from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable

from flask import Flask, g, jsonify, request

from .audit import AuditService
from .auth import AuthError, AuthService
from .config import ConfigStore
from .db import Database
from .development import DevelopmentService
from .files import SFTPFileService
from .operations import OperationService
from .permissions import PermissionService
from .security import SecretBox
from .services import BackupService, HostService, ServiceError
from .credentials import CredentialService


COOKIE_NAME = "server_monitor_session"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(slots=True)
class WebContext:
    app: Flask
    database: Database
    secret_box: SecretBox
    config: ConfigStore
    auth: AuthService
    audit: AuditService
    hosts: HostService
    operations: OperationService
    development: DevelopmentService
    backups: BackupService
    permission_service: PermissionService
    files: SFTPFileService
    credentials: CredentialService

    def login_required(
        self,
        admin: bool = False,
        permission: str | None = None,
        write: bool = False,
        elevated: bool = False,
    ):
        def decorator(function: Callable[..., Any]):
            @wraps(function)
            def wrapped(*args: Any, **kwargs: Any):
                if not g.user:
                    return jsonify(error="请先登录"), 401
                if admin and g.user["role"] != "admin":
                    return jsonify(error="权限不足"), 403
                if permission and not self.permission_service.allowed(g.user, permission):
                    return jsonify(error="当前账户未获得该功能权限", permission=permission), 403
                if write or request.method in WRITE_METHODS:
                    try:
                        self.auth.require_csrf(g.user, request.headers.get("X-CSRF-Token"))
                    except AuthError as exc:
                        return jsonify(error=str(exc)), 403
                    if g.user["must_change_password"] and request.endpoint not in {"change_password", "logout"}:
                        return jsonify(error="首次登录或密码重置后必须先修改密码", must_change_password=True), 403
                if elevated and not self.auth.is_elevated(g.user):
                    return jsonify(error="该操作需要重新验证当前密码", requires_elevation=True), 403
                return function(*args, **kwargs)

            return wrapped

        return decorator

    @staticmethod
    def body() -> dict[str, Any]:
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return value

    @staticmethod
    def source_ip() -> str:
        # Forwarding headers are intentionally ignored unless deployment adds ProxyFix.
        return request.remote_addr or "unknown"

    def audit_action(
        self,
        action: str,
        *,
        target_type: str | None = None,
        target_id: Any = None,
        success: bool = True,
        summary: str = "",
        error: str | None = None,
        changes: dict[str, Any] | None = None,
    ) -> None:
        self.audit.write(
            action,
            actor=g.user,
            source_ip=self.source_ip(),
            target_type=target_type,
            target_id=target_id,
            request_id=g.request_id,
            success=success,
            summary=summary,
            error=error,
            changes=changes,
        )

    @staticmethod
    def diff_changes(
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        ignored: set[str] | None = None,
    ) -> dict[str, Any]:
        ignored = ignored or set()
        result: dict[str, Any] = {}
        for key in sorted((set(before) | set(after)) - ignored):
            left, right = before.get(key), after.get(key)
            if left != right:
                result[key] = {"before": left, "after": right}
        return result

    def operation_host(self, host_id: int, capability: str, label: str) -> dict[str, Any]:
        host = self.hosts.get(host_id, include_secrets=True)
        if not host.get(capability, False):
            raise ServiceError(f"该主机未允许{label}")
        return host

    def file_host(self, host_id: int) -> dict[str, Any]:
        return self.hosts.get(host_id, include_secrets=True)

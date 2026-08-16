from __future__ import annotations

import json
from typing import Any

from .security import redact
from .utils import utc_iso


SENSITIVE_CHANGE_KEY = ("password", "passwd", "passphrase", "sendkey", "token", "secret", "private_key")


def _sanitize_changes(value: Any, key: str = "") -> Any:
    normalized = key.lower()
    if normalized and any(marker in normalized for marker in SENSITIVE_CHANGE_KEY):
        return "***"
    if isinstance(value, dict):
        return {str(item_key): _sanitize_changes(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_sanitize_changes(item) for item in value]
    return redact(value) if isinstance(value, str) else value


class AuditService:
    def __init__(self, database: Any):
        self.database = database

    def write(
        self,
        action: str,
        *,
        actor: dict[str, Any] | None = None,
        source_ip: str | None = None,
        target_type: str | None = None,
        target_id: str | int | None = None,
        request_id: str | None = None,
        success: bool = True,
        summary: str = "",
        error: str | None = None,
        changes: dict[str, Any] | None = None,
    ) -> int:
        changes_json = None
        if changes:
            changes_json = json.dumps(_sanitize_changes(changes), ensure_ascii=False, sort_keys=True)
        return self.database.execute(
            "INSERT INTO audit_logs(ts,user_id,username,source_ip,action,target_type,target_id,"
            "request_id,success,summary,error,changes_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                utc_iso(),
                actor.get("id") if actor else None,
                actor.get("username") if actor else None,
                source_ip,
                action,
                target_type,
                str(target_id) if target_id is not None else None,
                request_id,
                int(success),
                redact(summary) or "",
                redact(error),
                changes_json,
            ),
        )

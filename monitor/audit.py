from __future__ import annotations

from typing import Any

from .security import redact
from .utils import utc_iso


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
    ) -> int:
        return self.database.execute(
            "INSERT INTO audit_logs(ts,user_id,username,source_ip,action,target_type,target_id,"
            "request_id,success,summary,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
            ),
        )

from __future__ import annotations

import concurrent.futures
import urllib.parse
import urllib.request
from typing import Any

from .security import redact
from .utils import utc_iso


class NotificationService:
    """Best-effort Server Chan delivery that never blocks collection or scheduling."""

    def __init__(self, database: Any, config: Any, secret_box: Any):
        self.database = database
        self.config = config
        self.secret_box = secret_box
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="server-chan")

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def notify(self, alert_id: int) -> None:
        row = self.database.query_one("SELECT * FROM alerts WHERE id=?", (alert_id,))
        if not row:
            return
        settings = self.config.all()
        if not settings["serverchan_enabled"] or row["alert_type"] not in settings["serverchan_events"]:
            return
        encrypted = settings.get("serverchan_sendkey")
        if not encrypted:
            return
        try:
            sendkey = self.secret_box.decrypt(encrypted)
        except Exception:
            self._record(alert_id, False, "SendKey 无法解密")
            return
        if not sendkey:
            return
        self.executor.submit(self._send, dict(row), sendkey)

    def _send(self, alert: dict[str, Any], sendkey: str) -> None:
        host = self.database.query_one("SELECT name FROM hosts WHERE id=?", (alert.get("host_id"),)) if alert.get("host_id") else None
        host_name = host["name"] if host else "平台"
        description = "\n".join((
            f"事件: {alert['alert_type']}",
            f"主机: {host_name}",
            f"摘要: {alert['summary']}",
            f"时间: {alert['created_at']}",
            f"事件 ID: {alert['id']}",
        ))
        body = urllib.parse.urlencode({"title": f"Server Monitor: {alert['alert_type']}", "desp": description}).encode("utf-8")
        try:
            request = urllib.request.Request(f"https://sctapi.ftqq.com/{sendkey}.send", data=body, method="POST")
            with urllib.request.urlopen(request, timeout=8) as response:  # nosec B310: explicitly enabled external service
                summary = f"HTTP {response.status}"
            self._record(alert["id"], True, summary)
            self.database.execute("UPDATE alerts SET last_sent_at=? WHERE id=?", (utc_iso(), alert["id"]))
        except Exception as exc:
            self._record(alert["id"], False, redact(str(exc)) or "通知请求失败")

    def _record(self, alert_id: int, success: bool, summary: str) -> None:
        self.database.execute(
            "INSERT INTO notifications(alert_id,channel,success,response_summary,created_at) VALUES(?,?,?,?,?)",
            (alert_id, "serverchan", int(success), summary, utc_iso()),
        )

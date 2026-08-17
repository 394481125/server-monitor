from __future__ import annotations

import concurrent.futures
from typing import Any
from urllib.parse import urlsplit

from .security import redact
from .utils import utc_iso

try:  # Apprise is an optional import during development; production installs it from requirements.txt.
    import apprise as _apprise
except ImportError:  # pragma: no cover - exercised only in minimal installations
    _apprise = None


class NotificationService:
    """Deliver alert notifications through any service supported by Apprise."""

    def __init__(self, database: Any, config: Any, secret_box: Any):
        self.database = database
        self.config = config
        self.secret_box = secret_box
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="apprise")

    @property
    def available(self) -> bool:
        return _apprise is not None

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _urls(self, values: list[str] | None = None) -> list[str]:
        configured = values if values is not None else self.config.all().get("apprise_urls", [])
        urls: list[str] = []
        for value in configured:
            if not isinstance(value, str) or not value:
                continue
            try:
                decrypted = self.secret_box.decrypt(value[4:]) if value.startswith("enc:") else value
            except Exception:
                continue
            if decrypted and decrypted not in urls:
                urls.append(decrypted)
        return urls

    def notify(self, alert_id: int) -> None:
        row = self.database.query_one("SELECT * FROM alerts WHERE id=?", (alert_id,))
        if not row:
            return
        settings = self.config.all()
        if not settings.get("toast_enabled", True):
            return
        if not settings.get("apprise_enabled") or row["alert_type"] not in settings.get("apprise_events", []):
            return
        urls = self._urls()
        if not urls:
            return
        self.executor.submit(self._send, dict(row), urls)

    def _payload(self, alert: dict[str, Any]) -> tuple[str, str, Any]:
        host = self.database.query_one("SELECT name FROM hosts WHERE id=?", (alert.get("host_id"),)) if alert.get("host_id") else None
        host_name = host["name"] if host else "平台"
        body = "\n".join((
            f"事件: {alert['alert_type']}",
            f"主机: {host_name}",
            f"摘要: {alert['summary']}",
            f"时间: {alert['created_at']}",
            f"事件 ID: {alert['id']}",
        ))
        title = f"Server Monitor: {alert['alert_type']}"
        notify_type = None
        if _apprise is not None:
            notify_type = {
                "critical": _apprise.NotifyType.FAILURE,
                "warning": _apprise.NotifyType.WARNING,
                "info": _apprise.NotifyType.INFO,
            }.get(alert.get("severity"), _apprise.NotifyType.INFO)
        return title, body, notify_type

    @staticmethod
    def _channel(url: str) -> str:
        return f"apprise:{urlsplit(url).scheme.lower() or 'unknown'}"

    def _deliver(self, url: str, title: str, body: str, notify_type: Any) -> tuple[bool, str]:
        if _apprise is None:
            return False, "Apprise 未安装，请安装项目依赖"
        try:
            client = _apprise.Apprise()
            if not client.add(url):
                return False, "Apprise 无法解析该通知 URL"
            kwargs = {"title": title, "body": body}
            if notify_type is not None:
                kwargs["notify_type"] = notify_type
            result = client.notify(**kwargs)
            return bool(result), "发送成功" if result else "通知服务返回失败"
        except Exception as exc:
            message = redact(str(exc)) or "通知请求失败"
            return False, message.replace(url, "通知 URL")

    def _send(self, alert: dict[str, Any], urls: list[str]) -> None:
        title, body, notify_type = self._payload(alert)
        successful = False
        for url in urls:
            success, summary = self._deliver(url, title, body, notify_type)
            successful = successful or success
            self._record(alert["id"], success, summary, channel=self._channel(url))
        if successful:
            self.database.execute("UPDATE alerts SET last_sent_at=? WHERE id=?", (utc_iso(), alert["id"]))

    def test(self, urls: list[str] | None = None) -> dict[str, Any]:
        targets = self._urls(urls)
        if not targets:
            raise ValueError("请至少配置一个通知 URL")
        title = "Server Monitor 测试通知"
        body = "Apprise 通知配置测试成功。"
        notify_type = _apprise.NotifyType.INFO if _apprise is not None else None
        results = []
        for url in targets:
            success, summary = self._deliver(url, title, body, notify_type)
            results.append({"channel": self._channel(url), "success": success, "summary": summary})
        successful_count = sum(1 for item in results if item["success"])
        return {
            "available": self.available,
            "success": successful_count == len(results),
            "successful_count": successful_count,
            "results": results,
        }

    def _record(self, alert_id: int | None, success: bool, summary: str, *, channel: str = "apprise") -> None:
        self.database.execute(
            "INSERT INTO notifications(alert_id,channel,success,response_summary,created_at) VALUES(?,?,?,?,?)",
            (alert_id, channel, int(success), summary, utc_iso()),
        )

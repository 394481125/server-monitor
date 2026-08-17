from __future__ import annotations

import sqlite3
import time
import json
from datetime import timedelta
from pathlib import Path

from monitor.services import BackupService
from monitor.utils import utc_iso, utc_now

from .conftest import csrf


def test_apprise_settings_are_encrypted_and_masked(client, app, admin):
    response = client.patch(
        "/api/settings",
        json={
            "apprise_enabled": True,
            "apprise_urls": ["ntfy://shengziran", "discord://bot-secret.example.invalid/webhook-token"],
            "apprise_events": ["host_offline"],
        },
        headers=csrf(admin),
    )
    assert response.status_code == 200, response.get_json()
    settings = client.get("/api/settings").get_json()["settings"]
    assert settings["apprise_urls"] == ["ntfy://shengziran", "discord://***"]
    stored = app.extensions["database"].query_one("SELECT value FROM settings WHERE key='apprise_urls'")
    assert "shengziran" not in stored["value"]
    assert all(value.startswith("enc:") for value in json.loads(stored["value"]))


def test_apprise_test_endpoint_resolves_configured_url(client, app, admin, monkeypatch):
    app.extensions["monitor_config"].update({"apprise_urls": ["ntfy://shengziran"]})
    calls = []
    monkeypatch.setattr(
        app.extensions["notifications"],
        "_deliver",
        lambda url, title, body, notify_type: calls.append((url, title)) or (True, "ok"),
    )
    response = client.post("/api/notifications/test", json={"url": "configured:0"}, headers=csrf(admin))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["success"] is True
    assert calls == [("ntfy://shengziran", "Server Monitor 测试通知")]


def test_notification_payload_is_redacted_and_async(app, monkeypatch):
    notifications = app.extensions["notifications"]
    sent = []
    monkeypatch.setattr(notifications, "_send", lambda alert, urls: sent.append((alert["alert_type"], urls)))
    config = app.extensions["monitor_config"]
    config.update({"apprise_enabled": True, "apprise_urls": ["ntfy://test-topic"], "apprise_events": ["host_offline"]})
    app.extensions["alerts"].emit("test-host-offline", None, "host_offline", "critical", "摘要 password=secret")
    for _ in range(20):
        if sent:
            break
        time.sleep(0.01)
    assert sent == [("host_offline", ["ntfy://test-topic"])]
    notifications.close()


def test_backup_online_api_and_restore_probe(app, tmp_path):
    database = app.extensions["database"]
    box = app.extensions["secret_box"]
    encrypted = box.encrypt("dedicated-probe")
    database.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("probe", '"ignored"'))
    service = BackupService(database, tmp_path)
    backup = service.create(tmp_path / "backups", 2)
    assert backup.exists() and backup.stat().st_mode & 0o777 == 0o600
    assert backup.parent.stat().st_mode & 0o777 == 0o700
    restored = service.verify_restore(backup, tmp_path / "restore", box, encrypted)
    check = sqlite3.connect(restored)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    check.close()


def test_backup_api_resolves_relative_directory_from_data_dir(app, client, admin):
    response = client.post("/api/backups", json={}, headers=csrf(admin))
    assert response.status_code == 201, response.get_json()
    backup = Path(response.get_json()["path"])
    assert backup.parent == Path(app.config["DATA_DIR"]) / "backups"
    assert backup.exists()


def test_database_compaction_requires_elevation_and_reclaims_expired_tasks(app, client, admin):
    database = app.extensions["database"]
    database.execute(
        "INSERT INTO tasks(id,task_type,state,result_json,created_at,finished_at) VALUES(?,?,?,?,?,?)",
        ("expired-large-task", "collection", "success", "x" * (1024 * 1024), utc_iso(utc_now() - timedelta(hours=2)), utc_iso()),
    )
    denied = client.post("/api/maintenance/compact", json={}, headers=csrf(admin))
    assert denied.status_code == 403 and denied.get_json()["requires_elevation"] is True
    elevated = client.post("/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin))
    assert elevated.status_code == 200

    result = client.post("/api/maintenance/compact", json={}, headers=csrf(admin))

    assert result.status_code == 200, result.get_json()
    payload = result.get_json()
    assert payload["cleanup"]["collection_tasks"] == 1
    assert payload["after_bytes"] <= payload["before_bytes"]
    assert payload["reclaimed_bytes"] > 0
    assert database.query_one("SELECT id FROM tasks WHERE id='expired-large-task'") is None
    with database.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

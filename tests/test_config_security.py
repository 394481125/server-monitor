from __future__ import annotations

import os
import json

import pytest

from monitor.config import DEFAULTS, LEGACY_ALERT_DEFAULTS, ConfigError, ConfigStore, validate_settings
from monitor.app import create_app
from monitor.security import PasswordService, ProcessLock, SecretBox, redact


def test_settings_validate_ranges_and_cross_fields():
    assert DEFAULTS["filesystem_usage_threshold"] == 90
    assert DEFAULTS["swap_usage_threshold"] == 80
    assert DEFAULTS["alert_samples"] == 5
    assert DEFAULTS["cleanup_interval_minutes"] == 60
    assert {"gpu_idle", "gpu_busy"} <= set(DEFAULTS["apprise_events"])
    assert DEFAULTS["toast_events"] == DEFAULTS["apprise_events"]
    with pytest.raises(ConfigError):
        validate_settings({"collection_interval": 1})
    with pytest.raises(ConfigError):
        validate_settings({"green_threshold": 90, "yellow_threshold": 80})
    with pytest.raises(ConfigError):
        validate_settings({"ssh_connect_timeout": 20, "collection_timeout": 10})
    with pytest.raises(ConfigError):
        validate_settings({"metric_raw_retention_minutes": 120, "metric_mid_retention_hours": 1})
    with pytest.raises(ConfigError):
        validate_settings({"metric_mid_retention_hours": 24, "metric_retention_days": 1})
    with pytest.raises(ConfigError):
        validate_settings({"scan_timeout_seconds": 121})
    with pytest.raises(ConfigError):
        validate_settings({"cleanup_interval_minutes": 0})
    with pytest.raises(ConfigError, match="协议无效"):
        validate_settings({"apprise_urls": ["missing-scheme"]})
    assert validate_settings({"scan_max_depth": "6"})["scan_max_depth"] == 6
    assert validate_settings({"collection_interval": "10"})["collection_interval"] == 10
    assert validate_settings({"cleanup_interval_minutes": "60"})["cleanup_interval_minutes"] == 60
    assert validate_settings({"gpu_power_alert_enabled": False})["gpu_power_alert_enabled"] is False
    assert validate_settings({"toast_events": ["host_offline"]})["toast_events"] == ["host_offline"]
    with pytest.raises(ConfigError, match="toast_events"):
        validate_settings({"toast_events": ["unknown_alert"]})
    assert validate_settings({"apprise_urls": ["ntfy://shengziran"]})["apprise_urls"] == ["ntfy://shengziran"]


def test_secret_box_round_trip_and_permissions(tmp_path):
    key_path = tmp_path / "master.key"
    box = SecretBox(key_path)
    encrypted = box.encrypt("密码内容")
    assert encrypted and encrypted != "密码内容"
    assert box.decrypt(encrypted) == "密码内容"
    assert os.stat(key_path).st_mode & 0o777 == 0o600
    assert box.decrypt(None) is None


def test_password_policy_and_redaction():
    with pytest.raises(ValueError):
        PasswordService.hash("short")
    with pytest.raises(ValueError):
        PasswordService.hash("qwer1234")
    initial_hash = PasswordService.hash_initial("qwer1234")
    assert PasswordService.verify(initial_hash, "qwer1234")
    hashed = PasswordService.hash("a-long-test-password")
    assert PasswordService.verify(hashed, "a-long-test-password")
    assert not PasswordService.verify(hashed, "wrong-password")
    assert "password=***" in (redact("password=secret") or "")
    assert "token=***" in (redact("token=abc") or "")


def test_process_lock_is_exclusive(tmp_path):
    first = ProcessLock(tmp_path / "monitor.lock")
    second = ProcessLock(tmp_path / "monitor.lock")
    first.acquire()
    with pytest.raises(RuntimeError):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_app_acquires_process_lock_before_database_access(tmp_path, monkeypatch):
    lock = ProcessLock(tmp_path / "server-monitor.lock")
    lock.acquire()
    monkeypatch.setattr("monitor.app.Database.initialize", lambda _self: pytest.fail("锁冲突后不应访问数据库"))
    try:
        with pytest.raises(RuntimeError, match="已有 Server Monitor 实例"):
            create_app({"DATA_DIR": str(tmp_path), "START_BACKGROUND": False, "ACQUIRE_PROCESS_LOCK": True})
    finally:
        lock.release()


def test_config_store_round_trip(tmp_path):
    from monitor.db import Database

    database = Database(tmp_path / "db")
    database.initialize()
    store = ConfigStore(database)
    assert store.update({"toast_enabled": False})["toast_enabled"] is False
    store.update({"metric_mid_retention_hours": 48})
    with pytest.raises(ConfigError, match="中期聚合"):
        store.update({"metric_retention_days": 1})
    assert os.stat(database.path).st_mode & 0o777 == 0o600


def test_config_store_discards_retired_serverchan_settings(tmp_path):
    from monitor.db import Database

    database = Database(tmp_path / "db")
    database.initialize()
    for key in ("serverchan_enabled", "serverchan_sendkey", "serverchan_events"):
        database.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, '"legacy"'))
    store = ConfigStore(database)
    store.remove_legacy_notification_settings()
    assert database.query_one("SELECT key FROM settings WHERE key LIKE 'serverchan_%'") is None


def test_legacy_alert_defaults_are_migrated_but_custom_values_are_preserved(tmp_path):
    from monitor.db import Database

    database = Database(tmp_path / "db")
    database.initialize()
    for key, value in LEGACY_ALERT_DEFAULTS.items():
        database.execute(
            "INSERT INTO settings(key,value) VALUES(?,?)",
            (key, json.dumps(value)),
        )
    database.execute(
        "UPDATE settings SET value=? WHERE key=?",
        (json.dumps(82), "cpu_temp_threshold"),
    )
    store = ConfigStore(database)
    values = store.migrate_alert_defaults()
    assert values["gpu_temp_threshold"] == DEFAULTS["gpu_temp_threshold"]
    assert values["filesystem_usage_threshold"] == DEFAULTS["filesystem_usage_threshold"]
    assert values["cpu_temp_threshold"] == 82


def test_app_data_directory_permissions(tmp_path):
    data_dir = tmp_path / "data"
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(data_dir),
            "INITIAL_ADMIN_PASSWORD": "TemporaryPass123",
            "START_BACKGROUND": False,
            "ACQUIRE_PROCESS_LOCK": False,
        }
    )
    try:
        assert os.stat(data_dir).st_mode & 0o777 == 0o700
        assert os.stat(application.extensions["database"].path).st_mode & 0o777 == 0o600
    finally:
        application.extensions["shutdown"]()

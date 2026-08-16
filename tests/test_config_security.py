from __future__ import annotations

import os

import pytest

from monitor.config import ConfigError, ConfigStore, validate_settings
from monitor.app import create_app
from monitor.security import PasswordService, ProcessLock, SecretBox, redact


def test_settings_validate_ranges_and_cross_fields():
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
    assert validate_settings({"collection_interval": "10"})["collection_interval"] == 10


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

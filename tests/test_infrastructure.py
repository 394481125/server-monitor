from __future__ import annotations

import sqlite3

import pytest

from monitor.db import Database
from monitor.logging_config import configured_log_level
from monitor.migrations import MIGRATIONS, Migration, run_migrations
from monitor.ssh_client import SSHConnectionPool


def test_database_applies_ordered_migrations_and_records_checksums(tmp_path):
    database = Database(tmp_path / "migrations.sqlite3")
    database.initialize()

    rows = database.query_all("SELECT version,name,checksum FROM schema_migrations ORDER BY version")
    assert [row["version"] for row in rows] == list(range(1, len(MIGRATIONS) + 1))
    assert all(row["name"] and len(row["checksum"]) == 64 for row in rows)
    assert database.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='gpu_benchmarks'")
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_migration_batch_rolls_back_all_pending_steps_on_failure():
    connection = sqlite3.connect(":memory:")

    def first(database):
        database.execute("CREATE TABLE should_rollback(id INTEGER PRIMARY KEY)")

    def second(_database):
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        run_migrations(connection, (Migration(1, "first", first), Migration(2, "second", second)))

    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='should_rollback'"
    ).fetchone() is None
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0


def test_migration_runner_rejects_newer_database_and_backfills_legacy_metadata():
    newer = sqlite3.connect(":memory:")
    newer.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="拒绝降级"):
        run_migrations(newer)
    assert newer.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone() is None

    legacy = sqlite3.connect(":memory:")
    legacy.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    legacy.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(1,'legacy')")
    legacy.execute("PRAGMA user_version = 1")
    legacy_migrations = (
        Migration(1, "legacy-one", lambda _database: None),
        Migration(2, "legacy-two", lambda database: database.execute("CREATE TABLE upgraded(id INTEGER)")),
    )
    run_migrations(legacy, legacy_migrations)
    first = legacy.execute(
        "SELECT name,checksum FROM schema_migrations WHERE version=1"
    ).fetchone()
    assert first[0] == legacy_migrations[0].name and first[1] == legacy_migrations[0].checksum


def test_migration_runner_repairs_legacy_columns_marked_by_old_bootstrap(tmp_path):
    database = Database(tmp_path / "legacy.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version >= 2")
        connection.execute("DROP TABLE gpu_benchmarks")
        connection.execute("ALTER TABLE alerts DROP COLUMN acknowledged_at")
        connection.execute("ALTER TABLE alerts DROP COLUMN acknowledged_by")
        connection.execute("ALTER TABLE alerts DROP COLUMN cleared_at")
        connection.execute("ALTER TABLE hosts DROP COLUMN asset_location")
        connection.execute("ALTER TABLE hosts DROP COLUMN asset_owner")
        connection.execute("ALTER TABLE hosts DROP COLUMN warranty_expires")
        connection.execute("ALTER TABLE host_runtime DROP COLUMN error_code")
        connection.execute("ALTER TABLE audit_logs DROP COLUMN changes_json")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    database.initialize()

    assert {row["name"] for row in database.query_all("PRAGMA table_info(alerts)")} >= {
        "acknowledged_at", "acknowledged_by", "cleared_at"
    }
    assert {row["name"] for row in database.query_all("PRAGMA table_info(hosts)")} >= {
        "asset_location", "asset_owner", "warranty_expires"
    }
    assert database.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='gpu_benchmarks'")


def test_log_level_supports_service_specific_and_standard_environment(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    assert configured_log_level()[0] == "WARNING"
    monkeypatch.setenv("SERVER_MONITOR_LOG_LEVEL", "debug")
    assert configured_log_level()[0] == "DEBUG"
    monkeypatch.setenv("SERVER_MONITOR_LOG_LEVEL", "verbose")
    with pytest.raises(RuntimeError, match="LOG_LEVEL"):
        configured_log_level()


class FakePoolClient:
    def __init__(self, host, _secret_box, _settings):
        self.host = host
        self.active = False
        self.closed = 0

    def connect(self):
        self.active = True
        return "SHA256:test"

    def is_reusable(self):
        return self.active

    def close(self):
        self.active = False
        self.closed += 1


def test_ssh_pool_reuses_only_idle_matching_connections_and_expires_them():
    now = [100.0]
    settings = {"ssh_reuse": True, "ssh_idle_close": 10, "ssh_concurrency": 2}
    created = []

    def factory(host, secret_box, current):
        client = FakePoolClient(host, secret_box, current)
        created.append(client)
        return client

    pool = SSHConnectionPool(None, lambda: settings, client_factory=factory, clock=lambda: now[0])
    host = {"id": 1, "address": "10.0.0.1", "port": 22, "username": "ops", "auth_type": "password", "auth_secret": "one", "fingerprint": "SHA256:test"}

    first = pool.client(host)
    first.connect()
    first_raw = first.client
    first.close()
    assert pool.idle_count() == 1

    second = pool.client(host)
    assert second.client is first_raw
    second.close()

    changed = pool.client({**host, "auth_secret": "two"})
    assert changed.client is not first_raw
    assert first_raw.closed == 1
    changed.connect()
    changed_raw = changed.client
    changed.close()

    now[0] += 11
    fresh = pool.client({**host, "auth_secret": "two"})
    assert fresh.client is not changed_raw
    assert changed_raw.closed == 1
    fresh.close()
    pool.close()

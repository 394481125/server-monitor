from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator, Sequence

from .migrations import run_migrations


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    name TEXT,
    checksum TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'viewer')),
    active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    theme TEXT NOT NULL DEFAULT 'light' CHECK(theme IN ('light', 'dark', 'tech')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_permissions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission TEXT NOT NULL,
    granted INTEGER NOT NULL DEFAULT 0 CHECK(granted IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, permission)
);

CREATE TABLE IF NOT EXISTS user_permission_preferences (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission TEXT NOT NULL,
    visible INTEGER NOT NULL DEFAULT 1 CHECK(visible IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, permission)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    source_ip TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    elevated_until TEXT
);

CREATE TABLE IF NOT EXISTS login_attempts (
    username TEXT NOT NULL,
    source_ip TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    window_started_at TEXT NOT NULL,
    locked_until TEXT,
    PRIMARY KEY(username, source_ip)
);

CREATE TABLE IF NOT EXISTS hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 22,
    username TEXT NOT NULL,
    auth_type TEXT NOT NULL CHECK(auth_type IN ('password', 'key')),
    auth_secret TEXT,
    private_key TEXT,
    private_key_passphrase TEXT,
    sudo_password TEXT,
    fingerprint TEXT,
    machine_id TEXT,
    physical_id TEXT,
    identity_degraded INTEGER NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    asset_location TEXT NOT NULL DEFAULT '',
    asset_owner TEXT NOT NULL DEFAULT '',
    warranty_expires TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    docker_enabled INTEGER NOT NULL DEFAULT 1,
    allow_tmux INTEGER NOT NULL DEFAULT 1,
    allow_terminal INTEGER NOT NULL DEFAULT 1,
    allow_process INTEGER NOT NULL DEFAULT 1,
    allow_install INTEGER NOT NULL DEFAULT 1,
    allow_stress INTEGER NOT NULL DEFAULT 1,
    timeout_seconds INTEGER,
    scheduler_enabled INTEGER NOT NULL DEFAULT 0,
    scheduler_idle_seconds INTEGER,
    scheduler_process_guard INTEGER,
    schedule_command TEXT,
    schedule_cwd TEXT,
    schedule_shell TEXT NOT NULL DEFAULT '/bin/bash',
    schedule_env_json TEXT NOT NULL DEFAULT '{}',
    schedule_mode TEXT NOT NULL DEFAULT 'tmux' CHECK(schedule_mode IN ('tmux', 'direct')),
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_host_endpoint
ON hosts(address, port) WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_physical_host
ON hosts(physical_id) WHERE deleted_at IS NULL AND physical_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS host_runtime (
    host_id INTEGER PRIMARY KEY REFERENCES hosts(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'unknown',
    failure_cycles INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_attempt_at TEXT,
    last_error TEXT,
    error_code TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS latest_samples (
    host_id INTEGER PRIMARY KEY REFERENCES hosts(id) ON DELETE CASCADE,
    collected_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    metric TEXT NOT NULL,
    object_key TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK(kind IN ('raw', 'mid', 'long')),
    ts TEXT NOT NULL,
    value REAL,
    max_value REAL,
    UNIQUE(host_id, metric, object_key, kind, ts)
);

CREATE INDEX IF NOT EXISTS idx_metric_query
ON metric_points(host_id, metric, object_key, kind, ts);

CREATE TABLE IF NOT EXISTS mount_alert_thresholds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    mountpoint TEXT NOT NULL,
    usage_threshold REAL NOT NULL CHECK(usage_threshold > 0 AND usage_threshold <= 100),
    inode_threshold REAL CHECK(inode_threshold IS NULL OR (inode_threshold > 0 AND inode_threshold <= 100)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(host_id, mountpoint)
);

CREATE INDEX IF NOT EXISTS idx_mount_threshold_host ON mount_alert_thresholds(host_id, mountpoint);

CREATE TABLE IF NOT EXISTS gpu_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    physical_id TEXT NOT NULL,
    gpu_uuid TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    idle_mode TEXT CHECK(idle_mode IN ('util', 'memory', 'both')),
    util_threshold INTEGER,
    memory_threshold INTEGER,
    idle_seconds INTEGER,
    process_guard INTEGER,
    command_override TEXT,
    cwd_override TEXT,
    shell_override TEXT,
    env_override_json TEXT,
    mode_override TEXT CHECK(mode_override IN ('tmux', 'direct')),
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_gpu_config
ON gpu_configs(physical_id, gpu_uuid) WHERE active = 1;

CREATE TABLE IF NOT EXISTS gpu_runtime (
    physical_id TEXT NOT NULL,
    gpu_uuid TEXT NOT NULL,
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'unknown',
    idle_seconds_accum REAL NOT NULL DEFAULT 0,
    last_valid_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at TEXT,
    cooldown_until TEXT,
    frozen_until TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(physical_id, gpu_uuid)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
    gpu_uuid TEXT,
    state TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS schedule_jobs (
    id TEXT PRIMARY KEY,
    host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
    physical_id TEXT NOT NULL,
    gpu_uuid TEXT NOT NULL,
    mode TEXT NOT NULL,
    command_summary TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    stdout_truncated INTEGER NOT NULL DEFAULT 0,
    stderr_truncated INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_key TEXT NOT NULL,
    host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
    object_key TEXT,
    alert_type TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active', 'recovered')),
    severity TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_sent_at TEXT,
    recovered_at TEXT,
    acknowledged_at TEXT,
    acknowledged_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    cleared_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_alert_lookup ON alerts(alert_key, state, created_at);
CREATE INDEX IF NOT EXISTS idx_alert_filter ON alerts(created_at, alert_type, state);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username TEXT,
    source_ip TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    request_id TEXT,
    success INTEGER NOT NULL,
    summary TEXT NOT NULL,
    error TEXT,
    changes_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_search ON audit_logs(ts, action, username, success);
CREATE INDEX IF NOT EXISTS idx_schedule_filter ON schedule_jobs(started_at, state, host_id);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER REFERENCES alerts(id) ON DELETE SET NULL,
    channel TEXT NOT NULL,
    success INTEGER NOT NULL,
    response_summary TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    size_bytes INTEGER,
    success INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stress_jobs (
    id TEXT PRIMARY KEY,
    host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
    state TEXT NOT NULL,
    cpu_workers INTEGER NOT NULL,
    memory_workers INTEGER NOT NULL,
    memory_percent INTEGER NOT NULL,
    duration_seconds INTEGER NOT NULL,
    remote_pid INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_stress_jobs_host ON stress_jobs(host_id, started_at);

CREATE TABLE IF NOT EXISTS saved_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    page TEXT NOT NULL CHECK(page IN ('dashboard', 'hosts')),
    name TEXT NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, page, name)
);

"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        if self.path.stat().st_mode & 0o077:
            raise RuntimeError("数据库文件权限过宽，必须设置为 0600")
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        with self._lock:
            connection = self.connect()
            try:
                connection.executescript(SCHEMA)
                connection.commit()
                run_migrations(connection)
            finally:
                connection.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        with self._lock:
            with self.connect() as connection:
                cursor = connection.execute(sql, parameters)
                connection.commit()
                return cursor.lastrowid

    def query_one(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            with self.connect() as connection:
                return connection.execute(sql, parameters).fetchone()

    def query_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            with self.connect() as connection:
                return list(connection.execute(sql, parameters).fetchall())

    def storage_info(self) -> dict[str, int]:
        paths = {
            "database_size_bytes": self.path,
            "wal_size_bytes": Path(f"{self.path}-wal"),
            "shm_size_bytes": Path(f"{self.path}-shm"),
        }
        values = {key: item.stat().st_size if item.exists() else 0 for key, item in paths.items()}
        values["database_total_bytes"] = sum(values.values())
        values["disk_free_bytes"] = shutil.disk_usage(self.path.parent).free
        return values

    def online_backup(self, target: str | Path) -> None:
        with self._lock:
            source = self.connect()
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                source.close()
                destination.close()

    def compact(self) -> dict[str, int]:
        """Checkpoint WAL and reclaim deleted SQLite pages in one process-wide critical section."""
        with self._lock:
            before = self.storage_info()
            required_free = before["database_size_bytes"] + max(64 * 1024 * 1024, before["database_size_bytes"] // 10)
            if before["disk_free_bytes"] < required_free:
                raise ValueError(
                    "磁盘可用空间不足以安全压缩数据库；请先将备份目录移到其他磁盘或清理无用备份"
                )
            connection = self.connect()
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
            after = self.storage_info()
            return {
                "before_bytes": before["database_total_bytes"],
                "after_bytes": after["database_total_bytes"],
                "database_size_bytes": after["database_size_bytes"],
                "wal_size_bytes": after["wal_size_bytes"],
                "disk_free_bytes": after["disk_free_bytes"],
                "reclaimed_bytes": max(0, before["database_total_bytes"] - after["database_total_bytes"]),
            }

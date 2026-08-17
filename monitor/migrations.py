from __future__ import annotations

import hashlib
import inspect
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable


MigrationStep = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    upgrade: MigrationStep

    @property
    def checksum(self) -> str:
        try:
            source = inspect.getsource(self.upgrade)
        except (OSError, TypeError):
            source = self.name
        return hashlib.sha256(f"{self.version}:{self.name}:{source}".encode("utf-8")).hexdigest()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_columns(connection: sqlite3.Connection, table: str, definitions: tuple[tuple[str, str], ...]) -> None:
    existing = _columns(connection, table)
    for name, definition in definitions:
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _baseline(_connection: sqlite3.Connection) -> None:
    # The idempotent base schema is installed before ordered migrations run.
    return


def _alert_acknowledgement(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "alerts",
        (
            ("acknowledged_at", "TEXT"),
            ("acknowledged_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
            ("cleared_at", "TEXT"),
        ),
    )


def _host_assets(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "hosts",
        (
            ("asset_location", "TEXT NOT NULL DEFAULT ''"),
            ("asset_owner", "TEXT NOT NULL DEFAULT ''"),
            ("warranty_expires", "TEXT"),
        ),
    )


def _runtime_and_audit_details(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "host_runtime", (("error_code", "TEXT"),))
    _add_columns(connection, "audit_logs", (("changes_json", "TEXT"),))


def _saved_views(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            page TEXT NOT NULL CHECK(page IN ('dashboard', 'hosts')),
            name TEXT NOT NULL,
            filters_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, page, name)
        )
        """
    )


def _supporting_indexes(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE INDEX IF NOT EXISTS idx_schedule_filter ON schedule_jobs(started_at, state, host_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_stress_jobs_host ON stress_jobs(host_id, started_at)")


def _repair_legacy_schema_and_add_gpu_benchmarks(connection: sqlite3.Connection) -> None:
    # Releases before the ordered runner marked versions 1-6 during bootstrap.
    # Recheck their expected columns once so those databases converge safely.
    _alert_acknowledgement(connection)
    _host_assets(connection)
    _runtime_and_audit_details(connection)
    _saved_views(connection)
    _supporting_indexes(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS gpu_benchmarks (
            id TEXT PRIMARY KEY,
            host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            mode TEXT NOT NULL CHECK(mode IN ('single', 'multi')),
            python_command TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL,
            gpu_count INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_gpu_benchmark_host ON gpu_benchmarks(host_id, created_at DESC)"
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline", _baseline),
    Migration(2, "alert-acknowledgement", _alert_acknowledgement),
    Migration(3, "host-asset-fields", _host_assets),
    Migration(4, "runtime-and-audit-details", _runtime_and_audit_details),
    Migration(5, "saved-views", _saved_views),
    Migration(6, "supporting-indexes", _supporting_indexes),
    Migration(7, "legacy-repair-and-gpu-benchmarks", _repair_legacy_schema_and_add_gpu_benchmarks),
)


def _validate_order(migrations: tuple[Migration, ...]) -> None:
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise RuntimeError("数据库迁移版本必须从 1 开始且连续递增")
    names = [migration.name for migration in migrations]
    if len(names) != len(set(names)):
        raise RuntimeError("数据库迁移名称不能重复")


def ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            name TEXT,
            checksum TEXT
        )
        """
    )
    _add_columns(connection, "schema_migrations", (("name", "TEXT"), ("checksum", "TEXT")))


def run_migrations(
    connection: sqlite3.Connection,
    migrations: Iterable[Migration] = MIGRATIONS,
) -> list[int]:
    ordered = tuple(migrations)
    _validate_order(ordered)
    declared_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if declared_version > len(ordered):
        raise RuntimeError("数据库版本高于当前程序支持的版本，拒绝降级启动")
    ensure_migration_table(connection)
    connection.commit()
    applied_rows = {
        int(row[0]): {"name": row[1], "checksum": row[2]}
        for row in connection.execute("SELECT version,name,checksum FROM schema_migrations ORDER BY version")
    }
    if applied_rows and max(applied_rows) > len(ordered):
        raise RuntimeError("数据库版本高于当前程序支持的版本，拒绝降级启动")
    for migration in ordered:
        row = applied_rows.get(migration.version)
        if row and row["name"] and row["name"] != migration.name:
            raise RuntimeError(f"数据库迁移 {migration.version} 名称不一致")
        if row and row["checksum"] and row["checksum"] != migration.checksum:
            raise RuntimeError(f"数据库迁移 {migration.version} 校验和不一致")

    applied_now: list[int] = []
    connection.execute("BEGIN IMMEDIATE")
    try:
        for migration in ordered:
            if migration.version in applied_rows:
                row = applied_rows[migration.version]
                if not row["name"] or not row["checksum"]:
                    connection.execute(
                        "UPDATE schema_migrations SET name=?,checksum=? WHERE version=?",
                        (migration.name, migration.checksum, migration.version),
                    )
                continue
            migration.upgrade(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at,name,checksum) "
                "VALUES(?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?,?)",
                (migration.version, migration.name, migration.checksum),
            )
            applied_now.append(migration.version)
        connection.execute(f"PRAGMA user_version = {len(ordered)}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return applied_now

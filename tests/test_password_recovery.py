from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from monitor.security import PasswordService
from scripts.reset_admin_password import reset_password


def test_reset_admin_password_revokes_sessions_and_clears_lock(app, tmp_path: Path):
    database_path = Path(app.config["DATABASE"])
    database = app.extensions["database"]
    user = database.query_one("SELECT id FROM users WHERE username='admin'")
    database.execute(
        "INSERT INTO login_attempts(username,source_ip,attempts,window_started_at,locked_until) VALUES(?,?,?,?,?)",
        ("admin", "127.0.0.1", 4, "2026-01-01T00:00:00.000Z", None),
    )
    database.execute(
        "INSERT INTO sessions(token_hash,user_id,csrf_token,created_at,last_seen_at,expires_at) VALUES(?,?,?,?,?,?)",
        ("token", user["id"], "csrf", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z", "2030-01-01T00:00:00.000Z"),
    )

    reset_password(database_path, "admin", "RecoveredPass123")

    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT password_hash,must_change_password FROM users WHERE username='admin'"
    ).fetchone()
    assert PasswordService.verify(row[0], "RecoveredPass123")
    assert row[1] == 1
    assert connection.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user["id"],)).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM login_attempts WHERE username='admin'").fetchone()[0] == 0
    assert connection.execute("SELECT action FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()[0] == "password_recovered"
    connection.close()


def test_reset_password_rejects_non_admin(app):
    database_path = Path(app.config["DATABASE"])
    database = app.extensions["database"]
    now = "2026-01-01T00:00:00.000Z"
    database.execute(
        "INSERT INTO users(username,password_hash,role,active,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("viewer", PasswordService.hash("ViewerPass123"), "viewer", 1, now, now),
    )

    with pytest.raises(ValueError, match="非管理员"):
        reset_password(database_path, "viewer", "RecoveredPass123")

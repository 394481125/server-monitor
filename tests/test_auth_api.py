from __future__ import annotations

from .conftest import csrf, login
from monitor.app import create_app


def test_production_first_start_requires_initial_password(tmp_path):
    import pytest

    with pytest.raises(RuntimeError, match="SERVER_MONITOR_INITIAL_PASSWORD"):
        create_app(
            {
                "DATA_DIR": str(tmp_path),
                "DATABASE": str(tmp_path / "server-monitor.sqlite3"),
                "MASTER_KEY": str(tmp_path / "master.key"),
                "INITIAL_ADMIN_PASSWORD": None,
                "START_BACKGROUND": False,
                "ACQUIRE_PROCESS_LOCK": False,
            }
        )


def test_existing_install_can_restart_without_initial_password(tmp_path):
    config = {
        "DATA_DIR": str(tmp_path),
        "DATABASE": str(tmp_path / "server-monitor.sqlite3"),
        "MASTER_KEY": str(tmp_path / "master.key"),
        "START_BACKGROUND": False,
        "ACQUIRE_PROCESS_LOCK": False,
    }
    first = create_app({**config, "INITIAL_ADMIN_PASSWORD": "TemporaryPass123"})
    first.extensions["shutdown"]()

    restarted = create_app({**config, "INITIAL_ADMIN_PASSWORD": None})
    try:
        assert restarted.extensions["database"].query_one("SELECT username FROM users")["username"] == "admin"
    finally:
        restarted.extensions["shutdown"]()


def test_default_initial_password_is_allowed_and_requires_rotation(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "server-monitor.sqlite3"),
            "MASTER_KEY": str(tmp_path / "master.key"),
            "START_BACKGROUND": False,
            "ACQUIRE_PROCESS_LOCK": False,
        }
    )
    try:
        client = app.test_client()
        response = client.post("/api/auth/login", json={"username": "admin", "password": "qwer1234"})
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["user"]["must_change_password"] is True
    finally:
        app.extensions["shutdown"]()


def test_empty_initial_password_is_rejected(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="首次引导密码"):
        create_app(
            {
                "TESTING": True,
                "DATA_DIR": str(tmp_path),
                "DATABASE": str(tmp_path / "server-monitor.sqlite3"),
                "MASTER_KEY": str(tmp_path / "master.key"),
                "INITIAL_ADMIN_PASSWORD": "",
                "START_BACKGROUND": False,
                "ACQUIRE_PROCESS_LOCK": False,
            }
        )


def test_initial_password_rotation_and_csrf(client):
    user = login(client)
    assert user["must_change_password"] is True
    denied = client.patch("/api/settings", json={"collection_interval": 10}, headers=csrf(user))
    assert denied.status_code == 403
    missing_csrf = client.post("/api/auth/change-password", json={"current_password": "TemporaryPass123", "new_password": "TemporaryPass456"})
    assert missing_csrf.status_code == 403
    changed = client.post("/api/auth/change-password", json={"current_password": "TemporaryPass123", "new_password": "TemporaryPass456"}, headers=csrf(user))
    assert changed.status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    rotated = login(client, password="TemporaryPass456")
    assert rotated["must_change_password"] is False


def test_login_failure_is_locked_by_source_ip(client):
    for _ in range(4):
        assert client.post("/api/auth/login", json={"username": "unknown", "password": "bad"}).status_code == 401
    fifth = client.post("/api/auth/login", json={"username": "another", "password": "bad"})
    assert fifth.status_code == 429


def test_last_admin_cannot_be_disabled_or_deleted(client, admin):
    response = client.patch("/api/users/1", json={"active": False}, headers=csrf(admin))
    assert response.status_code == 400
    elevated = client.post("/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin))
    assert elevated.status_code == 200
    response = client.delete("/api/users/1", headers=csrf(admin))
    assert response.status_code == 400


def test_read_only_user_cannot_write(client, admin):
    token = client.post("/api/users", json={"username": "viewer", "password": "ViewerPass123", "role": "viewer"}, headers=csrf(admin))
    assert token.status_code == 201
    client.post("/api/auth/logout", json={}, headers=csrf(admin))
    viewer = login(client, "viewer", "ViewerPass123")
    assert client.patch("/api/settings", json={"collection_interval": 10}, headers=csrf(viewer)).status_code in {401, 403}


def test_duplicate_user_and_missing_password_reset_are_actionable(client, admin):
    created = client.post(
        "/api/users",
        json={"username": "viewer", "password": "ViewerPass123", "role": "viewer"},
        headers=csrf(admin),
    )
    assert created.status_code == 201
    duplicate = client.post(
        "/api/users",
        json={"username": "VIEWER", "password": "ViewerPass456", "role": "viewer"},
        headers=csrf(admin),
    )
    assert duplicate.status_code == 400
    assert duplicate.get_json()["error"] == "用户名已存在"

    elevated = client.post(
        "/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin)
    )
    assert elevated.status_code == 200
    missing = client.post(
        "/api/users/999/reset-password",
        json={"password": "ReplacementPass123"},
        headers=csrf(admin),
    )
    assert missing.status_code == 400
    assert missing.get_json()["error"] == "用户不存在"

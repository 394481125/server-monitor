from __future__ import annotations

from pathlib import Path

import pytest

from monitor.app import create_app


@pytest.fixture()
def app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "server-monitor.sqlite3"),
            "MASTER_KEY": str(tmp_path / "master.key"),
            "INITIAL_ADMIN_PASSWORD": "TemporaryPass123",
            "START_BACKGROUND": False,
            "ACQUIRE_PROCESS_LOCK": False,
        }
    )
    yield app
    app.extensions["shutdown"]()


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, username="admin", password="TemporaryPass123"):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["user"]


@pytest.fixture()
def admin(client):
    user = login(client)
    # Initial admin is intentionally forced to rotate its password.
    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "TemporaryPass123", "new_password": "TemporaryPass456"},
        headers={"X-CSRF-Token": user["csrf_token"]},
    )
    assert response.status_code == 200, response.get_json()
    user = login(client, password="TemporaryPass456")
    return user


def csrf(user):
    return {"X-CSRF-Token": user["csrf_token"]}

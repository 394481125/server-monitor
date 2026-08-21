from __future__ import annotations

import io
import stat

import paramiko
from cryptography.hazmat.primitives import serialization

from monitor.credentials import CredentialError
from monitor.files import FileManagerError
from monitor.ssh_client import SSHClient

from .conftest import csrf
from .test_hosts_history import host_payload


def _rsa_private() -> str:
    key = paramiko.RSAKey.generate(1024)
    stream = io.StringIO()
    key.write_private_key(stream)
    return stream.getvalue()


def _elevate(client, admin) -> None:
    response = client.post("/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin))
    assert response.status_code == 200


def test_key_vault_encrypts_and_protects_references(client, app, admin):
    _elevate(client, admin)
    private_key = _rsa_private()
    created = client.post(
        "/api/credentials/ssh-keys",
        json={"name": "lab-rsa", "private_key": private_key, "passphrase": "secret-pass"},
        headers=csrf(admin),
    )
    assert created.status_code == 201, created.get_json()
    key = created.get_json()["key"]
    assert key["key_type"] == "rsa" and "private_key" not in key and "passphrase" not in key
    stored = app.extensions["database"].query_one("SELECT private_key,passphrase FROM ssh_keys WHERE id=?", (key["id"],))
    assert stored["private_key"] != private_key
    host = app.extensions["hosts"].create(
        {"name": "vault-host", "address": "10.1.1.1", "port": 22, "username": "u", "auth_type": "key", "ssh_key_id": key["id"]},
        fingerprint="SHA256:vault", machine_id="vault-machine",
    )
    resolved = app.extensions["hosts"].get(host["id"], include_secrets=True)
    assert resolved["private_key"] == stored["private_key"] and resolved["private_key_passphrase"] == stored["passphrase"]
    blocked = client.delete(f"/api/credentials/ssh-keys/{key['id']}", headers=csrf(admin))
    assert blocked.status_code == 400


def test_key_push_script_is_idempotent_and_remote_is_explicit(client, app, admin):
    _elevate(client, admin)
    private_key = _rsa_private()
    key = client.post("/api/credentials/ssh-keys", json={"name": "push", "private_key": private_key}, headers=csrf(admin)).get_json()["key"]
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:push", machine_id="push-machine")
    script = client.post(f"/api/hosts/{host['id']}/key-push", json={"ssh_key_id": key["id"], "mode": "script"}, headers=csrf(admin))
    assert script.status_code == 200
    body = script.get_json()["script"]
    assert "grep -qxF" in body and "authorized_keys" in body and "private" not in body.lower()


def test_generated_key_vault_entries_are_encrypted_and_parseable(client, app, admin):
    _elevate(client, admin)
    for key_type in ("ed25519", "rsa"):
        response = client.post(
            "/api/credentials/ssh-keys/generate",
            json={"name": f"generated-{key_type}", "key_type": key_type, "passphrase": "generated-pass"},
            headers=csrf(admin),
        )
        assert response.status_code == 201, response.get_json()
        item = response.get_json()["key"]
        assert item["key_type"] == key_type
        assert "private_key" not in item and "passphrase" not in item
        stored = app.extensions["database"].query_one("SELECT private_key,passphrase FROM ssh_keys WHERE id=?", (item["id"],))
        assert stored["private_key"] and "BEGIN OPENSSH PRIVATE KEY" not in stored["private_key"]
        private_key = app.extensions["secret_box"].decrypt(stored["private_key"])
        loaded = serialization.load_ssh_private_key(private_key.encode(), password=b"generated-pass")
        assert loaded.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode().startswith(
            "ssh-ed25519" if key_type == "ed25519" else "ssh-rsa"
        )
    duplicate = client.post(
        "/api/credentials/ssh-keys/generate",
        json={"name": "generated-ed25519", "key_type": "ed25519"},
        headers=csrf(admin),
    )
    assert duplicate.status_code == 400


def test_generated_key_requires_authentication_and_valid_type(client, admin):
    assert client.post("/api/credentials/ssh-keys/generate", json={"name": "nope", "key_type": "ed25519"}).status_code in {401, 403}
    _elevate(client, admin)
    invalid = client.post(
        "/api/credentials/ssh-keys/generate",
        json={"name": "invalid", "key_type": "dsa"},
        headers=csrf(admin),
    )
    assert invalid.status_code == 400


def test_unreferenced_key_can_be_deleted(client, app, admin):
    _elevate(client, admin)
    created = client.post(
        "/api/credentials/ssh-keys",
        json={"name": "delete-me", "private_key": _rsa_private()},
        headers=csrf(admin),
    )
    assert created.status_code == 201
    key_id = created.get_json()["key"]["id"]
    deleted = client.delete(f"/api/credentials/ssh-keys/{key_id}", headers=csrf(admin))
    assert deleted.status_code == 200 and deleted.get_json() == {"ok": True}
    assert app.extensions["database"].query_one("SELECT id FROM ssh_keys WHERE id=?", (key_id,)) is None


def test_command_and_directory_favorites_are_scoped(client, app, admin):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:fav", machine_id="fav-machine")
    assert client.post(f"/api/hosts/{host['id']}/command-favorites", json={"name": "blocked", "command": "id"}).status_code == 403
    command = client.post(f"/api/hosts/{host['id']}/command-favorites", json={"name": "disk", "command": "df -h"}, headers=csrf(admin))
    assert command.status_code == 201
    assert client.get(f"/api/hosts/{host['id']}/command-favorites").get_json()["items"][0]["command"] == "df -h"
    directory = client.post(f"/api/hosts/{host['id']}/directory-favorites", json={"name": "data", "path": "/srv/data"}, headers=csrf(admin))
    assert directory.status_code == 201
    assert client.get(f"/api/hosts/{host['id']}/directory-favorites").get_json()["items"][0]["path"] == "/srv/data"


def test_preview_and_script_guards(app):
    service = app.extensions["files"]
    plan = service.permission_script({}, "/tmp/a.txt", mode="640", owner="alice", group="users")
    assert plan["remote_execution"] is False and "chmod 640" in plan["script"]
    transfer = service.transfer_script({"username": "u", "address": "10.0.0.2", "jump_enabled": True, "jump_username": "j", "jump_address": "10.0.0.1", "jump_port": 2222}, "/data/model.bin", local_path="/tmp/model.bin")
    assert "-J" in transfer["script"] and "rsync" in transfer["rsync_script"] and "password" not in transfer["script"]
    try:
        service.permission_script({}, "/tmp/a", mode="7777x")
    except FileManagerError:
        pass
    else:
        raise AssertionError("invalid mode must be rejected")


def test_jump_host_uses_server_side_direct_tcpip(monkeypatch, app):
    class Transport:
        def __init__(self):
            self.key = paramiko.RSAKey.generate(1024)
        def is_active(self): return True
        def get_remote_server_key(self): return self.key
        def open_channel(self, kind, destination, source):
            assert kind == "direct-tcpip" and destination == ("10.0.0.3", 22)
            return object()
    calls = []
    class FakeClient:
        def __init__(self): self.transport = Transport()
        def set_missing_host_key_policy(self, _policy): pass
        def connect(self, **kwargs): calls.append(kwargs)
        def get_transport(self): return self.transport
        def close(self): pass
    monkeypatch.setattr("monitor.ssh_client.paramiko.SSHClient", FakeClient)
    box = app.extensions["secret_box"]
    host = {"address": "10.0.0.3", "port": 22, "username": "target", "auth_type": "password", "auth_secret": box.encrypt("target-pass"), "jump_enabled": True, "jump_address": "10.0.0.1", "jump_port": 2222, "jump_username": "jump", "jump_auth_type": "password", "jump_auth_secret": box.encrypt("jump-pass")}
    client = SSHClient(host, box, app.extensions["monitor_config"].all())
    client.connect()
    assert len(calls) == 2 and calls[0]["hostname"] == "10.0.0.1" and calls[1]["sock"] is not None
    client.close()

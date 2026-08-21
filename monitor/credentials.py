from __future__ import annotations

import io
import shlex
from typing import Any

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from .security import SecretBox
from .ssh_client import host_fingerprint
from .utils import utc_iso


class CredentialError(ValueError):
    pass


class CredentialService:
    """Encrypted SSH key vault and server-side public-key helpers."""

    def __init__(self, database: Any, secrets: SecretBox):
        self.database = database
        self.secrets = secrets

    @staticmethod
    def _parse(private_key: str, passphrase: str | None) -> tuple[str, str, str]:
        if not isinstance(private_key, str) or not private_key.strip():
            raise CredentialError("私钥不能为空")
        errors: list[Exception] = []
        for key_type, label in ((paramiko.RSAKey, "rsa"), (paramiko.Ed25519Key, "ed25519")):
            try:
                key = key_type.from_private_key(io.StringIO(private_key), password=passphrase or None)
                public = f"{key.get_name()} {key.get_base64()}"
                return label, public, host_fingerprint(key)
            except (paramiko.SSHException, ValueError) as exc:
                errors.append(exc)
        raise CredentialError("仅支持 RSA 或 ed25519 私钥，或私钥口令不正确") from errors[-1]

    def list(self) -> list[dict[str, Any]]:
        rows = self.database.query_all("SELECT id,name,key_type,public_key,fingerprint,created_at,updated_at FROM ssh_keys ORDER BY name COLLATE NOCASE")
        return [dict(row) for row in rows]

    def create(self, name: str, private_key: str, passphrase: str | None = None) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name or len(name) > 128:
            raise CredentialError("密钥名称无效")
        if passphrase is not None and not isinstance(passphrase, str):
            raise CredentialError("私钥口令格式无效")
        key_type, public, fingerprint = self._parse(private_key, passphrase)
        now = utc_iso()
        try:
            key_id = self.database.execute(
                "INSERT INTO ssh_keys(name,key_type,private_key,passphrase,public_key,fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (name, key_type, self.secrets.encrypt(private_key), self.secrets.encrypt(passphrase) if passphrase else None, public, fingerprint, now, now),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise CredentialError("密钥名称已存在") from exc
            raise
        row = self.database.query_one("SELECT id,name,key_type,public_key,fingerprint,created_at,updated_at FROM ssh_keys WHERE id=?", (key_id,))
        return dict(row) if row else {}

    def generate(self, name: str, key_type: str, passphrase: str | None = None) -> dict[str, Any]:
        """Generate an OpenSSH private key and store it through the normal encrypted path."""
        key_type = str(key_type or "").strip().lower()
        if key_type not in {"rsa", "ed25519"}:
            raise CredentialError("密钥类型仅支持 RSA 或 ed25519")
        if passphrase is not None and not isinstance(passphrase, str):
            raise CredentialError("私钥口令格式无效")
        if key_type == "rsa":
            private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        else:
            private = ed25519.Ed25519PrivateKey.generate()
        encryption = (
            serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
            if passphrase
            else serialization.NoEncryption()
        )
        private_key = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=encryption,
        ).decode("ascii")
        return self.create(name, private_key, passphrase)

    def delete(self, key_id: int) -> None:
        if self.database.query_one("SELECT id FROM hosts WHERE ssh_key_id=? AND deleted_at IS NULL", (key_id,)):
            raise CredentialError("密钥仍被主机引用，不能删除")
        self.database.execute("DELETE FROM ssh_keys WHERE id=?", (key_id,))

    @staticmethod
    def push_script(public_key: str) -> str:
        if not isinstance(public_key, str) or "\n" in public_key or not public_key.startswith(("ssh-rsa ", "ssh-ed25519 ")):
            raise CredentialError("公钥格式无效")
        key = shlex.quote(public_key)
        home = "$HOME"
        return "#!/bin/sh\nset -eu\numask 077\nmkdir -p %s/.ssh\ntouch %s/.ssh/authorized_keys\nchmod 700 %s/.ssh\ngrep -qxF -- %s %s/.ssh/authorized_keys || printf '%%s\\n' %s >> %s/.ssh/authorized_keys\nchmod 600 %s/.ssh/authorized_keys\n" % (home, home, home, key, home, key, home, home)

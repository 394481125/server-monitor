from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import BinaryIO

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)
SECRET_PATTERN = re.compile(
    r"(?i)(password|passwd|passphrase|sendkey|token|secret)(\s*[=:]\s*)([^\s,;]+)"
)


class PasswordService:
    @staticmethod
    def validate(password: str) -> None:
        if not 10 <= len(password) <= 128:
            raise ValueError("密码长度必须为 10～128 个字符")

    @staticmethod
    def validate_initial(password: str) -> None:
        """首个管理员允许使用内网约定的 8 位引导密码。"""
        if not 8 <= len(password) <= 128:
            raise ValueError("首次引导密码长度必须为 8～128 个字符")

    @classmethod
    def hash(cls, password: str) -> str:
        cls.validate(password)
        return PASSWORD_HASHER.hash(password)

    @classmethod
    def hash_initial(cls, password: str) -> str:
        cls.validate_initial(password)
        return PASSWORD_HASHER.hash(password)

    @staticmethod
    def verify(password_hash: str, password: str) -> bool:
        try:
            return PASSWORD_HASHER.verify(password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False

class SecretBox:
    VERSION = b"\x01"

    def __init__(self, key_path: str | Path):
        self.key_path = Path(key_path)
        self._key = self._load_or_create_key()
        self._cipher = AESGCM(self._key)

    def _load_or_create_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            if self.key_path.stat().st_mode & 0o077:
                raise RuntimeError("主密钥文件权限过宽，必须设置为 0600")
            key = base64.urlsafe_b64decode(self.key_path.read_bytes())
            if len(key) != 32:
                raise RuntimeError("主密钥长度无效")
            return key
        key = AESGCM.generate_key(bit_length=256)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(self.key_path, flags, 0o600)
        try:
            os.write(descriptor, base64.urlsafe_b64encode(key))
        finally:
            os.close(descriptor)
        os.chmod(self.key_path, 0o600)
        if self.key_path.stat().st_mode & 0o077:
            raise RuntimeError("数据目录所在文件系统不支持主密钥 0600 权限，请更换数据目录")
        return key

    def encrypt(self, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, value.encode("utf-8"), self.VERSION)
        return base64.urlsafe_b64encode(self.VERSION + nonce + ciphertext).decode("ascii")

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        version, nonce, ciphertext = raw[:1], raw[1:13], raw[13:]
        if version != self.VERSION:
            raise ValueError("不支持的加密数据版本")
        return self._cipher.decrypt(nonce, ciphertext, version).decode("utf-8")


class ProcessLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.handle: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError("已有 Server Monitor 实例正在使用该数据目录") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self.handle = handle

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def new_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def redact(value: str | None) -> str | None:
    if value is None:
        return None
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}***", value)

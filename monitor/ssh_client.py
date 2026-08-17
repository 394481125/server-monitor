from __future__ import annotations

import io
import base64
import hashlib
import socket
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable

import paramiko


class SSHError(RuntimeError):
    code = "ssh_error"
    remote_started: bool | None = False


class SSHTimeout(SSHError):
    code = "timeout"


class SSHAuthenticationError(SSHError):
    code = "authentication_failed"


class SSHFingerprintError(SSHError):
    code = "fingerprint_changed"

    def __init__(self, message: str, *, expected: str | None = None, observed: str | None = None):
        super().__init__(message)
        self.expected = expected
        self.observed = observed


class SSHConnectionError(SSHError):
    code = "connection_failed"


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def host_fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class SSHClient:
    """A narrow Paramiko wrapper with TOFU checking and bounded command output."""

    def __init__(self, host: dict[str, Any], secret_box: Any, settings: dict[str, Any]):
        self.host = host
        self.secret_box = secret_box
        self.settings = settings
        self.client: paramiko.SSHClient | None = None

    def _pkey(self) -> paramiko.PKey | None:
        encrypted = self.host.get("private_key")
        if not encrypted:
            return None
        key_text = self.secret_box.decrypt(encrypted)
        passphrase = self.secret_box.decrypt(self.host.get("private_key_passphrase"))
        errors: list[Exception] = []
        for key_type in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
            try:
                return key_type.from_private_key(io.StringIO(key_text or ""), password=passphrase)
            except (paramiko.SSHException, ValueError) as exc:
                errors.append(exc)
        raise SSHAuthenticationError("私钥格式无法识别") from errors[-1]

    def connect(self) -> str:
        if self.client:
            transport = self.client.get_transport()
            if transport and transport.is_active():
                return self._fingerprint()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            kwargs: dict[str, Any] = {
                "hostname": self.host["address"],
                "port": int(self.host.get("port") or 22),
                "username": self.host["username"],
                "timeout": self.settings["ssh_connect_timeout"],
                "banner_timeout": self.settings["ssh_connect_timeout"],
                "auth_timeout": self.settings["ssh_connect_timeout"],
                "look_for_keys": False,
                "allow_agent": False,
            }
            if self.host["auth_type"] == "password":
                kwargs["password"] = self.secret_box.decrypt(self.host.get("auth_secret"))
            else:
                kwargs["pkey"] = self._pkey()
            client.connect(**kwargs)
        except paramiko.AuthenticationException as exc:
            client.close()
            raise SSHAuthenticationError("SSH 认证失败") from exc
        except (socket.timeout, TimeoutError) as exc:
            client.close()
            raise SSHTimeout("SSH 连接超时") from exc
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            raise SSHConnectionError(f"SSH 连接失败: {exc}") from exc
        self.client = client
        fingerprint = self._fingerprint()
        expected = self.host.get("fingerprint")
        if expected and expected != fingerprint:
            self.close()
            raise SSHFingerprintError(
                "SSH 主机指纹与已记录值不一致",
                expected=expected,
                observed=fingerprint,
            )
        return fingerprint

    def _fingerprint(self) -> str:
        if not self.client:
            raise SSHConnectionError("SSH 连接尚未建立")
        transport = self.client.get_transport()
        if not transport or not transport.is_active():
            raise SSHConnectionError("SSH 连接已关闭")
        return host_fingerprint(transport.get_remote_server_key())

    def run(
        self,
        command: str,
        timeout: int | float,
        output_limit: int | None = None,
        stdin_data: str | None = None,
    ) -> CommandResult:
        self.connect()
        assert self.client is not None
        limit = output_limit or self.settings.get("schedule_output_limit", 1024 * 1024)
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            if stdin_data is not None:
                stdin.write(stdin_data)
                stdin.flush()
            stdin.close()
            result = self._read_channels(stdout.channel, limit, timeout)
        except (socket.timeout, TimeoutError) as exc:
            raise SSHTimeout("远端命令执行超时") from exc
        except (paramiko.SSHException, OSError) as exc:
            # The command might have reached the remote peer before a transport failure.
            error = SSHConnectionError(f"远端命令通信失败: {exc}")
            error.remote_started = None
            raise error from exc
        return result

    @staticmethod
    def _read_channels(channel: Any, limit: int, timeout: int | float) -> CommandResult:
        stdout = bytearray()
        stderr = bytearray()
        stdout_truncated = False
        stderr_truncated = False
        done = threading.Event()

        def consume() -> None:
            nonlocal stdout_truncated, stderr_truncated
            while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
                if channel.recv_ready():
                    chunk = channel.recv(32768)
                    remaining = max(0, limit - len(stdout))
                    if len(stdout) < limit:
                        stdout.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        stdout_truncated = True
                if channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(32768)
                    remaining = max(0, limit - len(stderr))
                    if len(stderr) < limit:
                        stderr.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        stderr_truncated = True
                if not channel.recv_ready() and not channel.recv_stderr_ready():
                    channel.status_event.wait(0.02)
            done.set()

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        if not done.wait(timeout):
            channel.close()
            raise SSHTimeout("远端命令执行超时")
        return CommandResult(
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            channel.recv_exit_status(),
            stdout_truncated,
            stderr_truncated,
        )

    def open_shell(self, width: int = 120, height: int = 32) -> Any:
        self.connect()
        assert self.client is not None
        transport = self.client.get_transport()
        assert transport is not None
        channel = transport.open_session(timeout=self.settings["ssh_connect_timeout"])
        channel.get_pty(term="xterm-256color", width=width, height=height)
        channel.invoke_shell()
        return channel

    def open_sftp(self) -> Any:
        self.connect()
        assert self.client is not None
        try:
            return self.client.open_sftp()
        except (paramiko.SSHException, OSError) as exc:
            raise SSHConnectionError(f"SFTP 通道建立失败: {exc}") from exc

    def close(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    def is_reusable(self) -> bool:
        if not self.client:
            return False
        transport = self.client.get_transport()
        return bool(transport and transport.is_active())


@dataclass
class _IdleConnection:
    key: tuple[Any, ...]
    host_id: Any
    client: SSHClient
    released_at: float


class SSHConnectionLease(AbstractContextManager[SSHClient]):
    def __init__(self, pool: "SSHConnectionPool", key: tuple[Any, ...], host_id: Any, client: SSHClient):
        self.pool = pool
        self.key = key
        self.host_id = host_id
        self.client = client
        self.closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def __enter__(self) -> SSHClient:
        return self.client

    def __exit__(self, exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close(discard=exc_type is not None)

    def close(self, *, discard: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        self.pool.release(self.key, self.host_id, self.client, discard=discard)


class SSHConnectionPool:
    """Thread-safe pool for idle Paramiko transports; a lease is never shared concurrently."""

    def __init__(
        self,
        secret_box: Any,
        settings_provider: Callable[[], dict[str, Any]],
        *,
        client_factory: Callable[[dict[str, Any], Any, dict[str, Any]], SSHClient] = SSHClient,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.secret_box = secret_box
        self.settings_provider = settings_provider
        self.client_factory = client_factory
        self.clock = clock
        self._idle: list[_IdleConnection] = []
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _key(host: dict[str, Any]) -> tuple[Any, ...]:
        return (
            host.get("id"),
            host.get("address"),
            int(host.get("port") or 22),
            host.get("username"),
            host.get("auth_type"),
            host.get("auth_secret"),
            host.get("private_key"),
            host.get("private_key_passphrase"),
            host.get("fingerprint"),
        )

    def _prune_locked(self, settings: dict[str, Any], now: float) -> None:
        idle_seconds = int(settings.get("ssh_idle_close", 60))
        keep: list[_IdleConnection] = []
        for entry in self._idle:
            if now - entry.released_at >= idle_seconds or not entry.client.is_reusable():
                entry.client.close()
            else:
                keep.append(entry)
        self._idle = keep

    def client(self, host: dict[str, Any]) -> SSHClient | SSHConnectionLease:
        settings = self.settings_provider()
        if not settings.get("ssh_reuse", True):
            return self.client_factory(host, self.secret_box, settings)
        key = self._key(host)
        host_id = host.get("id")
        now = self.clock()
        with self._lock:
            if self._closed:
                return self.client_factory(host, self.secret_box, settings)
            self._prune_locked(settings, now)
            reusable: SSHClient | None = None
            retained: list[_IdleConnection] = []
            for entry in self._idle:
                if entry.host_id == host_id and entry.key != key:
                    entry.client.close()
                elif reusable is None and entry.key == key:
                    reusable = entry.client
                else:
                    retained.append(entry)
            self._idle = retained
        client = reusable or self.client_factory(host, self.secret_box, settings)
        return SSHConnectionLease(self, key, host_id, client)

    def release(
        self,
        key: tuple[Any, ...],
        host_id: Any,
        client: SSHClient,
        *,
        discard: bool = False,
    ) -> None:
        settings = self.settings_provider()
        with self._lock:
            if self._closed or discard or not settings.get("ssh_reuse", True) or not client.is_reusable():
                client.close()
                return
            self._prune_locked(settings, self.clock())
            self._idle.append(_IdleConnection(key, host_id, client, self.clock()))
            maximum = max(1, int(settings.get("ssh_concurrency", 10)))
            while len(self._idle) > maximum:
                self._idle.pop(0).client.close()

    def close_host(self, host_id: Any) -> None:
        with self._lock:
            retained: list[_IdleConnection] = []
            for entry in self._idle:
                if entry.host_id == host_id:
                    entry.client.close()
                else:
                    retained.append(entry)
            self._idle = retained

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for entry in idle:
            entry.client.close()

    def idle_count(self) -> int:
        with self._lock:
            return len(self._idle)

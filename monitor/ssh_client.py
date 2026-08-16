from __future__ import annotations

import io
import base64
import hashlib
import socket
import threading
from dataclasses import dataclass
from typing import Any

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

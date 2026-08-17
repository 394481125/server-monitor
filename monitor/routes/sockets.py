from __future__ import annotations

import codecs
import json
import queue
import shlex
import threading
import time
import uuid
from typing import Any

from flask import request
from flask_sock import Sock

from ..auth import AuthService
from ..ssh_client import SSHClient
from ..web import COOKIE_NAME, WebContext


def _tmux_attach_command(name: str) -> str:
    return (
        "if locale -a 2>/dev/null | grep -Eiq '^(C\\.UTF-8|C\\.utf8|en_US\\.UTF-8|en_US\\.utf8)$'; "
        "then export LANG=C.UTF-8 LC_ALL=C.UTF-8; fi; "
        f"tmux attach-session -t {shlex.quote(name)}\n"
    )


def register_socket_routes(context: WebContext) -> None:
    audit = context.audit
    auth = context.auth
    config = context.config
    hosts = context.hosts
    permission_service = context.permission_service
    secret_box = context.secret_box
    sock = Sock(context.app)

    interactive_lock = threading.RLock()
    interactive_total = 0
    interactive_by_user_host: dict[tuple[int, int], int] = {}

    def interactive_acquire(user_id: int, host_id: int) -> bool:
        nonlocal interactive_total
        key = (user_id, host_id)
        with interactive_lock:
            if interactive_total >= config.all()["interactive_ssh_limit"] or interactive_by_user_host.get(key, 0) >= 2:
                return False
            interactive_total += 1
            interactive_by_user_host[key] = interactive_by_user_host.get(key, 0) + 1
            return True

    def interactive_release(user_id: int, host_id: int) -> None:
        nonlocal interactive_total
        key = (user_id, host_id)
        with interactive_lock:
            interactive_total = max(0, interactive_total - 1)
            if interactive_by_user_host.get(key, 0) <= 1:
                interactive_by_user_host.pop(key, None)
            else:
                interactive_by_user_host[key] -= 1

    def interactive_socket(ws: Any, host_id: int, tmux_name: str | None = None) -> None:
        user = permission_service.decorate(auth.authenticate(request.cookies.get(COOKIE_NAME), touch=False))
        origin = request.headers.get("Origin")
        allowed_origins = {request.host_url.rstrip("/"), request.url_root.rstrip("/")}
        interactive_permission = "tmux.view" if tmux_name else "terminal.open"
        if not user or not permission_service.allowed(user, interactive_permission) or not AuthService.is_elevated(user) or (origin and origin not in allowed_origins):
            ws.close(reason=1008, message="unauthorized")
            return
        host = hosts.get(host_id, include_secrets=True)
        if tmux_name and not host["allow_tmux"]:
            ws.close(reason=1008, message="tmux disabled")
            return
        if not tmux_name and not host["allow_terminal"]:
            ws.close(reason=1008, message="terminal disabled")
            return
        if not interactive_acquire(user["id"], host_id):
            ws.close(reason=1013, message="interactive ssh limit")
            return
        client = SSHClient(host, secret_box, config.all())
        session_id = str(uuid.uuid4())
        channel = None
        output_queue: queue.Queue[bytes] = queue.Queue()
        output_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        output_size = 0
        output_lock = threading.Lock()
        overflow = threading.Event()
        stopped = threading.Event()
        last_activity = time.monotonic()
        reason = "client_closed"

        def read_output() -> None:
            nonlocal output_size
            try:
                while not stopped.is_set() and channel and not channel.closed:
                    if not channel.recv_ready():
                        time.sleep(0.01)
                        continue
                    chunk = channel.recv(65536)
                    with output_lock:
                        if output_size + len(chunk) > 1024 * 1024:
                            overflow.set()
                            return
                        output_size += len(chunk)
                    output_queue.put(chunk)
            except Exception:
                return

        try:
            channel = client.open_shell()
            if tmux_name:
                channel.send(_tmux_attach_command(tmux_name).encode("utf-8"))
            threading.Thread(target=read_output, daemon=True).start()
            audit.write("tmux_attached" if tmux_name else "terminal_connected", actor=user, source_ip=request.remote_addr, target_type="host", target_id=host_id, success=True, summary=f"交互会话 {session_id}")
            while True:
                current = permission_service.decorate(auth.authenticate(request.cookies.get(COOKIE_NAME), touch=False))
                if not current or not permission_service.allowed(current, interactive_permission):
                    reason = "session_invalidated"
                    break
                if overflow.is_set():
                    reason = "output_buffer_limit"
                    break
                if time.monotonic() - last_activity > config.all()["terminal_idle_seconds"]:
                    reason = "idle_timeout"
                    break
                try:
                    chunk = output_queue.get_nowait()
                except queue.Empty:
                    chunk = None
                if chunk is not None:
                    decoded = output_decoder.decode(chunk)
                    if decoded:
                        ws.send(decoded)
                    with output_lock:
                        output_size = max(0, output_size - len(chunk))
                    last_activity = time.monotonic()
                try:
                    message = ws.receive(timeout=0.05)
                except (TimeoutError, queue.Empty):
                    message = None
                if message:
                    last_activity = time.monotonic()
                    try:
                        parsed = json.loads(message)
                    except (TypeError, json.JSONDecodeError):
                        parsed = {"type": "input", "data": message}
                    if parsed.get("type") == "resize":
                        channel.resize_pty(width=max(20, min(500, int(parsed.get("cols", 120)))), height=max(5, min(200, int(parsed.get("rows", 32)))))
                    elif parsed.get("type") == "input":
                        channel.send(str(parsed.get("data", "")).encode("utf-8"))
                if channel.closed or channel.exit_status_ready():
                    reason = "remote_closed"
                    break
        except Exception as exc:
            reason = f"error:{type(exc).__name__}"
        finally:
            stopped.set()
            if channel:
                channel.close()
            client.close()
            interactive_release(user["id"], host_id)
            audit.write("tmux_detached" if tmux_name else "terminal_disconnected", actor=user, source_ip=request.remote_addr, target_type="host", target_id=host_id, success=True, summary=f"交互会话 {session_id} 断开: {reason}")
            try:
                ws.close(reason=1000, message=reason[:123])
            except Exception:
                pass

    @sock.route("/ws/terminal/<int:host_id>")
    def terminal(ws: Any, host_id: int) -> None:
        interactive_socket(ws, host_id)

    @sock.route("/ws/tmux/<int:host_id>/<path:tmux_name>")
    def tmux_terminal(ws: Any, host_id: int, tmux_name: str) -> None:
        interactive_socket(ws, host_id, tmux_name)

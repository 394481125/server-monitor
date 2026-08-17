from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from simple_websocket import Client, ConnectionClosed


COOKIE_NAME = "server_monitor_session"


def session_cookie(cookie_jar: Path) -> str:
    for line in cookie_jar.read_text(encoding="utf-8").splitlines():
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        fields = line.split("\t")
        if len(fields) >= 7 and fields[5] == COOKIE_NAME:
            return fields[6]
    raise RuntimeError(f"{COOKIE_NAME} 不存在于 Cookie 文件")


def expect_unauthorized(base_url: str, host_id: int, headers: dict[str, str] | None = None) -> None:
    websocket = Client.connect(f"{base_url}/ws/terminal/{host_id}", headers=headers)
    try:
        try:
            websocket.receive(timeout=2)
        except ConnectionClosed:
            pass
        if websocket.close_reason != 1008:
            raise AssertionError(f"未认证 WebSocket 关闭码应为 1008，实际为 {websocket.close_reason}")
    finally:
        try:
            websocket.close()
        except ConnectionClosed:
            pass


def run_command(base_url: str, path: str, token: str, marker: str) -> int:
    websocket = Client.connect(
        f"{base_url}{path}",
        # simple-websocket omits non-default ports from its Host header. Leave
        # Origin unset here; browser-based acceptance covers the valid-origin path.
        headers={"Cookie": f"{COOKIE_NAME}={token}"},
    )
    output = ""
    try:
        ready_deadline = time.monotonic() + 1.5
        while time.monotonic() < ready_deadline and websocket.connected:
            try:
                initial = websocket.receive(timeout=0.2)
            except ConnectionClosed:
                break
            if initial is not None:
                output += initial.decode("utf-8", errors="replace") if isinstance(initial, bytes) else initial
        encoded_marker = "".join(f"\\{ord(character):03o}" for character in marker)
        websocket.send(json.dumps({"type": "input", "data": f"printf '{encoded_marker}\\n'\n"}))
        deadline = time.monotonic() + 10
        while marker not in output and time.monotonic() < deadline:
            try:
                message = websocket.receive(timeout=1)
            except ConnectionClosed:
                break
            if message is None:
                if websocket.connected:
                    continue
                break
            output += message.decode("utf-8", errors="replace") if isinstance(message, bytes) else message
    finally:
        try:
            websocket.close()
        except ConnectionClosed:
            pass
    if marker not in output:
        raise AssertionError(f"WebSocket 未返回标记 {marker}，close={websocket.close_reason}，输出={output[:500]!r}")
    return len(output)


def main() -> None:
    parser = argparse.ArgumentParser(description="Server Monitor WebSocket 真实验收")
    parser.add_argument("--base-url", default="ws://127.0.0.1:18080")
    parser.add_argument("--cookie-jar", type=Path, required=True)
    parser.add_argument("--host-id", type=int, required=True)
    parser.add_argument("--tmux-name", required=True)
    args = parser.parse_args()

    token = session_cookie(args.cookie_jar)
    expect_unauthorized(args.base_url, args.host_id)
    expect_unauthorized(
        args.base_url,
        args.host_id,
        {"Cookie": f"{COOKIE_NAME}={token}", "Origin": "http://invalid.example"},
    )
    terminal_bytes = run_command(args.base_url, f"/ws/terminal/{args.host_id}", token, "terminal-ws-ok")
    tmux_bytes = run_command(args.base_url, f"/ws/tmux/{args.host_id}/{args.tmux_name}", token, "tmux-ws-ok")
    print(json.dumps({"unauthorized_close": 1008, "terminal": "passed", "terminal_bytes": terminal_bytes, "tmux_attach": "passed", "tmux_bytes": tmux_bytes}, ensure_ascii=False))


if __name__ == "__main__":
    main()

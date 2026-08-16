#!/usr/bin/env python3
"""Recover a local administrator account without exposing a password in argv."""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from monitor.security import PasswordService  # noqa: E402
from monitor.utils import utc_iso  # noqa: E402


def parse_args() -> argparse.Namespace:
    default_data_dir = Path(os.environ.get("SERVER_MONITOR_DATA_DIR", PROJECT_ROOT / "data"))
    parser = argparse.ArgumentParser(description="重置 Server Monitor 本地管理员密码")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir, help="应用数据目录")
    parser.add_argument("--username", default="admin", help="要恢复的管理员用户名")
    return parser.parse_args()


def read_password() -> str:
    password = getpass.getpass("一次性新密码: ")
    confirmation = getpass.getpass("再次输入: ")
    if password != confirmation:
        raise ValueError("两次输入的密码不一致")
    PasswordService.validate_initial(password)
    return password


def reset_password(database_path: Path, username: str, password: str) -> None:
    if not database_path.is_file():
        raise FileNotFoundError(f"数据库不存在: {database_path}")
    password_hash = PasswordService.hash_initial(password)
    now = utc_iso()
    connection = sqlite3.connect(database_path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        user = connection.execute(
            "SELECT id,role,active FROM users WHERE username=? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
        if not user:
            raise ValueError(f"用户不存在: {username}")
        if user[1] != "admin":
            raise ValueError(f"拒绝恢复非管理员用户: {username}")
        if not user[2]:
            raise ValueError(f"管理员已禁用: {username}")
        connection.execute(
            "UPDATE users SET password_hash=?,must_change_password=1,updated_at=? WHERE id=?",
            (password_hash, now, user[0]),
        )
        connection.execute("DELETE FROM sessions WHERE user_id=?", (user[0],))
        connection.execute("DELETE FROM login_attempts WHERE username=? COLLATE NOCASE", (username.strip(),))
        connection.execute(
            "INSERT INTO audit_logs(ts,user_id,username,source_ip,action,target_type,target_id,request_id,success,summary,error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now, user[0], username.strip(), "local-console", "password_recovered", "user", str(user[0]), None, 1, "管理员密码由本机恢复工具重置", None),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    try:
        password = read_password()
        reset_password(args.data_dir / "server-monitor.sqlite3", args.username, password)
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"重置失败: {exc}", file=sys.stderr)
        return 1
    print(f"管理员 {args.username} 的密码已重置；旧会话已失效，下次登录必须修改密码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from .security import PasswordService, new_token, token_hash
from .utils import future_iso, parse_utc, utc_iso, utc_now


class AuthError(ValueError):
    pass


class LoginLocked(AuthError):
    pass


class AuthService:
    def __init__(self, database: Any, config: Any):
        self.database = database
        self.config = config

    def ensure_initial_admin(self, password: str | None = None) -> str | None:
        if self.database.query_one("SELECT id FROM users LIMIT 1"):
            return None
        if password is None:
            generated = secrets.token_urlsafe(15)
            password_hash = PasswordService.hash(generated)
        else:
            generated = password
            password_hash = PasswordService.hash_initial(generated)
        now = utc_iso()
        self.database.execute(
            "INSERT INTO users(username,password_hash,role,active,must_change_password,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("admin", password_hash, "admin", 1, 1, now, now),
        )
        return generated

    def _attempt_rows(self, username: str, source_ip: str) -> list[Any]:
        return self.database.query_all(
            "SELECT * FROM login_attempts WHERE (username=? OR source_ip=?)",
            (username, source_ip),
        )

    def _check_lock(self, username: str, source_ip: str) -> None:
        now = utc_now()
        for row in self._attempt_rows(username, source_ip):
            locked_until = parse_utc(row["locked_until"])
            if locked_until and locked_until > now:
                raise LoginLocked("登录失败次数过多，请稍后重试")

    def _failed(self, username: str, source_ip: str) -> bool:
        settings = self.config.all()
        now = utc_now()
        row = self.database.query_one(
            "SELECT * FROM login_attempts WHERE username=? AND source_ip=?",
            (username, source_ip),
        )
        window = timedelta(minutes=settings["login_window_minutes"])
        if not row or not parse_utc(row["window_started_at"]) or now - parse_utc(row["window_started_at"]) > window:
            attempts = 1
            window_started = utc_iso(now)
        else:
            attempts = row["attempts"] + 1
            window_started = row["window_started_at"]
        locked_until = None
        cutoff = utc_iso(now - window)
        aggregate = self.database.query_one(
            "SELECT COALESCE(SUM(attempts),0) AS count FROM login_attempts "
            "WHERE (username=? OR source_ip=?) AND window_started_at>=?",
            (username, source_ip, cutoff),
        )["count"]
        if aggregate + 1 >= settings["login_fail_limit"]:
            locked_until = utc_iso(now + timedelta(minutes=settings["login_lock_minutes"]))
        self.database.execute(
            "INSERT INTO login_attempts(username,source_ip,attempts,window_started_at,locked_until) "
            "VALUES(?,?,?,?,?) ON CONFLICT(username,source_ip) DO UPDATE SET "
            "attempts=excluded.attempts,window_started_at=excluded.window_started_at,locked_until=excluded.locked_until",
            (username, source_ip, attempts, window_started, locked_until),
        )
        return locked_until is not None

    def login(self, username: str, password: str, source_ip: str) -> tuple[str, dict[str, Any]]:
        username = username.strip()
        self._check_lock(username, source_ip)
        row = self.database.query_one("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,))
        if not row or not row["active"] or not PasswordService.verify(row["password_hash"], password):
            if self._failed(username, source_ip):
                raise LoginLocked("登录失败次数过多，请稍后重试")
            raise AuthError("用户名或密码错误")
        self.database.execute("DELETE FROM login_attempts WHERE username=? OR source_ip=?", (username, source_ip))
        token = new_token()
        now = utc_iso()
        expires = future_iso(minutes=self.config.all()["session_idle_minutes"])
        csrf = new_token(24)
        self.database.execute(
            "INSERT INTO sessions(token_hash,user_id,csrf_token,source_ip,created_at,last_seen_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (token_hash(token), row["id"], csrf, source_ip, now, now, expires),
        )
        return token, self._user_dict(row, csrf)

    @staticmethod
    def _user_dict(row: Any, csrf: str | None = None) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "theme": row["theme"],
            "must_change_password": bool(row["must_change_password"]),
        }
        if csrf:
            result["csrf_token"] = csrf
        return result

    def authenticate(self, token: str | None, *, touch: bool = True) -> dict[str, Any] | None:
        if not token:
            return None
        row = self.database.query_one(
            "SELECT u.*,s.id session_id,s.csrf_token,s.expires_at,s.elevated_until "
            "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (token_hash(token),),
        )
        if not row or not row["active"] or (parse_utc(row["expires_at"]) or utc_now()) <= utc_now():
            if row:
                self.database.execute("DELETE FROM sessions WHERE id=?", (row["session_id"],))
            return None
        if touch:
            now = utc_iso()
            expires = future_iso(minutes=self.config.all()["session_idle_minutes"])
            self.database.execute(
                "UPDATE sessions SET last_seen_at=?,expires_at=? WHERE id=?",
                (now, expires, row["session_id"]),
            )
        result = self._user_dict(row, row["csrf_token"])
        result.update({"session_id": row["session_id"], "elevated_until": row["elevated_until"]})
        return result

    def require_csrf(self, user: dict[str, Any], supplied: str | None) -> None:
        if not supplied or not secrets.compare_digest(user["csrf_token"], supplied):
            raise AuthError("CSRF 校验失败")

    def elevate(self, user: dict[str, Any], password: str) -> str:
        row = self.database.query_one("SELECT password_hash FROM users WHERE id=?", (user["id"],))
        if not row or not PasswordService.verify(row["password_hash"], password):
            raise AuthError("当前密码错误")
        until = future_iso(minutes=5)
        self.database.execute("UPDATE sessions SET elevated_until=? WHERE id=?", (until, user["session_id"]))
        return until

    @staticmethod
    def is_elevated(user: dict[str, Any]) -> bool:
        until = parse_utc(user.get("elevated_until"))
        return bool(until and until > utc_now())

    def logout(self, token: str | None) -> None:
        if token:
            self.database.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    def change_password(self, user_id: int, current: str, new_password: str) -> None:
        row = self.database.query_one("SELECT password_hash FROM users WHERE id=?", (user_id,))
        if not row or not PasswordService.verify(row["password_hash"], current):
            raise AuthError("当前密码错误")
        new_hash = PasswordService.hash(new_password)
        now = utc_iso()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE users SET password_hash=?,must_change_password=0,updated_at=? WHERE id=?",
                (new_hash, now, user_id),
            )
            connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .collector import CollectionResult, flattened_metrics
from .security import SecretBox
from .utils import (
    clamp_page,
    clamp_page_size,
    json_dump,
    json_load,
    paged,
    parse_utc,
    utc_iso,
    utc_now,
)


ACTIVE_HOST_FIELDS = {
    "name", "address", "port", "username", "auth_type", "tags", "notes", "asset_location", "asset_owner", "warranty_expires", "enabled",
    "docker_enabled", "allow_tmux", "allow_terminal", "allow_process", "allow_install",
    "allow_stress", "timeout_seconds", "scheduler_enabled", "scheduler_idle_seconds",
    "scheduler_process_guard", "schedule_command", "schedule_cwd", "schedule_shell",
    "schedule_env", "schedule_mode",
}
BOOLEAN_HOST_FIELDS = {
    "enabled", "docker_enabled", "allow_tmux", "allow_terminal", "allow_process", "allow_install",
    "allow_stress", "scheduler_enabled", "scheduler_process_guard",
}
SECRET_HOST_FIELDS = {"auth_secret", "private_key", "private_key_passphrase", "sudo_password"}
HOST_TRANSFER_FIELDS = (
    "name", "address", "port", "username", "auth_type", "auth_secret", "private_key",
    "private_key_passphrase", "sudo_password", "tags", "notes", "asset_location", "asset_owner", "warranty_expires", "enabled", "docker_enabled",
    "allow_tmux", "allow_terminal", "allow_process", "allow_install", "allow_stress",
    "timeout_seconds",
)
HOST_IMPORT_LIMIT = 100
HOST_IMPORT_BYTES = 2 * 1024 * 1024


class ServiceError(ValueError):
    pass


class PermissionError(ServiceError):
    pass


def host_transfer_rows(items: Iterable[dict[str, Any]], *, csv_mode: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        row = {key: item.get(key) for key in HOST_TRANSFER_FIELDS if key not in SECRET_HOST_FIELDS}
        if csv_mode:
            row["tags"] = ",".join(item.get("tags") or [])
            for key in SECRET_HOST_FIELDS:
                row[key] = ""
            row = {key: row.get(key, "") for key in HOST_TRANSFER_FIELDS}
        rows.append(row)
    return rows


def _import_boolean(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "是"}:
        return True
    if normalized in {"0", "false", "no", "off", "否"}:
        return False
    raise ServiceError(f"{field} 必须是布尔值")


def parse_host_import(content: bytes, filename: str) -> list[dict[str, Any]]:
    if not content:
        raise ServiceError("导入文件为空")
    if len(content) > HOST_IMPORT_BYTES:
        raise ServiceError("主机导入文件不能超过 2 MiB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ServiceError("导入文件必须使用 UTF-8 编码") from exc
    lower_name = filename.lower()
    if lower_name.endswith(".json"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ServiceError(f"JSON 格式无效: 第 {exc.lineno} 行第 {exc.colno} 列") from exc
        raw_rows = value.get("hosts") if isinstance(value, dict) else value
        if not isinstance(raw_rows, list):
            raise ServiceError("JSON 顶层必须是主机数组或包含 hosts 数组的对象")
    elif lower_name.endswith(".csv"):
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ServiceError("CSV 缺少表头")
        raw_rows = list(reader)
    else:
        raise ServiceError("仅支持 .json 或 .csv 主机导入文件")
    if not raw_rows:
        raise ServiceError("导入文件没有主机记录")
    if len(raw_rows) > HOST_IMPORT_LIMIT:
        raise ServiceError(f"单次最多导入 {HOST_IMPORT_LIMIT} 台主机")

    rows: list[dict[str, Any]] = []
    allowed = set(HOST_TRANSFER_FIELDS)
    boolean_fields = BOOLEAN_HOST_FIELDS & allowed
    for index, raw in enumerate(raw_rows, 1):
        if not isinstance(raw, dict):
            raise ServiceError(f"第 {index} 条主机记录必须是对象")
        unknown = {str(key).strip() for key in raw if str(key).strip()} - allowed
        if unknown:
            raise ServiceError(f"第 {index} 条包含未知字段: {', '.join(sorted(unknown))}")
        row: dict[str, Any] = {}
        for raw_key, raw_value in raw.items():
            key = str(raw_key).strip()
            if not key or raw_value is None:
                continue
            value = raw_value.strip() if isinstance(raw_value, str) else raw_value
            if value == "" and key not in {"notes"}:
                continue
            if key in boolean_fields:
                row[key] = _import_boolean(value, key)
            elif key in {"port", "timeout_seconds"}:
                try:
                    row[key] = int(value)
                except (TypeError, ValueError) as exc:
                    raise ServiceError(f"第 {index} 条的 {key} 必须是整数") from exc
            elif key == "tags":
                if isinstance(value, list):
                    row[key] = value
                elif isinstance(value, str):
                    row[key] = [part.strip() for part in re.split(r"[,;]", value) if part.strip()]
                else:
                    raise ServiceError(f"第 {index} 条的 tags 必须是数组或逗号分隔字符串")
            else:
                row[key] = value
        rows.append(row)
    return rows


class HostService:
    def __init__(self, database: Any, secrets: SecretBox, config: Any):
        self.database = database
        self.secrets = secrets
        self.config = config

    @staticmethod
    def physical_id(fingerprint: str, machine_id: str | None) -> tuple[str, bool]:
        material = f"{fingerprint}|{machine_id or ''}".encode("utf-8")
        return hashlib.sha256(material).hexdigest(), not bool(machine_id)

    @staticmethod
    def _row(row: Any, *, include_secrets: bool = False) -> dict[str, Any]:
        result = dict(row)
        result["tags"] = json_load(result.pop("tags_json", "[]"), [])
        result["schedule_env"] = json_load(result.pop("schedule_env_json", "{}"), {})
        for key in BOOLEAN_HOST_FIELDS | {"identity_degraded"}:
            if key in result:
                result[key] = None if key == "scheduler_process_guard" and result[key] is None else bool(result[key])
        if not result.get("enabled") and "status" in result:
            result["status"] = "disabled"
        if not include_secrets:
            for key in SECRET_HOST_FIELDS:
                if key in result:
                    result[f"{key}_configured"] = bool(result.pop(key))
        return result

    def list(self, *, include_deleted: bool = False, search: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses = ["h.deleted_at IS NULL" if not include_deleted else "1=1"]
        params: list[Any] = []
        if search:
            clauses.append("(h.name LIKE ? OR h.address LIKE ? OR h.tags_json LIKE ?)")
            params.extend([f"%{search}%"] * 3)
        if status:
            clauses.append("r.status=?")
            params.append(status)
        rows = self.database.query_all(
            "SELECT h.*,r.status,r.failure_cycles,r.last_success_at,r.last_attempt_at,r.last_error,r.error_code,r.updated_at AS runtime_updated_at "
            "FROM hosts h LEFT JOIN host_runtime r ON r.host_id=h.id WHERE " + " AND ".join(clauses) + " ORDER BY h.name COLLATE NOCASE, h.id",
            params,
        )
        return [self._row(row) for row in rows]

    def get(self, host_id: int, *, include_secrets: bool = False, required: bool = True) -> dict[str, Any] | None:
        row = self.database.query_one(
            "SELECT h.*,r.status,r.failure_cycles,r.last_success_at,r.last_attempt_at,r.last_error,r.error_code,r.updated_at AS runtime_updated_at "
            "FROM hosts h LEFT JOIN host_runtime r ON r.host_id=h.id WHERE h.id=? AND h.deleted_at IS NULL",
            (host_id,),
        )
        if not row:
            if required:
                raise ServiceError("主机不存在或已删除")
            return None
        return self._row(row, include_secrets=include_secrets)

    def _validate(self, payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
        unknown = set(payload) - ACTIVE_HOST_FIELDS - SECRET_HOST_FIELDS
        if unknown:
            raise ServiceError(f"未知主机字段: {', '.join(sorted(unknown))}")
        result: dict[str, Any] = {}
        required = {"name", "address", "username", "auth_type"}
        if not partial and not required.issubset(payload):
            raise ServiceError("主机名称、地址、用户名和认证方式不能为空")
        for key, value in payload.items():
            if key in {"name", "address", "username"}:
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > 255:
                    raise ServiceError(f"{key} 无效")
                result[key] = value.strip()
            elif key == "port":
                try:
                    value = int(value)
                except (ValueError, TypeError) as exc:
                    raise ServiceError("SSH 端口无效") from exc
                if not 1 <= value <= 65535:
                    raise ServiceError("SSH 端口必须在 1～65535 之间")
                result[key] = value
            elif key == "auth_type":
                if value not in {"password", "key"}:
                    raise ServiceError("认证方式仅支持 password 或 key")
                result[key] = value
            elif key in BOOLEAN_HOST_FIELDS:
                if key == "scheduler_process_guard" and value is None:
                    result[key] = None
                    continue
                if not isinstance(value, bool):
                    raise ServiceError(f"{key} 必须是布尔值")
                result[key] = int(value)
            elif key == "timeout_seconds":
                if value is None or value == "":
                    result[key] = None
                else:
                    value = int(value)
                    if not 5 <= value <= 60:
                        raise ServiceError("主机采集超时必须在 5～60 秒之间")
                    result[key] = value
            elif key == "scheduler_idle_seconds":
                if value is None or value == "":
                    result[key] = None
                else:
                    value = int(value)
                    if not 60 <= value <= 86400:
                        raise ServiceError("GPU 空闲时长必须在 60～86400 秒之间")
                    result[key] = value
            elif key == "tags":
                if not isinstance(value, list) or any(not isinstance(tag, str) or not tag.strip() for tag in value):
                    raise ServiceError("标签必须是非空字符串数组")
                result["tags_json"] = json_dump(sorted(set(tag.strip()[:64] for tag in value)))
            elif key == "notes":
                if not isinstance(value, str) or len(value) > 4000:
                    raise ServiceError("备注无效")
                result[key] = value
            elif key in {"asset_location", "asset_owner"}:
                if not isinstance(value, str) or len(value.strip()) > 255:
                    raise ServiceError(f"{key} 无效")
                result[key] = value.strip()
            elif key == "warranty_expires":
                if value in {None, ""}:
                    result[key] = None
                elif not isinstance(value, str) or len(value) != 10:
                    raise ServiceError("warranty_expires 必须使用 YYYY-MM-DD 格式")
                else:
                    try:
                        datetime.strptime(value, "%Y-%m-%d")
                    except ValueError as exc:
                        raise ServiceError("warranty_expires 日期无效") from exc
                    result[key] = value
            elif key in {"schedule_command", "schedule_cwd", "schedule_shell"}:
                if value is not None and not isinstance(value, str):
                    raise ServiceError(f"{key} 必须是字符串")
                value = (value or "").strip()
                if key == "schedule_command" and value and len(value) > 500:
                    raise ServiceError("调度命令最长 500 个字符")
                if key == "schedule_shell" and not value:
                    value = "/bin/bash"
                result[key] = value if key == "schedule_shell" else (value or None)
            elif key == "schedule_env":
                if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                    raise ServiceError("调度环境变量必须是字符串键值对象")
                result["schedule_env_json"] = json_dump(value)
            elif key == "schedule_mode":
                if value not in {"tmux", "direct"}:
                    raise ServiceError("调度模式仅支持 tmux 或 direct")
                result[key] = value
            elif key == "sudo_password":
                if value is not None and (not isinstance(value, str) or "\n" in value or "\r" in value):
                    raise ServiceError("sudo_password 必须是不含换行符的字符串")
                result[key] = self.secrets.encrypt(value) if value else None
            elif key in SECRET_HOST_FIELDS:
                if value is not None and not isinstance(value, str):
                    raise ServiceError(f"{key} 必须是字符串")
                result[key] = self.secrets.encrypt(value) if value else None
        return result

    def create(self, payload: dict[str, Any], *, fingerprint: str, machine_id: str | None) -> dict[str, Any]:
        clean = self._validate(payload, partial=False)
        if clean.get("auth_type") == "password" and not clean.get("auth_secret"):
            raise ServiceError("密码认证必须提供 SSH 密码")
        if clean.get("auth_type") == "key" and not clean.get("private_key"):
            raise ServiceError("私钥认证必须提供私钥")
        if clean.get("auth_type") == "password":
            clean["private_key"] = None
            clean["private_key_passphrase"] = None
        else:
            clean["auth_secret"] = None
        physical_id, degraded = self.physical_id(fingerprint, machine_id)
        now = utc_iso()
        values = {
            "port": 22, "tags_json": "[]", "notes": "", "asset_location": "", "asset_owner": "", "warranty_expires": None, "enabled": 1, "docker_enabled": 1,
            "allow_tmux": 1, "allow_terminal": 1, "allow_process": 1, "allow_install": 1,
            "allow_stress": 1, "schedule_shell": "/bin/bash", "schedule_env_json": "{}", "schedule_mode": "tmux",
            **clean, "fingerprint": fingerprint, "machine_id": machine_id, "physical_id": physical_id,
            "identity_degraded": int(degraded), "created_at": now, "updated_at": now,
        }
        columns, params = zip(*values.items())
        try:
            host_id = self.database.execute(
                f"INSERT INTO hosts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", params
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "uq_active_host_endpoint" in message or "hosts.address" in message or "hosts.port" in message:
                raise ServiceError("该地址和 SSH 端口已被纳管") from exc
            if "uq_active_physical_host" in message or "hosts.physical_id" in message:
                raise ServiceError("该物理主机已被纳管，请编辑已有主机") from exc
            raise
        self.database.execute(
            "INSERT INTO host_runtime(host_id,status,updated_at) VALUES(?,?,?)", (host_id, "unknown", now)
        )
        return self.get(host_id)

    def update(self, host_id: int, payload: dict[str, Any], *, fingerprint: str | None = None, machine_id: str | None = None) -> dict[str, Any]:
        current = self.get(host_id, include_secrets=True)
        clean = self._validate(payload, partial=True)
        identity_changes = {"address", "port", "username", "auth_type", "auth_secret", "private_key", "private_key_passphrase"} & set(clean)
        if identity_changes and not fingerprint:
            raise ServiceError("修改连接信息时必须先重新测试 SSH 连接")
        if fingerprint:
            physical_id, degraded = self.physical_id(fingerprint, machine_id)
            clean.update({"fingerprint": fingerprint, "machine_id": machine_id, "physical_id": physical_id, "identity_degraded": int(degraded)})
        if not clean:
            return self.get(host_id)
        if "auth_type" in clean:
            auth_type = clean["auth_type"]
            password = clean["auth_secret"] if "auth_secret" in clean else current.get("auth_secret")
            private_key = clean["private_key"] if "private_key" in clean else current.get("private_key")
            if auth_type == "password" and not password:
                raise ServiceError("密码认证必须保留或提供 SSH 密码")
            if auth_type == "key" and not private_key:
                raise ServiceError("私钥认证必须保留或提供私钥")
            if auth_type == "password":
                clean["private_key"] = None
                clean["private_key_passphrase"] = None
            else:
                clean["auth_secret"] = None
        clean["updated_at"] = utc_iso()
        try:
            self.database.execute(
                "UPDATE hosts SET " + ",".join(f"{key}=?" for key in clean) + " WHERE id=?",
                [*clean.values(), host_id],
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError("主机地址或物理身份与现有有效主机重复") from exc
        if {"scheduler_enabled", "schedule_command", "scheduler_idle_seconds", "scheduler_process_guard"} & set(clean):
            self.reset_gpu_runtime(host_id, "主机调度配置已修改")
        if "enabled" in clean and not clean["enabled"]:
            self.mark_gpu_unknown(host_id, "主机采集已禁用")
        return self.get(host_id)

    def soft_delete(self, host_id: int) -> None:
        self.get(host_id)
        now = utc_iso()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE hosts SET deleted_at=?,auth_secret=NULL,private_key=NULL,private_key_passphrase=NULL,"
                "sudo_password=NULL,enabled=0,scheduler_enabled=0,updated_at=? WHERE id=?",
                (now, now, host_id),
            )
            connection.execute("UPDATE gpu_configs SET active=0,updated_at=? WHERE host_id=?", (now, host_id))
            connection.execute("UPDATE gpu_runtime SET state='unknown',idle_seconds_accum=0,updated_at=? WHERE host_id=?", (now, host_id))

    def update_tags(self, host_id: int, add: list[str], remove: list[str]) -> dict[str, Any]:
        host = self.get(host_id)
        tags = set(host["tags"])
        tags.update(tag.strip()[:64] for tag in add if isinstance(tag, str) and tag.strip())
        tags.difference_update(tag.strip() for tag in remove if isinstance(tag, str))
        return self.update(host_id, {"tags": sorted(tags)})

    def mount_thresholds(self, host_id: int) -> list[dict[str, Any]]:
        self.get(host_id)
        return [dict(row) for row in self.database.query_all(
            "SELECT mountpoint,usage_threshold,inode_threshold,updated_at FROM mount_alert_thresholds WHERE host_id=? ORDER BY mountpoint",
            (host_id,),
        )]

    def replace_mount_thresholds(self, host_id: int, rules: Any) -> list[dict[str, Any]]:
        self.get(host_id)
        if not isinstance(rules, list) or len(rules) > 128:
            raise ServiceError("mountpoint 阈值规则必须是最多 128 项的数组")
        cleaned: list[tuple[str, float, float | None]] = []
        seen: set[str] = set()
        hysteresis = float(self.config.all()["alert_hysteresis"])
        for item in rules:
            if not isinstance(item, dict):
                raise ServiceError("mountpoint 阈值规则必须是对象")
            mountpoint = item.get("mountpoint")
            if not isinstance(mountpoint, str) or not mountpoint.startswith("/") or len(mountpoint) > 1024:
                raise ServiceError("mountpoint 必须是有效的绝对路径")
            mountpoint = mountpoint.rstrip("/") or "/"
            if mountpoint in seen:
                raise ServiceError("mountpoint 阈值规则不能重复")
            seen.add(mountpoint)
            try:
                usage_threshold = float(item.get("usage_threshold"))
            except (TypeError, ValueError) as exc:
                raise ServiceError("容量阈值必须是数字") from exc
            inode_raw = item.get("inode_threshold")
            try:
                inode_threshold = None if inode_raw is None or inode_raw == "" else float(inode_raw)
            except (TypeError, ValueError) as exc:
                raise ServiceError("inode 阈值必须是数字") from exc
            if not 0 < usage_threshold <= 100 or inode_threshold is not None and not 0 < inode_threshold <= 100:
                raise ServiceError("挂载点阈值必须在 0～100 之间")
            if hysteresis > usage_threshold or inode_threshold is not None and hysteresis > inode_threshold:
                raise ServiceError("挂载点阈值不得小于告警恢复回差")
            cleaned.append((mountpoint, usage_threshold, inode_threshold))
        now = utc_iso()
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM mount_alert_thresholds WHERE host_id=?", (host_id,))
            connection.executemany(
                "INSERT INTO mount_alert_thresholds(host_id,mountpoint,usage_threshold,inode_threshold,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                [(host_id, mountpoint, usage, inode, now, now) for mountpoint, usage, inode in cleaned],
            )
        return self.mount_thresholds(host_id)

    def status(self, host_id: int, status: str, *, failure_cycles: int | None = None, error: str | None = None, error_code: str | None = None, success: bool = False) -> None:
        now = utc_iso()
        existing = self.database.query_one("SELECT failure_cycles FROM host_runtime WHERE host_id=?", (host_id,))
        failure_cycles = existing["failure_cycles"] if failure_cycles is None and existing else failure_cycles or 0
        self.database.execute(
            "INSERT INTO host_runtime(host_id,status,failure_cycles,last_success_at,last_attempt_at,last_error,error_code,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(host_id) DO UPDATE SET status=excluded.status,"
            "failure_cycles=excluded.failure_cycles,last_success_at=COALESCE(excluded.last_success_at,host_runtime.last_success_at),"
            "last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error,error_code=excluded.error_code,updated_at=excluded.updated_at",
            (host_id, status, failure_cycles, now if success else None, now, error, error_code, now),
        )

    def ingest_collection(self, host_id: int, result: CollectionResult) -> dict[str, Any]:
        host = self.get(host_id)
        if not host["enabled"]:
            self.status(host_id, "disabled", error="采集已禁用")
            self.mark_gpu_unknown(host_id, "主机采集已禁用")
            return {"status": "disabled"}
        if not result.core_ok:
            if result.error_code == "fingerprint_changed" or (result.error and "指纹" in result.error):
                self.status(host_id, "fingerprint_error", error=result.error, error_code="fingerprint_changed")
                self.mark_gpu_unknown(host_id, "SSH 指纹异常")
                return {"status": "fingerprint_error", "error": result.error}
            runtime = self.database.query_one("SELECT failure_cycles FROM host_runtime WHERE host_id=?", (host_id,))
            cycles = (runtime["failure_cycles"] if runtime else 0) + 1
            specific_status = {
                "authentication_failed": "auth_failed",
                "timeout": "collection_timeout",
                "connection_failed": "ssh_unreachable",
                "remote_command_failed": "command_error",
            }.get(result.error_code)
            status = specific_status or ("offline" if cycles >= 3 else "unknown")
            self.status(host_id, status, failure_cycles=cycles, error=result.error, error_code=result.error_code)
            if status == "offline" or specific_status:
                self.mark_gpu_unknown(host_id, "主机离线")
            return {"status": status, "failure_cycles": cycles, "error": result.error, "error_code": result.error_code}
        if result.fingerprint and host.get("fingerprint") and result.fingerprint != host["fingerprint"]:
            self.status(host_id, "fingerprint_error", error="SSH 主机指纹与已记录值不一致", error_code="fingerprint_changed")
            self.mark_gpu_unknown(host_id, "SSH 指纹异常")
            return {"status": "fingerprint_error"}
        data = result.data
        now = data["collected_at"]
        status = "gpu_error" if "gpu" in result.optional_errors else "degraded" if result.optional_errors else "online"
        optional_summary = "; ".join(f"{key}: {value}" for key, value in result.optional_errors.items()) or None
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO latest_samples(host_id,collected_at,data_json) VALUES(?,?,?) ON CONFLICT(host_id) "
                "DO UPDATE SET collected_at=excluded.collected_at,data_json=excluded.data_json",
                (host_id, now, json_dump(data)),
            )
            connection.execute(
                "INSERT INTO host_runtime(host_id,status,failure_cycles,last_success_at,last_attempt_at,last_error,error_code,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(host_id) DO UPDATE SET status=excluded.status,failure_cycles=0,"
                "last_success_at=excluded.last_success_at,last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error,error_code=excluded.error_code,updated_at=excluded.updated_at",
                (host_id, status, 0, now, now, optional_summary, "gpu_command_failed" if status == "gpu_error" else None, now),
            )
            for metric, object_key, value in flattened_metrics(data):
                connection.execute(
                    "INSERT INTO metric_points(host_id,metric,object_key,kind,ts,value) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(host_id,metric,object_key,kind,ts) DO UPDATE SET value=excluded.value",
                    (host_id, metric, object_key, "raw", now, value),
                )
        return {"status": status, "data": data}

    def latest(self, host_id: int) -> dict[str, Any] | None:
        row = self.database.query_one("SELECT * FROM latest_samples WHERE host_id=?", (host_id,))
        if not row:
            return None
        return {"collected_at": row["collected_at"], "data": json_load(row["data_json"], {})}

    def mark_busy(self, host_id: int, reason: str = "采集繁忙") -> None:
        host = self.get(host_id, required=False)
        if host and host["enabled"]:
            self.status(host_id, "busy", error=reason)
            self.mark_gpu_unknown(host_id, reason)

    def mark_gpu_unknown(self, host_id: int, reason: str) -> None:
        self.database.execute(
            "UPDATE gpu_runtime SET state='unknown',idle_seconds_accum=0,last_error=?,updated_at=? WHERE host_id=?",
            (reason, utc_iso(), host_id),
        )

    def reset_gpu_runtime(self, host_id: int, reason: str) -> None:
        self.mark_gpu_unknown(host_id, reason)

    def history(self, host_id: int, metric: str, object_key: str, start: str, end: str, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self.database.query_all(
                "SELECT ts,value,max_value FROM metric_points WHERE host_id=? AND metric=? AND object_key=? AND kind=? "
                "AND ts>=? AND ts<=? ORDER BY ts",
                (host_id, metric, object_key, kind, start, end),
            )
        else:
            # Each rollup transaction inserts the target bucket before deleting
            # its source rows, so combining tiers avoids gaps around retention cutoffs.
            rows = self.database.query_all(
                "SELECT ts,value,max_value FROM metric_points WHERE host_id=? AND metric=? AND object_key=? "
                "AND kind IN ('raw','mid','long') AND ts>=? AND ts<=? ORDER BY ts",
                (host_id, metric, object_key, start, end),
            )
        return [dict(row) for row in rows]


class HistoryService:
    def __init__(self, database: Any):
        self.database = database

    def aggregate(
        self,
        *,
        now: Any | None = None,
        mid_seconds: int = 60,
        long_seconds: int = 300,
        raw_retention_minutes: int = 15,
        mid_retention_hours: int = 6,
    ) -> dict[str, int]:
        now = now or utc_now()
        counts = {
            "mid": self._aggregate_kind("raw", "mid", mid_seconds, now - timedelta(minutes=raw_retention_minutes)),
            "long": self._aggregate_kind("mid", "long", long_seconds, now - timedelta(hours=mid_retention_hours)),
        }
        return counts

    def _aggregate_kind(self, source_kind: str, target_kind: str, bucket_seconds: int, before: Any) -> int:
        rows = self.database.query_all(
            "SELECT id,host_id,metric,object_key,ts,value FROM metric_points WHERE kind=? AND ts<? ORDER BY ts",
            (source_kind, utc_iso(before)),
        )
        buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            timestamp = parse_utc(row["ts"])
            if timestamp is None or row["value"] is None:
                continue
            epoch = int(timestamp.timestamp())
            bucket = epoch - epoch % bucket_seconds
            if datetime.fromtimestamp(bucket + bucket_seconds, UTC) > before:
                continue
            key = (row["host_id"], row["metric"], row["object_key"], bucket)
            entry = buckets.setdefault(key, {"values": [], "ids": []})
            entry["values"].append(float(row["value"]))
            entry["ids"].append(row["id"])
        if not buckets:
            return 0
        with self.database.transaction() as connection:
            source_ids: list[int] = []
            for (host_id, metric, object_key, bucket), entry in buckets.items():
                values = entry["values"]
                connection.execute(
                    "INSERT INTO metric_points(host_id,metric,object_key,kind,ts,value,max_value) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(host_id,metric,object_key,kind,ts) DO UPDATE SET value=excluded.value,max_value=excluded.max_value",
                    (host_id, metric, object_key, target_kind, utc_iso(datetime.fromtimestamp(bucket, UTC)), sum(values) / len(values), max(values)),
                )
                source_ids.extend(entry["ids"])
            for offset in range(0, len(source_ids), 500):
                batch = source_ids[offset:offset + 500]
                connection.execute("DELETE FROM metric_points WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch)
        return len(buckets)

    def cleanup(
        self,
        *,
        metric_retention_days: int,
        log_retention_days: int,
        collection_task_retention_minutes: int = 60,
        now: Any | None = None,
    ) -> dict[str, int]:
        now = now or utc_now()
        metric_before = utc_iso(now - timedelta(days=metric_retention_days))
        log_before = utc_iso(now - timedelta(days=log_retention_days))
        collection_before = utc_iso(now - timedelta(minutes=collection_task_retention_minutes))
        session_before = utc_iso(now)
        with self.database.transaction() as connection:
            metrics = connection.execute("DELETE FROM metric_points WHERE ts<?", (metric_before,)).rowcount
            logs = connection.execute("DELETE FROM audit_logs WHERE ts<?", (log_before,)).rowcount
            sessions = connection.execute("DELETE FROM sessions WHERE expires_at<?", (session_before,)).rowcount
            collection_tasks = connection.execute("DELETE FROM tasks WHERE task_type='collection' AND created_at<?", (collection_before,)).rowcount
            schedule_jobs = connection.execute(
                "DELETE FROM schedule_jobs WHERE state NOT IN ('running','retry_wait') AND COALESCE(finished_at,started_at)<?",
                (log_before,),
            ).rowcount
            notifications = connection.execute("DELETE FROM notifications WHERE created_at<?", (log_before,)).rowcount
            recovered_alerts = connection.execute("DELETE FROM alerts WHERE state='recovered' AND recovered_at<?", (log_before,)).rowcount
            stress_jobs = connection.execute("DELETE FROM stress_jobs WHERE finished_at IS NOT NULL AND finished_at<?", (log_before,)).rowcount
        return {
            "metrics": metrics,
            "logs": logs,
            "sessions": sessions,
            "collection_tasks": collection_tasks,
            "schedule_jobs": schedule_jobs,
            "notifications": notifications,
            "recovered_alerts": recovered_alerts,
            "stress_jobs": stress_jobs,
        }


def compact_collection_result(outcome: dict[str, Any]) -> dict[str, Any]:
    """Keep task polling useful without duplicating a full sample in SQLite."""
    data = outcome.get("data") if isinstance(outcome, dict) else None
    return {
        "status": outcome.get("status", "unknown") if isinstance(outcome, dict) else "unknown",
        "collected_at": data.get("collected_at") if isinstance(data, dict) else None,
        "failure_cycles": outcome.get("failure_cycles") if isinstance(outcome, dict) else None,
    }


class AlertService:
    def __init__(self, database: Any, config: Any):
        self.database = database
        self.config = config
        self.notifier: Any | None = None

    def emit(self, key: str, host_id: int | None, alert_type: str, severity: str, summary: str, *, state: str = "active") -> int | None:
        existing = self.database.query_one(
            "SELECT * FROM alerts WHERE alert_key=? AND state='active' AND cleared_at IS NULL ORDER BY id DESC LIMIT 1", (key,)
        )
        now = utc_iso()
        if state == "recovered":
            if not existing:
                return None
            self.database.execute("UPDATE alerts SET state='recovered',recovered_at=? WHERE id=?", (now, existing["id"]))
            return existing["id"]
        if existing:
            return existing["id"]
        alert_id = self.database.execute(
            "INSERT INTO alerts(alert_key,host_id,alert_type,state,severity,summary,created_at) VALUES(?,?,?,?,?,?,?)",
            (key, host_id, alert_type, "active", severity, summary, now),
        )
        if self.notifier:
            self.notifier.notify(alert_id)
        return alert_id

    def evaluate_host(self, host_id: int, status: str, data: dict[str, Any] | None = None) -> None:
        failure_labels = {
            "offline": "主机连续三个采集周期失败",
            "ssh_unreachable": "SSH 网络连接失败",
            "auth_failed": "SSH 密码或密钥认证失败",
            "collection_timeout": "SSH 采集命令超时",
            "command_error": "远端核心采集命令执行失败",
        }
        if status in failure_labels:
            self.emit(f"host-offline:{host_id}", host_id, "host_offline", "critical", failure_labels[status])
        elif status == "fingerprint_error":
            self.emit(f"host-fingerprint:{host_id}", host_id, "ssh_fingerprint_changed", "critical", "SSH 主机指纹异常，连接已停止")
        elif status in {"online", "degraded", "gpu_error"}:
            self.emit(f"host-offline:{host_id}", host_id, "host_online", "info", "主机已恢复在线", state="recovered")
            self.emit(f"host-fingerprint:{host_id}", host_id, "ssh_fingerprint_recovered", "info", "SSH 主机指纹已确认", state="recovered")
        if not data:
            return
        settings = self.config.all()
        memory = data.get("memory") or {}
        if float(memory.get("swap_total") or 0) > 0:
            self._utilization(
                host_id, "swap_usage", "", "swap_usage_high", "swap_usage_recovered",
                "Swap", memory.get("swap_usage_percent"), float(settings["swap_usage_threshold"]), settings,
            )
        checks: list[tuple[str, str, float | None, float]] = [("cpu_temperature", "CPU", data.get("cpu_temperature_c"), settings["cpu_temp_threshold"])]
        for gpu in data.get("gpus", []):
            checks.append((f"gpu_temperature:{gpu['uuid']}", f"GPU {gpu['uuid'][:12]}", gpu.get("temperature_c"), settings["gpu_temp_threshold"]))
            self._gpu_health(host_id, gpu, settings)
        for disk in data.get("smart", []):
            checks.append((f"disk_temperature:{disk['device']}", f"磁盘 {disk['device']}", disk.get("temperature_c"), settings["disk_temp_threshold"]))
        for key, title, value, limit in checks:
            if value is None:
                continue
            self._temperature(host_id, key, title, value, limit, settings)
        overrides = {
            row["mountpoint"]: dict(row)
            for row in self.database.query_all(
                "SELECT mountpoint,usage_threshold,inode_threshold FROM mount_alert_thresholds WHERE host_id=?",
                (host_id,),
            )
        }
        for filesystem in data.get("filesystems", []):
            mountpoint = filesystem.get("mountpoint")
            if not isinstance(mountpoint, str):
                continue
            rule = overrides.get(mountpoint, {})
            usage_limit = float(rule.get("usage_threshold", settings["filesystem_usage_threshold"]))
            self._utilization(
                host_id, "filesystem_usage", mountpoint, "filesystem_usage_high", "filesystem_usage_recovered",
                f"挂载点 {mountpoint} 容量", filesystem.get("usage_percent"), usage_limit, settings,
            )
            inode_limit = rule.get("inode_threshold")
            if inode_limit is None:
                inode_limit = settings["filesystem_inode_threshold"]
            self._utilization(
                host_id, "filesystem_inode_usage", mountpoint, "filesystem_inode_high", "filesystem_inode_recovered",
                f"挂载点 {mountpoint} inode", filesystem.get("inode_usage_percent"), float(inode_limit), settings,
            )

    def _gpu_health(self, host_id: int, gpu: dict[str, Any], settings: dict[str, Any]) -> None:
        uuid = str(gpu.get("uuid") or "unknown")
        title = f"GPU {gpu.get('index', '?')}"

        def state_alert(suffix: str, active: bool, alert_type: str, summary: str, recovered_summary: str, severity: str = "warning") -> None:
            key = f"gpu-health:{host_id}:{uuid}:{suffix}"
            if active:
                self.emit(key, host_id, alert_type, severity, summary)
            else:
                self.emit(key, host_id, f"{alert_type}_recovered", "info", recovered_summary, state="recovered")

        power = gpu.get("power_w")
        power_limit = gpu.get("power_limit_w")
        power_percent = float(power) * 100 / float(power_limit) if power is not None and power_limit else None
        state_alert(
            "power",
            power_percent is not None and power_percent >= settings["gpu_power_threshold_percent"],
            "gpu_power_high",
            f"{title} 功耗 {float(power or 0):.1f}W 达到功耗上限的 {float(power_percent or 0):.1f}%",
            f"{title} 功耗恢复正常",
        )
        fan = gpu.get("fan_percent")
        temperature = gpu.get("temperature_c")
        fan_low = fan is not None and temperature is not None and float(temperature) >= settings["gpu_fan_alert_temperature"] and float(fan) <= settings["gpu_fan_min_percent"]
        state_alert("fan", fan_low, "gpu_fan_low", f"{title} 温度 {float(temperature or 0):.1f}C 但风扇仅 {float(fan or 0):.1f}%", f"{title} 风扇状态恢复正常")
        corrected = float(gpu.get("ecc_corrected") or 0)
        uncorrected = float(gpu.get("ecc_uncorrected") or 0)
        ecc_bad = uncorrected > 0 or corrected >= settings["gpu_ecc_corrected_threshold"]
        state_alert("ecc", ecc_bad, "gpu_ecc_error", f"{title} ECC 错误：可纠正 {corrected:g}，不可纠正 {uncorrected:g}", f"{title} ECC 状态恢复正常", "critical" if uncorrected > 0 else "warning")
        xid_events = gpu.get("xid_errors") or []
        xid_codes = sorted({str(event.get("code")) for event in xid_events})
        state_alert("xid", settings["gpu_xid_alert_enabled"] and bool(xid_events), "gpu_xid_error", f"{title} 检测到 XID 错误：{', '.join(xid_codes)}", f"{title} 最近未检测到 XID 错误", "critical")
        state_alert(
            "pcie",
            settings["gpu_pcie_alert_enabled"] and bool(gpu.get("pcie_degraded")),
            "gpu_pcie_degraded",
            f"{title} PCIe 链路为 Gen{gpu.get('pcie_gen')} x{gpu.get('pcie_width')}，低于最大 Gen{gpu.get('pcie_gen_max')} x{gpu.get('pcie_width_max')}",
            f"{title} PCIe 链路恢复正常",
        )
        reasons = gpu.get("throttle_reasons") or []
        state_alert(
            "throttle",
            settings["gpu_throttle_alert_enabled"] and bool(reasons),
            "gpu_throttling",
            f"{title} 正在节流：{', '.join(str(reason) for reason in reasons)}",
            f"{title} 节流状态已解除",
        )
        state_alert(
            "residual-memory",
            settings["gpu_residual_alert_enabled"] and bool(gpu.get("residual_memory_suspected")),
            "gpu_residual_memory",
            f"{title} 疑似存在残留显存：未归属 {float(gpu.get('residual_memory_mib') or 0):.1f} MiB",
            f"{title} 残留显存异常已解除",
        )

    def _temperature(self, host_id: int, key: str, title: str, value: float, limit: float, settings: dict[str, Any]) -> None:
        recent = self.database.query_all(
            "SELECT value FROM metric_points WHERE host_id=? AND metric=? AND object_key=? AND kind='raw' ORDER BY ts DESC LIMIT ?",
            (
                host_id,
                "gpu_temperature" if key.startswith("gpu_") else "disk_temperature" if key.startswith("disk_") else "cpu_temperature",
                key.split(":", 1)[1] if ":" in key else "",
                settings["alert_samples"],
            ),
        )
        # CPU temperatures are optional in the basic collector; current value still permits a future parser.
        values = [float(row["value"]) for row in recent] if recent else [value]
        alert_key = f"temperature:{host_id}:{key}"
        if len(values) >= settings["alert_samples"] and all(item >= limit for item in values):
            self.emit(alert_key, host_id, "temperature_high", "warning", f"{title} 温度 {value:.1f}C 超过阈值 {limit:.1f}C")
        elif len(values) >= settings["alert_samples"] and all(item < limit - settings["alert_hysteresis"] for item in values):
            self.emit(alert_key, host_id, "temperature_recovered", "info", f"{title} 温度恢复正常", state="recovered")

    def _utilization(
        self,
        host_id: int,
        metric: str,
        object_key: str,
        alert_type: str,
        recovered_type: str,
        title: str,
        value: Any,
        limit: float,
        settings: dict[str, Any],
    ) -> None:
        if value is None:
            return
        try:
            current = float(value)
        except (TypeError, ValueError):
            return
        recent = self.database.query_all(
            "SELECT value FROM metric_points WHERE host_id=? AND metric=? AND object_key=? AND kind='raw' ORDER BY ts DESC LIMIT ?",
            (host_id, metric, object_key, settings["alert_samples"]),
        )
        values = [float(row["value"]) for row in recent] if recent else [current]
        alert_key = f"{metric}:{host_id}:{object_key}"
        if len(values) >= settings["alert_samples"] and all(item >= limit for item in values):
            self.emit(alert_key, host_id, alert_type, "warning", f"{title} 使用率 {current:.1f}% 超过阈值 {limit:.1f}%")
        elif len(values) >= settings["alert_samples"] and all(item < limit - settings["alert_hysteresis"] for item in values):
            self.emit(alert_key, host_id, recovered_type, "info", f"{title} 使用率恢复正常", state="recovered")

    @staticmethod
    def _where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
        filters = filters or {}
        clauses, params = ["1=1"], []
        if not filters.get("include_cleared"):
            clauses.append("a.cleared_at IS NULL")
        for field in ("host_id", "alert_type", "state", "severity"):
            value = filters.get(field)
            if value not in {None, ""}:
                clauses.append(f"a.{field}=?")
                params.append(value)
        for field, operator in (("start", ">="), ("end", "<=")):
            value = filters.get(field)
            if value:
                clauses.append(f"a.created_at{operator}?")
                params.append(value)
        search = str(filters.get("search") or "").strip()
        if search:
            clauses.append("(a.alert_type LIKE ? OR a.summary LIKE ? OR h.name LIKE ?)")
            params.extend([f"%{search}%"] * 3)
        return " AND ".join(clauses), params

    def list(self, page: int = 1, page_size: int = 20, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        page, page_size = clamp_page(page), clamp_page_size(page_size)
        where, params = self._where(filters or {})
        total = self.database.query_one(f"SELECT COUNT(*) AS count FROM alerts a LEFT JOIN hosts h ON h.id=a.host_id WHERE {where}", params)["count"]
        rows = self.database.query_all(
            f"SELECT a.*,h.name AS host_name FROM alerts a LEFT JOIN hosts h ON h.id=a.host_id WHERE {where} ORDER BY a.id DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        )
        return paged(total, page, page_size, [dict(row) for row in rows])

    def _bulk_update(self, filters: dict[str, Any], *, user_id: int | None = None, clear: bool = False, limit: int = 1000) -> int:
        limit = max(1, min(int(limit), 1000))
        where, params = self._where(filters)
        clauses = [where, "a.cleared_at IS NULL"]
        if not clear:
            clauses.append("a.acknowledged_at IS NULL")
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"SELECT a.id FROM alerts a LEFT JOIN hosts h ON h.id=a.host_id WHERE {' AND '.join(clauses)} ORDER BY a.id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            now = utc_iso()
            if clear:
                connection.execute(f"UPDATE alerts SET cleared_at=COALESCE(cleared_at,?) WHERE id IN ({placeholders})", [now, *ids])
            else:
                connection.execute(
                    f"UPDATE alerts SET acknowledged_at=COALESCE(acknowledged_at,?),acknowledged_by=COALESCE(acknowledged_by,?) WHERE id IN ({placeholders})",
                    [now, user_id, *ids],
                )
        return len(ids)

    def bulk_acknowledge(self, filters: dict[str, Any], user_id: int, limit: int = 1000) -> int:
        return self._bulk_update(filters, user_id=user_id, limit=limit)

    def bulk_clear(self, filters: dict[str, Any], limit: int = 1000) -> int:
        return self._bulk_update(filters, clear=True, limit=limit)

    def acknowledge(self, alert_id: int, user_id: int) -> dict[str, Any]:
        row = self.database.query_one("SELECT * FROM alerts WHERE id=?", (alert_id,))
        if not row:
            raise ServiceError("告警不存在")
        self.database.execute(
            "UPDATE alerts SET acknowledged_at=COALESCE(acknowledged_at,?),acknowledged_by=COALESCE(acknowledged_by,?) WHERE id=?",
            (utc_iso(), user_id, alert_id),
        )
        return dict(self.database.query_one("SELECT * FROM alerts WHERE id=?", (alert_id,)))

    def clear(self, alert_id: int) -> dict[str, Any]:
        row = self.database.query_one("SELECT * FROM alerts WHERE id=?", (alert_id,))
        if not row:
            raise ServiceError("告警不存在")
        self.database.execute("UPDATE alerts SET cleared_at=COALESCE(cleared_at,?) WHERE id=?", (utc_iso(), alert_id))
        return dict(self.database.query_one("SELECT * FROM alerts WHERE id=?", (alert_id,)))


class BackupService:
    def __init__(self, database: Any, root: str | Path):
        self.database = database
        self.root = Path(root)

    def create(self, directory: str | Path, keep: int) -> Path:
        target_dir = Path(directory)
        if not target_dir.is_absolute():
            target_dir = self.root / target_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(target_dir, 0o700)
        target = target_dir / f"server-monitor-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
        try:
            self.database.online_backup(target)
            os.chmod(target, 0o600)
            self.database.execute("INSERT INTO backups(path,size_bytes,success,created_at) VALUES(?,?,?,?)", (str(target), target.stat().st_size, 1, utc_iso()))
            backups = sorted(target_dir.glob("server-monitor-*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
            for old in backups[keep:]:
                old.unlink()
            return target
        except Exception as exc:
            self.database.execute("INSERT INTO backups(path,success,error,created_at) VALUES(?,?,?,?)", (str(target), 0, str(exc), utc_iso()))
            raise

    def verify_restore(self, backup: str | Path, temporary_directory: str | Path, secret_box: SecretBox | None = None, encrypted_probe: str | None = None) -> Path:
        temporary = Path(temporary_directory)
        temporary.mkdir(parents=True, exist_ok=True)
        restored = temporary / "restored.sqlite3"
        shutil.copy2(backup, restored)
        check = sqlite3.connect(restored)
        try:
            check.execute("PRAGMA integrity_check").fetchone()
            check.execute("SELECT count(*) FROM users").fetchone()
        finally:
            check.close()
        if secret_box and encrypted_probe:
            if secret_box.decrypt(encrypted_probe) is None:
                raise ServiceError("备份主密钥解密验证失败")
        return restored


def export_csv(rows: Iterable[dict[str, Any]], filename_prefix: str) -> tuple[str, str]:
    materialized = list(rows)
    headers = sorted({key for row in materialized for key in row})
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(materialized)
    return f"{filename_prefix}_{utc_now().strftime('%Y%m%d_%H%M%S')}.csv", "\ufeff" + output.getvalue()


def gpu_user_usage(host_items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate GPU process ownership from the latest trusted samples."""
    grouped: dict[str, dict[str, Any]] = {}
    for item in host_items:
        host = item.get("host", item)
        latest = item.get("latest") if isinstance(item, dict) else None
        data = (latest or {}).get("data", {}) if isinstance(latest, dict) else {}
        for gpu in data.get("gpus", []) or []:
            for process in gpu.get("processes", []) or []:
                username = str(process.get("user") or "unknown").strip() or "unknown"
                row = grouped.setdefault(username, {"username": username, "gpu_count": 0, "process_count": 0, "memory_mib": 0.0, "hosts": set(), "gpus": set()})
                gpu_key = (host.get("id"), gpu.get("uuid"))
                if gpu_key not in row["gpus"]:
                    row["gpu_count"] += 1
                    row["gpus"].add(gpu_key)
                row["process_count"] += 1
                row["memory_mib"] += float(process.get("memory_mib") or 0)
                row["hosts"].add(host.get("name") or str(host.get("id")))
    result = []
    for row in grouped.values():
        row["memory_mib"] = round(row["memory_mib"], 2)
        row["hosts"] = sorted(row["hosts"])
        row["gpus"] = [f"{host_id}:{uuid}" for host_id, uuid in sorted(row["gpus"], key=lambda value: (value[0] or 0, value[1] or ""))]
        result.append(row)
    return sorted(result, key=lambda value: (-value["memory_mib"], value["username"].lower()))


def hardware_asset_rows(host_items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in host_items:
        host = item.get("host", item)
        latest = item.get("latest") if isinstance(item, dict) else None
        data = (latest or {}).get("data", {}) if isinstance(latest, dict) else {}
        hardware = data.get("hardware") or {}
        gpus = data.get("gpus") or []
        rows.append({
            "host_id": host.get("id"),
            "host_name": host.get("name"),
            "address": host.get("address"),
            "status": host.get("status"),
            "asset_location": host.get("asset_location", ""),
            "asset_owner": host.get("asset_owner", ""),
            "warranty_expires": host.get("warranty_expires") or "",
            "cpu_model": hardware.get("cpu_model", ""),
            "memory_total_bytes": hardware.get("memory_total_bytes"),
            "motherboard": hardware.get("motherboard", ""),
            "gpu_models": "; ".join(f"GPU {gpu.get('index')}: {gpu.get('name', '')}" for gpu in gpus),
            "pci_devices": "; ".join(
                " ".join(str(value) for value in (device.get("bus", device.get("name", "")), device.get("description", "")) if value)
                for device in hardware.get("pci_devices", []) or []
            ),
            "collected_at": latest.get("collected_at") if isinstance(latest, dict) else None,
        })
    return rows

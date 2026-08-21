from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .security import redact
from .utils import command_summary, json_dump, json_load, parse_utc, utc_iso, utc_now


GPU_STATES = {"disabled", "unknown", "busy", "idle_timing", "pending", "running", "retry_wait", "cooldown", "frozen"}


@dataclass
class DispatchResult:
    success: bool
    confirmed_not_started: bool = False
    unknown_execution: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class GPUScheduler:
    def __init__(self, database: Any, config: Any, audit: Any | None = None, alert: Any | None = None):
        self.database = database
        self.config = config
        self.audit = audit
        self.alert = alert
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def _lock_for(self, physical_id: str, gpu_uuid: str) -> threading.Lock:
        key = (physical_id, gpu_uuid)
        with self._locks_lock:
            return self._locks.setdefault(key, threading.Lock())

    @staticmethod
    def _config(row: Any | None) -> dict[str, Any]:
        if not row:
            return {}
        result = dict(row)
        for key in ("enabled", "process_guard", "active"):
            if key in result and result[key] is not None:
                result[key] = bool(result[key])
        result["env_override"] = json_load(result.pop("env_override_json", None), None)
        return result

    def configure_gpu(self, host: dict[str, Any], gpu_uuid: str, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"enabled", "idle_mode", "util_threshold", "memory_threshold", "process_guard", "command_override", "cwd_override", "shell_override", "env_override", "mode_override"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"未知 GPU 配置: {', '.join(sorted(unknown))}")
        clean: dict[str, Any] = {}
        if "enabled" in values:
            if not isinstance(values["enabled"], bool):
                raise ValueError("enabled 必须为布尔值")
            clean["enabled"] = int(values["enabled"])
        if "idle_mode" in values:
            if values["idle_mode"] not in {"util", "memory", "both", None}:
                raise ValueError("idle_mode 无效")
            clean["idle_mode"] = values["idle_mode"]
        for key in ("util_threshold", "memory_threshold"):
            if key in values:
                if values[key] is None:
                    clean[key] = None
                elif isinstance(values[key], bool) or not 0 <= int(values[key]) <= 100:
                    raise ValueError(f"{key} 必须在 0～100 之间")
                else:
                    clean[key] = int(values[key])
        if "process_guard" in values:
            if values["process_guard"] is not None and not isinstance(values["process_guard"], bool):
                raise ValueError("process_guard 必须为布尔值或 null")
            clean["process_guard"] = None if values["process_guard"] is None else int(values["process_guard"])
        for key in ("command_override", "cwd_override", "shell_override"):
            if key in values:
                value = values[key]
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{key} 必须为字符串或 null")
                if key == "command_override" and value and len(value) > 500:
                    raise ValueError("单卡调度命令最长 500 个字符")
                clean[key] = value.strip() if value else None
        if "env_override" in values:
            value = values["env_override"]
            if value is not None and (not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items())):
                raise ValueError("环境变量必须是字符串键值对象或 null")
            clean["env_override_json"] = json_dump(value) if value is not None else None
        if "mode_override" in values:
            if values["mode_override"] not in {"tmux", "direct", None}:
                raise ValueError("mode_override 无效")
            clean["mode_override"] = values["mode_override"]
        now = utc_iso()
        existing = self.database.query_one("SELECT id FROM gpu_configs WHERE physical_id=? AND gpu_uuid=? AND active=1", (host["physical_id"], gpu_uuid))
        if existing:
            if clean:
                self.database.execute("UPDATE gpu_configs SET " + ",".join(f"{key}=?" for key in clean) + ",updated_at=? WHERE id=?", [*clean.values(), now, existing["id"]])
        else:
            columns = {"host_id": host["id"], "physical_id": host["physical_id"], "gpu_uuid": gpu_uuid, "updated_at": now, **clean}
            self.database.execute("INSERT INTO gpu_configs(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", list(columns.values()))
        self._set_unknown(host["physical_id"], gpu_uuid, host["id"], "GPU 调度配置已修改")
        return self.get_gpu_config(host, gpu_uuid)

    def get_gpu_config(self, host: dict[str, Any], gpu_uuid: str) -> dict[str, Any]:
        row = self.database.query_one("SELECT * FROM gpu_configs WHERE physical_id=? AND gpu_uuid=? AND active=1", (host["physical_id"], gpu_uuid))
        return self._config(row)

    def _set_runtime(self, physical_id: str, gpu_uuid: str, host_id: int, **values: Any) -> None:
        now = utc_iso()
        defaults = {"state": "unknown", "idle_seconds_accum": 0, "last_valid_at": None, "attempts": 0, "retry_at": None, "cooldown_until": None, "frozen_until": None, "last_error": None, "updated_at": now}
        defaults.update(values)
        existing = self.database.query_one("SELECT 1 FROM gpu_runtime WHERE physical_id=? AND gpu_uuid=?", (physical_id, gpu_uuid))
        if existing:
            self.database.execute("UPDATE gpu_runtime SET " + ",".join(f"{key}=?" for key in defaults) + " WHERE physical_id=? AND gpu_uuid=?", [*defaults.values(), physical_id, gpu_uuid])
        else:
            columns = {"physical_id": physical_id, "gpu_uuid": gpu_uuid, "host_id": host_id, **defaults}
            self.database.execute("INSERT INTO gpu_runtime(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", list(columns.values()))

    def _set_unknown(self, physical_id: str, gpu_uuid: str, host_id: int, reason: str) -> None:
        self._set_runtime(physical_id, gpu_uuid, host_id, state="unknown", idle_seconds_accum=0, attempts=0, retry_at=None, cooldown_until=None, frozen_until=None, last_error=reason)

    def reset_host(self, host_id: int, reason: str) -> None:
        self.database.execute("UPDATE gpu_runtime SET state='unknown',idle_seconds_accum=0,attempts=0,retry_at=NULL,cooldown_until=NULL,frozen_until=NULL,last_error=?,updated_at=? WHERE host_id=?", (reason, utc_iso(), host_id))

    def observe(self, host: dict[str, Any], gpus: list[dict[str, Any]], *, sample_valid: bool) -> list[dict[str, Any]]:
        settings = self.config.all()
        observed = {gpu["uuid"]: gpu for gpu in gpus}
        configs = self.database.query_all("SELECT * FROM gpu_configs WHERE host_id=? AND active=1", (host["id"],))
        for row in configs:
            if row["gpu_uuid"] not in observed:
                self._set_unknown(host["physical_id"], row["gpu_uuid"], host["id"], "GPU 指标缺失")
        output: list[dict[str, Any]] = []
        for gpu_uuid, gpu in observed.items():
            config = self.get_gpu_config(host, gpu_uuid)
            runtime_row = self.database.query_one("SELECT * FROM gpu_runtime WHERE physical_id=? AND gpu_uuid=?", (host["physical_id"], gpu_uuid))
            runtime = dict(runtime_row) if runtime_row else {}
            status = self._observe_one(host, gpu, config, runtime, settings, sample_valid)
            output.append(status)
        return output

    def _observe_one(self, host: dict[str, Any], gpu: dict[str, Any], config: dict[str, Any], runtime: dict[str, Any], settings: dict[str, Any], sample_valid: bool) -> dict[str, Any]:
        physical_id, gpu_uuid = host["physical_id"], gpu["uuid"]
        previous_state = runtime.get("state")
        all_enabled = bool(settings["gpu_scheduler_enabled"] and host["scheduler_enabled"] and config.get("enabled"))
        if not all_enabled:
            self._set_runtime(physical_id, gpu_uuid, host["id"], state="disabled", idle_seconds_accum=0, attempts=0, retry_at=None, cooldown_until=None, frozen_until=None, last_error=None)
            return {"uuid": gpu_uuid, "state": "disabled", "previous_state": previous_state, "idle_seconds": 0}
        if not sample_valid:
            self._set_unknown(physical_id, gpu_uuid, host["id"], "GPU 采样无效")
            return {"uuid": gpu_uuid, "state": "unknown", "previous_state": previous_state, "idle_seconds": 0}
        now = utc_now()
        frozen = parse_utc(runtime.get("frozen_until"))
        if frozen and frozen > now:
            self._set_runtime(physical_id, gpu_uuid, host["id"], **{**runtime, "state": "frozen", "updated_at": utc_iso()})
            return {"uuid": gpu_uuid, "state": "frozen", "previous_state": previous_state, "idle_seconds": 0, "until": utc_iso(frozen)}
        if frozen:
            self._set_unknown(physical_id, gpu_uuid, host["id"], "冻结已解除，等待重新采样")
            return {"uuid": gpu_uuid, "state": "unknown", "previous_state": previous_state, "idle_seconds": 0}
        cooldown = parse_utc(runtime.get("cooldown_until"))
        if cooldown and cooldown > now:
            self._set_runtime(physical_id, gpu_uuid, host["id"], **{**runtime, "state": "cooldown", "updated_at": utc_iso()})
            return {"uuid": gpu_uuid, "state": "cooldown", "previous_state": previous_state, "idle_seconds": 0, "until": utc_iso(cooldown)}
        if cooldown:
            self._set_unknown(physical_id, gpu_uuid, host["id"], "冷却结束，等待重新采样")
            return {"uuid": gpu_uuid, "state": "unknown", "previous_state": previous_state, "idle_seconds": 0}
        mode = config.get("idle_mode") or settings["gpu_idle_mode"]
        util_limit = config.get("util_threshold") if config.get("util_threshold") is not None else settings["gpu_util_threshold"]
        memory_limit = config.get("memory_threshold") if config.get("memory_threshold") is not None else settings["gpu_memory_threshold"]
        process_guard = config.get("process_guard") if config.get("process_guard") is not None else bool(host["scheduler_process_guard"]) if host.get("scheduler_process_guard") is not None else settings["gpu_process_guard"]
        util_ok = gpu.get("utilization_percent") is not None and gpu["utilization_percent"] < util_limit
        memory_ok = gpu.get("memory_percent") is not None and gpu["memory_percent"] < memory_limit
        idle = util_ok if mode == "util" else memory_ok if mode == "memory" else util_ok and memory_ok
        if process_guard and gpu.get("processes"):
            idle = False
        if not idle:
            self._set_runtime(physical_id, gpu_uuid, host["id"], state="busy", idle_seconds_accum=0, last_valid_at=utc_iso(), attempts=0, retry_at=None, cooldown_until=None, frozen_until=None, last_error=None)
            return {"uuid": gpu_uuid, "state": "busy", "previous_state": previous_state, "idle_seconds": 0}
        previous = parse_utc(runtime.get("last_valid_at"))
        elapsed = max(0.0, (now - previous).total_seconds()) if previous else 0.0
        # State restarts only after a real valid interval; application downtime never appears in samples.
        accumulated = (float(runtime.get("idle_seconds_accum") or 0) + elapsed) if runtime.get("state") in {"idle_timing", "pending", "retry_wait"} else 0.0
        idle_seconds = int(host.get("scheduler_idle_seconds") or settings["gpu_idle_seconds"])
        state = "pending" if accumulated >= idle_seconds else "idle_timing"
        self._set_runtime(physical_id, gpu_uuid, host["id"], state=state, idle_seconds_accum=accumulated, last_valid_at=utc_iso(), attempts=int(runtime.get("attempts") or 0), retry_at=runtime.get("retry_at"), cooldown_until=None, frozen_until=None, last_error=None)
        return {"uuid": gpu_uuid, "state": state, "previous_state": previous_state, "idle_seconds": accumulated, "remaining_seconds": max(0, idle_seconds - accumulated), "near_trigger": accumulated >= idle_seconds * 0.8}

    def ready(self) -> list[dict[str, Any]]:
        rows = self.database.query_all(
            "SELECT r.*,h.* FROM gpu_runtime r JOIN hosts h ON h.id=r.host_id WHERE h.deleted_at IS NULL "
            "AND r.state IN ('pending','retry_wait')"
        )
        now = utc_now()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            retry = parse_utc(item.get("retry_at"))
            if item["state"] == "retry_wait" and retry and retry > now:
                continue
            results.append(item)
        return results

    def effective_command(self, host: dict[str, Any], gpu_uuid: str) -> dict[str, Any]:
        gpu = self.get_gpu_config(host, gpu_uuid)
        fields = {
            "command": (gpu.get("command_override") or host.get("schedule_command") or "").strip(),
            "cwd": gpu.get("cwd_override") if gpu.get("cwd_override") is not None else host.get("schedule_cwd"),
            "shell": gpu.get("shell_override") or host.get("schedule_shell") or "/bin/bash",
            "env": gpu.get("env_override") if gpu.get("env_override") is not None else host.get("schedule_env") or {},
            "mode": gpu.get("mode_override") or host.get("schedule_mode") or "tmux",
        }
        if not fields["command"]:
            raise ValueError("该主机未配置 GPU 默认调度命令")
        return fields

    def dispatch(self, host: dict[str, Any], gpu: dict[str, Any], executor: Callable[[dict[str, Any], dict[str, Any], str], DispatchResult]) -> DispatchResult:
        gpu_uuid = gpu["uuid"]
        lock = self._lock_for(host["physical_id"], gpu_uuid)
        if not lock.acquire(blocking=False):
            return DispatchResult(False, confirmed_not_started=True, error="同一 GPU 已有调度操作正在执行")
        try:
            runtime = self.database.query_one("SELECT * FROM gpu_runtime WHERE physical_id=? AND gpu_uuid=?", (host["physical_id"], gpu_uuid))
            if not runtime or runtime["state"] not in {"pending", "retry_wait"}:
                return DispatchResult(False, confirmed_not_started=True, error="GPU 当前不处于待执行状态")
            task_id = str(uuid.uuid4())
            effective = self.effective_command(host, gpu_uuid)
            attempts = int(runtime["attempts"]) + 1
            self._set_runtime(host["physical_id"], gpu_uuid, host["id"], state="running", idle_seconds_accum=float(runtime["idle_seconds_accum"]), last_valid_at=runtime["last_valid_at"], attempts=attempts, retry_at=None, cooldown_until=None, frozen_until=None, last_error=None)
            self.database.execute(
                "INSERT INTO schedule_jobs(id,host_id,physical_id,gpu_uuid,mode,command_summary,state,attempt,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, host["id"], host["physical_id"], gpu_uuid, effective["mode"], command_summary(effective["command"]), "running", attempts, utc_iso()),
            )
            result = executor(effective, gpu, task_id)
            self._finish(host, gpu_uuid, task_id, attempts, result, effective["mode"])
            return result
        finally:
            lock.release()

    def _finish(self, host: dict[str, Any], gpu_uuid: str, task_id: str, attempts: int, result: DispatchResult, mode: str) -> None:
        settings = self.config.all()
        now = utc_iso()
        if result.success:
            cooldown_until = utc_iso(utc_now() + __import__("datetime").timedelta(seconds=settings["gpu_cooldown_seconds"]))
            self._set_runtime(host["physical_id"], gpu_uuid, host["id"], state="cooldown", idle_seconds_accum=0, last_valid_at=None, attempts=0, retry_at=None, cooldown_until=cooldown_until, frozen_until=None, last_error=None)
            state, error = "success", None
            if self.alert:
                title = "Tmux 提交成功" if mode == "tmux" else "直接 Shell 执行成功"
                self.alert.emit(f"gpu-schedule:{task_id}", host["id"], "gpu_schedule_success", "info", title)
        elif result.confirmed_not_started and attempts < settings["gpu_max_attempts"]:
            retry_at = utc_iso(utc_now() + __import__("datetime").timedelta(seconds=settings["gpu_retry_seconds"]))
            idle_required = int(host.get("scheduler_idle_seconds") or settings["gpu_idle_seconds"])
            self._set_runtime(host["physical_id"], gpu_uuid, host["id"], state="retry_wait", idle_seconds_accum=idle_required, last_valid_at=utc_iso(), attempts=attempts, retry_at=retry_at, cooldown_until=None, frozen_until=None, last_error=result.error)
            state, error = "retry_wait", result.error
            if self.alert:
                self.alert.emit(f"gpu-schedule-failed:{task_id}", host["id"], "gpu_schedule_failed", "warning", f"GPU {gpu_uuid[:12]} 调度失败，等待安全重试")
        else:
            frozen_until = utc_iso(utc_now() + __import__("datetime").timedelta(seconds=settings["gpu_freeze_seconds"]))
            self._set_runtime(host["physical_id"], gpu_uuid, host["id"], state="frozen", idle_seconds_accum=0, last_valid_at=None, attempts=attempts, retry_at=None, cooldown_until=None, frozen_until=frozen_until, last_error=result.error or "远端执行结果无法确认")
            state, error = "frozen", result.error or "远端执行结果无法确认"
            if self.alert:
                self.alert.emit(f"gpu-schedule-failed:{task_id}", host["id"], "gpu_schedule_failed", "critical", f"GPU {gpu_uuid[:12]} 调度失败: {error}")
                self.alert.emit(f"gpu-frozen:{host['physical_id']}:{gpu_uuid}", host["id"], "gpu_schedule_frozen", "critical", f"GPU {gpu_uuid[:12]} 调度已冻结: {error}")
        self.database.execute(
            "UPDATE schedule_jobs SET state=?,exit_code=?,stdout=?,stderr=?,stdout_truncated=?,stderr_truncated=?,error=?,finished_at=? WHERE id=?",
            (state, result.exit_code, redact(result.stdout) or "", redact(result.stderr) or "", int(result.stdout_truncated), int(result.stderr_truncated), redact(error), now, task_id),
        )

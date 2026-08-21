from __future__ import annotations

import concurrent.futures
import threading
import time
import uuid
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .collector import Collector
from .gpu_scheduler import GPUScheduler
from .operations import OperationService
from .ssh_client import SSHConnectionPool
from .services import compact_collection_result
from .utils import parse_utc, utc_iso, utc_now


class CollectionCancelled(RuntimeError):
    pass


class BackgroundService:
    def __init__(self, hosts: Any, config: Any, secrets: Any, database: Any, scheduler: GPUScheduler, alerts: Any, audit: Any, backups: Any | None = None, connection_pool: SSHConnectionPool | None = None):
        self.hosts = hosts
        self.config = config
        self.secrets = secrets
        self.database = database
        self.scheduler = scheduler
        self.alerts = alerts
        self.audit = audit
        self.backups = backups
        self.connection_pool = connection_pool
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=30, thread_name_prefix="server-monitor")
        self._active: dict[int, concurrent.futures.Future[Any]] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self._collection_tasks: dict[int, str] = {}
        self._manual: dict[tuple[int, int], str] = {}
        self._lock = threading.RLock()
        self._ssh_condition = threading.Condition()
        self._ssh_active = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_maintenance = 0.0
        self._started_at = time.monotonic()
        self._backup_day: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="server-monitor-background", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        next_collection = 0.0
        while not self._stop.is_set():
            settings = self.config.all()
            now = time.monotonic()
            if now >= next_collection:
                for host in self.hosts.list():
                    if host["enabled"]:
                        self.submit_collection(host["id"])
                next_collection = now + settings["collection_interval"]
            self._dispatch_ready()
            self._maybe_backup()
            maintenance_interval = max(60, int(settings["cleanup_interval_minutes"]) * 60)
            if now - self._last_maintenance >= maintenance_interval:
                self._maintenance()
                self._last_maintenance = now
            self._stop.wait(0.25)

    def submit_collection(self, host_id: int, *, manual: bool = False, user_id: int | None = None) -> str:
        now = time.monotonic()
        with self._lock:
            current = self._active.get(host_id)
            if current and not current.done():
                if manual and user_id is not None:
                    key = (user_id, host_id)
                    if key in self._manual and now - float(self._manual[key].split(":", 1)[0]) <= 10:
                        return self._manual[key].split(":", 1)[1]
                self.hosts.mark_busy(host_id, "上一次采集尚未结束")
                return "busy"
            if len([future for future in self._active.values() if not future.done()]) >= self.config.all()["queue_limit"]:
                self.hosts.mark_busy(host_id, "采集队列已满")
                return "busy"
            task_id = str(uuid.uuid4())
            self.database.execute("INSERT INTO tasks(id,task_type,host_id,state,created_at) VALUES(?,?,?,?,?)", (task_id, "collection", host_id, "queued", utc_iso()))
            cancel_event = threading.Event()
            future = self.executor.submit(self._collect, task_id, host_id, cancel_event)
            self._active[host_id] = future
            self._cancel_events[host_id] = cancel_event
            self._collection_tasks[host_id] = task_id
            if manual and user_id is not None:
                self._manual[(user_id, host_id)] = f"{now}:{task_id}"
            return task_id

    def cancel_host(self, host_id: int, reason: str) -> dict[str, bool]:
        cancelled = {"collection": False, "dispatch": False}
        with self._lock:
            for key, label in ((host_id, "collection"), (-host_id, "dispatch")):
                event = self._cancel_events.get(key)
                if event:
                    event.set()
                future = self._active.get(key)
                if future and not future.done() and future.cancel():
                    cancelled[label] = True
            task_id = self._collection_tasks.get(host_id)
            if cancelled["collection"] and task_id:
                self.database.execute(
                    "UPDATE tasks SET state='cancelled',error=?,finished_at=? WHERE id=? AND state='queued'",
                    (reason, utc_iso(), task_id),
                )
        self.scheduler.reset_host(host_id, reason)
        return cancelled

    def _wait_for_retry(self, seconds: float, cancel_event: threading.Event | None) -> None:
        if cancel_event is None:
            if self._stop.wait(seconds):
                raise RuntimeError("应用正在停止")
            return
        deadline = time.monotonic() + seconds
        while True:
            if cancel_event.is_set():
                raise CollectionCancelled("采集任务已取消")
            if self._stop.is_set():
                raise RuntimeError("应用正在停止")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            cancel_event.wait(min(0.2, remaining))

    def _collect(self, task_id: str, host_id: int, cancel_event: threading.Event | None = None) -> None:
        self.database.execute("UPDATE tasks SET state='running',started_at=? WHERE id=?", (utc_iso(), task_id))
        try:
            if cancel_event and cancel_event.is_set():
                raise CollectionCancelled("采集任务已取消")
            host = self.hosts.get(host_id, include_secrets=True)
            previous = self.hosts.latest(host_id)
            settings = self.config.all()
            result = None
            for attempt in range(settings["collection_retries"] + 1):
                if cancel_event and cancel_event.is_set():
                    raise CollectionCancelled("采集任务已取消")
                with self._ssh_condition:
                    while self._ssh_active >= self.config.all()["ssh_concurrency"] and not self._stop.is_set():
                        self._ssh_condition.wait(0.2)
                    if self._stop.is_set():
                        raise RuntimeError("应用正在停止")
                    self._ssh_active += 1
                try:
                    result = Collector(self.secrets, settings, self.connection_pool).collect(host, (previous or {}).get("data"))
                finally:
                    with self._ssh_condition:
                        self._ssh_active -= 1
                        self._ssh_condition.notify_all()
                if cancel_event and cancel_event.is_set():
                    raise CollectionCancelled("采集任务已取消")
                if result.core_ok or attempt >= settings["collection_retries"]:
                    break
                self._wait_for_retry(settings["retry_interval"], cancel_event)
            assert result is not None
            outcome = self.hosts.ingest_collection(host_id, result)
            self.alerts.evaluate_host(host_id, outcome["status"], outcome.get("data"))
            if result.core_ok and outcome.get("data"):
                self.alerts.evaluate_gpu_availability(host_id, (previous or {}).get("data"), outcome["data"])
                self.scheduler.observe(host, outcome["data"].get("gpus", []), sample_valid=True)
            else:
                self.scheduler.reset_host(host_id, outcome.get("error") or "采集失败")
            self.database.execute(
                "UPDATE tasks SET state=?,result_json=?,error=?,finished_at=? WHERE id=?",
                ("success" if result.core_ok else "failed", __import__("json").dumps(compact_collection_result(outcome), ensure_ascii=False), result.error, utc_iso(), task_id),
            )
        except CollectionCancelled as exc:
            self.scheduler.reset_host(host_id, str(exc))
            self.database.execute(
                "UPDATE tasks SET state='cancelled',error=?,finished_at=? WHERE id=?",
                (str(exc), utc_iso(), task_id),
            )
        except Exception as exc:
            self.hosts.status(host_id, "unknown", error=str(exc))
            self.scheduler.reset_host(host_id, "采集任务异常")
            self.database.execute("UPDATE tasks SET state='failed',error=?,finished_at=? WHERE id=?", (str(exc), utc_iso(), task_id))

    def _dispatch_ready(self) -> None:
        for row in self.scheduler.ready():
            host_id = row["host_id"]
            with self._lock:
                future = self._active.get(-int(host_id))
                if future and not future.done():
                    continue
                key = -int(host_id)
                cancel_event = threading.Event()
                future = self.executor.submit(self._dispatch_one, dict(row), cancel_event)
                self._active[key] = future
                self._cancel_events[key] = cancel_event

    def _dispatch_one(self, row: dict[str, Any], cancel_event: threading.Event | None = None) -> None:
        if cancel_event and cancel_event.is_set():
            return
        host = self.hosts.get(row["host_id"], include_secrets=True)
        latest = self.hosts.latest(host["id"])
        recheck = Collector(self.secrets, self.config.all(), self.connection_pool).collect(host, (latest or {}).get("data"))
        if cancel_event and cancel_event.is_set():
            return
        gpu = next((gpu for gpu in recheck.data.get("gpus", []) if gpu["uuid"] == row["gpu_uuid"]), None) if recheck.core_ok else None
        if not gpu:
            self.scheduler.reset_host(host["id"], "执行前复核缺少 GPU 指标")
            return
        status = self.scheduler.observe(host, [gpu], sample_valid=True)[0]
        if status["state"] not in {"pending", "retry_wait"}:
            return
        if cancel_event and cancel_event.is_set():
            return
        self.scheduler.dispatch(host, gpu, lambda effective, current_gpu, task_id: OperationService(self.secrets, self.config, self.database, self.connection_pool).dispatch_gpu(host, effective, current_gpu, task_id))

    def _maintenance(self) -> None:
        try:
            from .services import HistoryService

            settings = self.config.all()
            history = HistoryService(self.database)
            history.aggregate(
                mid_seconds=settings["aggregation_mid_seconds"],
                long_seconds=settings["aggregation_long_seconds"],
                raw_retention_minutes=settings["metric_raw_retention_minutes"],
                mid_retention_hours=settings["metric_mid_retention_hours"],
            )
            history.cleanup(
                metric_retention_days=settings["metric_retention_days"],
                log_retention_days=settings["log_retention_days"],
                collection_task_retention_minutes=settings["collection_task_retention_minutes"],
            )
            # Cleanup removes rows; checkpointing releases the WAL growth
            # without forcing a potentially expensive VACUUM on every cycle.
            self.database.checkpoint()
        except Exception as exc:
            self.audit.write("maintenance_failed", success=False, summary="历史维护任务失败", error=str(exc))

    def _maybe_backup(self) -> None:
        if not self.backups:
            return
        settings = self.config.all()
        try:
            local_now = utc_now().astimezone(ZoneInfo(settings["timezone"]))
        except Exception:
            local_now = utc_now()
        day = local_now.strftime("%Y-%m-%d")
        scheduled = local_now.strftime("%H:%M") == settings["backup_time"]
        latest = self.database.query_one("SELECT created_at FROM backups WHERE success=1 ORDER BY id DESC LIMIT 1")
        stale = not latest or (utc_now() - (parse_utc(latest["created_at"]) or utc_now())) > timedelta(hours=24)
        due_after_start = time.monotonic() - self._started_at >= 600 and stale
        if self._backup_day == day or not (scheduled or due_after_start):
            return
        self._backup_day = day
        try:
            self.backups.create(settings["backup_dir"], settings["backup_keep"])
            self.audit.write("backup_created", success=True, summary="自动数据库备份成功")
        except Exception as exc:
            self.audit.write("backup_failed", success=False, summary="自动数据库备份失败", error=str(exc))
            self.alerts.emit("backup-failed", None, "backup_failed", "critical", "数据库自动备份失败")

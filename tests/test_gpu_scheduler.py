from __future__ import annotations

from datetime import timedelta

from monitor.gpu_scheduler import DispatchResult
from monitor.utils import utc_iso, utc_now

from .test_hosts_history import host_payload


GPU_UUID = "GPU-11111111-2222-3333-4444-555555555555"


def enabled_host(app):
    hosts = app.extensions["hosts"]
    host = hosts.create(
        {
            **host_payload(),
            "scheduler_enabled": True,
            "scheduler_idle_seconds": 60,
            "schedule_command": "echo scheduled",
            "schedule_mode": "direct",
        },
        fingerprint="SHA256:key-one",
        machine_id="machine-one",
    )
    scheduler = app.extensions["gpu_scheduler"]
    scheduler.configure_gpu(host, GPU_UUID, {"enabled": True})
    app.extensions["monitor_config"].update({"gpu_scheduler_enabled": True, "gpu_idle_seconds": 60, "gpu_cooldown_seconds": 0})
    return hosts.get(host["id"], include_secrets=True)


def gpu(util=0, memory=0, processes=None):
    return {"uuid": GPU_UUID, "utilization_percent": util, "memory_percent": memory, "processes": processes or []}


def test_scheduler_gates_and_busy_reset(app):
    hosts = app.extensions["hosts"]
    host = hosts.create({**host_payload(), "scheduler_enabled": True}, fingerprint="SHA256:key-one", machine_id="machine-one")
    result = app.extensions["gpu_scheduler"].observe(host, [gpu()], sample_valid=True)[0]
    assert result["state"] == "disabled"
    host = enabled_host_for_existing(app, host)
    result = app.extensions["gpu_scheduler"].observe(host, [gpu(util=80)], sample_valid=True)[0]
    assert result["state"] == "busy"
    assert result["idle_seconds"] == 0


def enabled_host_for_existing(app, host):
    scheduler = app.extensions["gpu_scheduler"]
    scheduler.configure_gpu(host, GPU_UUID, {"enabled": True})
    app.extensions["monitor_config"].update({"gpu_scheduler_enabled": True})
    return app.extensions["hosts"].get(host["id"], include_secrets=True)


def test_idle_accumulation_requires_valid_samples(app):
    host = enabled_host(app)
    scheduler = app.extensions["gpu_scheduler"]
    first = scheduler.observe(host, [gpu()], sample_valid=True)[0]
    assert first["state"] == "idle_timing" and first["idle_seconds"] == 0
    app.extensions["database"].execute(
        "UPDATE gpu_runtime SET last_valid_at=?,idle_seconds_accum=50,state='idle_timing' WHERE physical_id=? AND gpu_uuid=?",
        (utc_iso(utc_now() - timedelta(seconds=15)), host["physical_id"], GPU_UUID),
    )
    second = scheduler.observe(host, [gpu()], sample_valid=True)[0]
    assert second["state"] == "pending"
    scheduler.observe(host, [gpu()], sample_valid=False)
    row = app.extensions["database"].query_one("SELECT * FROM gpu_runtime WHERE physical_id=? AND gpu_uuid=?", (host["physical_id"], GPU_UUID))
    assert row["state"] == "unknown" and row["idle_seconds_accum"] == 0


def set_pending(app, host):
    app.extensions["database"].execute(
        "UPDATE gpu_runtime SET state='pending',idle_seconds_accum=60,last_valid_at=? WHERE physical_id=? AND gpu_uuid=?",
        (utc_iso(), host["physical_id"], GPU_UUID),
    )


def test_success_enters_cooldown_and_records_job(app):
    host = enabled_host(app)
    scheduler = app.extensions["gpu_scheduler"]
    scheduler.observe(host, [gpu()], sample_valid=True)
    set_pending(app, host)
    result = scheduler.dispatch(host, gpu(), lambda effective, current, task_id: DispatchResult(True, exit_code=0, stdout="ok"))
    assert result.success
    runtime = app.extensions["database"].query_one("SELECT * FROM gpu_runtime")
    assert runtime["state"] == "cooldown"
    job = app.extensions["database"].query_one("SELECT * FROM schedule_jobs")
    assert job["state"] == "success"


def test_only_confirmed_not_started_failure_retries(app):
    host = enabled_host(app)
    scheduler = app.extensions["gpu_scheduler"]
    scheduler.observe(host, [gpu()], sample_valid=True)
    set_pending(app, host)
    scheduler.dispatch(host, gpu(), lambda *_: DispatchResult(False, confirmed_not_started=True, error="connect failed"))
    assert app.extensions["database"].query_one("SELECT state FROM gpu_runtime")["state"] == "retry_wait"
    set_pending(app, host)
    scheduler.dispatch(host, gpu(), lambda *_: DispatchResult(False, unknown_execution=True, error="transport lost"))
    assert app.extensions["database"].query_one("SELECT state FROM gpu_runtime")["state"] == "frozen"


def test_nonzero_remote_exit_freezes_without_retry(app):
    host = enabled_host(app)
    scheduler = app.extensions["gpu_scheduler"]
    scheduler.observe(host, [gpu()], sample_valid=True)
    set_pending(app, host)
    scheduler.dispatch(host, gpu(), lambda *_: DispatchResult(False, exit_code=2, error="exit 2"))
    runtime = app.extensions["database"].query_one("SELECT * FROM gpu_runtime")
    assert runtime["state"] == "frozen"
    assert runtime["retry_at"] is None

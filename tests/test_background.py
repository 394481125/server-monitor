from __future__ import annotations

import concurrent.futures
import json
import threading
from types import SimpleNamespace

from monitor.background import BackgroundService
from monitor.collector import CollectionResult
from monitor.utils import utc_iso

from .test_hosts_history import host_payload, sample_data


def test_background_retries_failed_collection_without_overlapping(app, monkeypatch):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    config = app.extensions["monitor_config"]
    config.update({"collection_retries": 2, "retry_interval": 1, "ssh_concurrency": 1})
    database = app.extensions["database"]
    task_id = "retry-test"
    database.execute("INSERT INTO tasks(id,task_type,host_id,state,created_at) VALUES(?,?,?,?,?)", (task_id, "collection", host["id"], "queued", utc_iso()))
    attempts = []

    class FakeCollector:
        def __init__(self, *_args):
            pass

        def collect(self, *_args):
            attempts.append(1)
            if len(attempts) < 3:
                return CollectionResult(False, {}, {}, error="temporary")
            return CollectionResult(True, sample_data(), {})

    monkeypatch.setattr("monitor.background.Collector", FakeCollector)
    service = BackgroundService(hosts, config, app.extensions["secret_box"], database, app.extensions["gpu_scheduler"], app.extensions["alerts"], app.extensions["audit"])
    service._stop = SimpleNamespace(is_set=lambda: False, wait=lambda _seconds: False, set=lambda: None)
    service._collect(task_id, host["id"])
    assert len(attempts) == 3
    task = database.query_one("SELECT state,result_json FROM tasks WHERE id=?", (task_id,))
    assert task["state"] == "success"
    summary = json.loads(task["result_json"])
    assert summary["status"] == "online"
    assert summary["collected_at"]
    assert "data" not in summary and len(task["result_json"]) < 256
    assert hosts.get(host["id"])["status"] == "online"
    service.stop()


def test_cancel_host_cancels_queued_collection_and_records_state(app):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    database = app.extensions["database"]
    task_id = "queued-cancel-test"
    database.execute(
        "INSERT INTO tasks(id,task_type,host_id,state,created_at) VALUES(?,?,?,?,?)",
        (task_id, "collection", host["id"], "queued", utc_iso()),
    )
    service = BackgroundService(
        hosts,
        app.extensions["monitor_config"],
        app.extensions["secret_box"],
        database,
        app.extensions["gpu_scheduler"],
        app.extensions["alerts"],
        app.extensions["audit"],
    )
    future = concurrent.futures.Future()
    cancel_event = threading.Event()
    service._active[host["id"]] = future
    service._cancel_events[host["id"]] = cancel_event
    service._collection_tasks[host["id"]] = task_id

    result = service.cancel_host(host["id"], "主机采集已禁用")

    assert result["collection"] is True
    assert cancel_event.is_set()
    task = database.query_one("SELECT state,error,finished_at FROM tasks WHERE id=?", (task_id,))
    assert task["state"] == "cancelled"
    assert task["error"] == "主机采集已禁用"
    assert task["finished_at"]
    service.stop()


def test_collection_cancel_signal_prevents_remote_collection(app, monkeypatch):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    database = app.extensions["database"]
    task_id = "running-cancel-test"
    database.execute(
        "INSERT INTO tasks(id,task_type,host_id,state,created_at) VALUES(?,?,?,?,?)",
        (task_id, "collection", host["id"], "queued", utc_iso()),
    )
    service = BackgroundService(
        hosts,
        app.extensions["monitor_config"],
        app.extensions["secret_box"],
        database,
        app.extensions["gpu_scheduler"],
        app.extensions["alerts"],
        app.extensions["audit"],
    )
    monkeypatch.setattr(
        "monitor.background.Collector",
        lambda *_args: (_ for _ in ()).throw(AssertionError("取消后不应创建远端采集器")),
    )
    cancel_event = threading.Event()
    cancel_event.set()

    service._collect(task_id, host["id"], cancel_event)

    task = database.query_one("SELECT state,error FROM tasks WHERE id=?", (task_id,))
    assert task["state"] == "cancelled"
    assert task["error"] == "采集任务已取消"
    service.stop()

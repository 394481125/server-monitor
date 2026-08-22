from __future__ import annotations

from monitor.collector import CollectionResult
from monitor.services import idle_gpu_rows

from .test_hosts_history import host_payload


def gpu(uuid: str, *, utilization: float, total: float = 24_576, used: float = 0, processes=None):
    return {
        "index": int(uuid.rsplit("-", 1)[-1]),
        "uuid": uuid,
        "name": "NVIDIA RTX 4090",
        "utilization_percent": utilization,
        "memory_total_mib": total,
        "memory_used_mib": used,
        "memory_percent": used / total * 100,
        "processes": processes or [],
    }


def test_idle_gpu_rows_excludes_unknown_busy_and_process_owned_devices():
    items = [{
        "host": {"id": 1, "name": "gpu-a", "address": "10.0.0.1", "port": 22, "username": "ops", "status": "online", "enabled": True, "allow_terminal": True, "tags": ["training"]},
        "latest": {"collected_at": "2026-08-21T08:00:00Z", "data": {"gpus": [
            gpu("GPU-0", utilization=2, used=512),
            gpu("GPU-1", utilization=70, used=512),
            gpu("GPU-2", utilization=1, used=512, processes=[{"pid": 10, "user": "alice"}]),
            {"index": 3, "uuid": "GPU-3", "memory_total_mib": 24_576, "memory_used_mib": 0, "processes": []},
        ]}},
    }]

    rows = idle_gpu_rows(items, min_memory_mib=20_000, max_utilization=10, max_memory_percent=10, statuses={"online"})

    assert [row["gpu_uuid"] for row in rows] == ["GPU-0"]
    assert rows[0]["memory_available_mib"] == 24_064
    assert rows[0]["allow_terminal"] is True


def test_idle_gpu_api_filters_cached_samples_and_dashboard_summarizes(client, app, admin):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(name="gpu-node", address="10.8.0.1"), fingerprint="SHA256:gpu-node", machine_id="gpu-node")
    data = {
        "collected_at": "2026-08-21T08:00:00Z",
        "cpu": {"usage_percent": 15},
        "memory": {"usage_percent": 20},
        "filesystems": [],
        "gpus": [
            gpu("GPU-0", utilization=3, used=1_024),
            gpu("GPU-1", utilization=60, used=2_048),
        ],
    }
    hosts.ingest_collection(host["id"], CollectionResult(True, data, {}))
    app.extensions["alerts"].emit("test-idle-summary", host["id"], "gpu_power_high", "warning", "测试活动告警")

    response = client.get("/api/idle-gpus?min_memory_mib=20000&max_utilization=5&max_memory_percent=10&host_status=online")
    assert response.status_code == 200
    payload = response.get_json()
    assert [item["gpu_uuid"] for item in payload["items"]] == ["GPU-0"]
    assert payload["filters"]["min_memory_mib"] == 20_000

    dashboard = client.get("/api/dashboard").get_json()
    assert dashboard["resource_summary"] == {
        "host_count": 1,
        "online_hosts": 1,
        "gpu_count": 2,
        "idle_gpu_count": 1,
        "active_alert_count": 1,
    }
    assert dashboard["idle_gpus"][0]["gpu_uuid"] == "GPU-0"


def test_idle_gpu_api_rejects_invalid_numeric_filters(client, admin):
    response = client.get("/api/idle-gpus?max_utilization=not-a-number")
    assert response.status_code == 400
    assert "必须是数字" in response.get_json()["error"]

    response = client.get("/api/idle-gpus?max_memory_percent=101")
    assert response.status_code == 400
    assert "超出范围" in response.get_json()["error"]


def test_idle_gpu_api_does_not_treat_gpu_collection_error_as_available(client, app, admin):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(name="gpu-error", address="10.8.0.9"), fingerprint="SHA256:gpu-error", machine_id="gpu-error")
    hosts.ingest_collection(host["id"], CollectionResult(True, {"collected_at": "2026-08-21T08:00:00Z", "cpu": {}, "memory": {}, "filesystems": [], "gpus": [gpu("GPU-9", utilization=0)]}, {}))
    app.extensions["database"].execute("UPDATE host_runtime SET status='gpu_error' WHERE host_id=?", (host["id"],))

    assert client.get("/api/idle-gpus").get_json()["items"] == []

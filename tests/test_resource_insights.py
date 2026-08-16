from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from monitor.audit import AuditService
from monitor.collector import (
    CollectionResult,
    _parse_docker,
    _parse_gpu,
    _parse_hardware,
    _parse_gpu_xid,
    _parse_limits,
    _parse_smart,
    _parse_software,
    _parse_system_activity,
    flattened_metrics,
)
from monitor.db import Database
from monitor.services import gpu_user_usage

from .conftest import csrf
from .test_hosts_history import host_payload


def sample_data():
    return {
        "collected_at": "2026-08-16T12:00:00Z",
        "cpu": {"usage_percent": 20},
        "memory": {"usage_percent": 30},
        "filesystems": [{"mountpoint": "/", "usage_percent": 40, "inode_usage_percent": 20}],
        "network": [],
        "gpus": [],
    }


def test_gpu_parser_collects_users_health_pcie_throttling_and_xid():
    gpu_text = "0,GPU-abc,RTX 4090,550.90,80,24576,12000,72,285,0"
    process_text = "GPU-abc,1234,python,6000\nGPU-abc,1235,python,1000"
    users = "1234\ttrain\t/srv/training run\tpython train.py --gpu 0\n1235 train\n"
    health = "0,GPU-abc,P2,300,00000000:17:00.0,4,5,8,16,120,1,0x1"
    performance = "GPU 00000000:17:00.0\n    Clocks Event Reasons\n        SW Power Cap : Active\n        Thermal : Not Active\n"
    xid = "NVRM: Xid (PCI:0000:17:00): 79, pid=1234, name=python"

    parsed = _parse_gpu(gpu_text, process_text, users, health, performance, xid)
    assert parsed[0]["processes"][0]["user"] == "train"
    assert parsed[0]["processes"][0]["cwd"] == "/srv/training run"
    assert parsed[0]["processes"][0]["command"] == "python train.py --gpu 0"
    assert parsed[0]["processes"][1]["cwd"] is None
    assert parsed[0]["processes"][1]["command"] == "python"
    assert parsed[0]["power_limit_w"] == 300
    assert parsed[0]["pstate"] == "P2"
    assert parsed[0]["pcie_degraded"] is True
    assert "SW Power Cap" in parsed[0]["throttle_reasons"]
    assert parsed[0]["xid_errors"][0]["code"] == 79
    assert _parse_gpu_xid(xid)[0]["bus"] == "17:00"


def test_smart_permission_failure_is_unavailable_not_disk_failure():
    output = """__DEVICE__:/dev/nvme0
smartctl 7.2 2020-12-30 r5155 [x86_64-linux-6.8.0] (local build)
Smartctl open device: /dev/nvme0 failed: Permission denied
"""

    devices, error = _parse_smart(output)

    assert devices == [{"device": "/dev/nvme0", "health": "未知", "temperature_c": None, "reason": "无权限"}]
    assert error == "无权限"


def test_gpu_parser_marks_residual_memory_and_collects_clock_mode():
    parsed = _parse_gpu(
        "0,GPU-abc,RTX 4090,550.90,5,24576,4096,40,80,20",
        "GPU-abc,9876,python,1024",
        "",
        "0,GPU-abc,P8,450,00000000:17:00.0,1,4,16,16,0,0,0x0,210,1500,2520,Exclusive_Process",
    )

    assert parsed[0]["processes"][0]["pid_exists"] is False
    assert parsed[0]["stale_processes"][0]["pid"] == "9876"
    assert parsed[0]["residual_memory_mib"] == 3072
    assert parsed[0]["residual_memory_suspected"] is True
    assert parsed[0]["clock_current_mhz"] == 210
    assert parsed[0]["clock_application_mhz"] == 1500
    assert parsed[0]["clock_default_application_mhz"] == 2520
    assert parsed[0]["compute_mode"] == "Exclusive_Process"


def test_system_activity_limits_software_and_swap_metric_parsers():
    previous = {"system_activity": {"counters": {"ctxt": 1000, "intr": 2000}}}
    activity = _parse_system_activity("cpu 1 2 3 4\nintr 2060 1 2\nctxt 1030\n", previous, 10)
    assert activity["counters"] == {"intr": 2060, "ctxt": 1030}
    assert activity["per_second"] == {"intr": 6.0, "ctxt": 3.0}

    limits = _parse_limits("open_files_soft\t1024\nopen_files_hard\t1048576\nprocesses_soft\tunlimited\n")
    assert limits["open_files_soft"] == 1024
    assert limits["processes_soft"] is None
    assert _parse_software("kernel\t6.8.0\npython3\tPython 3.12.3\nignored\tsecret\n") == {
        "kernel": "6.8.0",
        "python3": "Python 3.12.3",
    }
    assert ("swap_usage", "", 62.5) in flattened_metrics({"memory": {"swap_usage_percent": 62.5}})


def test_hardware_and_docker_deep_parsers():
    hardware = _parse_hardware("cpu_model\tAMD EPYC\nmemory_total_kib\t1048576\nboard_vendor\tSupermicro\nboard_name\tX12\npci\t0000:17:00.0 VGA controller\n")
    assert hardware["cpu_model"] == "AMD EPYC"
    assert hardware["memory_total_bytes"] == 1024 * 1024 * 1024
    assert hardware["motherboard"] == "Supermicro X12"
    assert hardware["pci_devices"][0]["bus"] == "0000:17:00.0"

    containers, error = _parse_docker(
        '{"ID":"abc123","Names":"trainer","Image":"cuda:12","State":"running"}\n',
        '{"ID":"abc123","CPUPerc":"20.0%","MemPerc":"30.0%","MemUsage":"1GiB / 4GiB","NetIO":"1kB / 2kB","BlockIO":"3kB / 4kB","PIDs":"8"}\n',
        json.dumps({"Id": "abc123", "HostConfig": {"DeviceRequests": [{"Capabilities": [["gpu"]]}], "Memory": 4294967296, "PidsLimit": 128}, "Mounts": [{"Source": "/data", "Destination": "/workspace", "RW": True},], "GraphDriver": {"Name": "overlay2"}}),
    )
    assert error is None
    assert containers[0]["cpu_percent"] == 20
    assert containers[0]["gpu_requests"]
    assert containers[0]["resource_limits"]["memory_bytes"] == 4294967296
    assert containers[0]["mounts"][0]["rw"] is True
    assert containers[0]["block_io"] == "3kB / 4kB"


def test_gpu_user_usage_counts_unique_gpus_and_sums_process_memory():
    items = [{
        "host": {"id": 1, "name": "node-a"},
        "latest": {"data": {"gpus": [
            {"uuid": "GPU-1", "processes": [{"user": "alice", "memory_mib": 100}, {"user": "alice", "memory_mib": 50}, {"user": "bob", "memory_mib": 20}]},
            {"uuid": "GPU-2", "processes": [{"user": "alice", "memory_mib": 30}]},
        ]}},
    }]
    rows = {item["username"]: item for item in gpu_user_usage(items)}
    assert rows["alice"]["gpu_count"] == 2
    assert rows["alice"]["process_count"] == 3
    assert rows["alice"]["memory_mib"] == 180
    assert rows["bob"]["gpu_count"] == 1


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [("authentication_failed", "auth_failed"), ("connection_failed", "ssh_unreachable"), ("timeout", "collection_timeout"), ("remote_command_failed", "command_error")],
)
def test_host_status_is_split_by_ssh_failure_reason(app, error_code, expected):
    host = app.extensions["hosts"].create(host_payload(address=f"10.0.0.{len(expected)}"), fingerprint="SHA256:key", machine_id=f"machine-{expected}")
    outcome = app.extensions["hosts"].ingest_collection(host["id"], CollectionResult(False, {}, {}, error="详细失败原因", error_code=error_code))
    assert outcome["status"] == expected
    current = app.extensions["hosts"].get(host["id"])
    assert current["error_code"] == error_code
    assert current["last_error"] == "详细失败原因"


def test_gpu_health_alerts_emit_and_recover(app):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    app.extensions["monitor_config"].update({"gpu_power_threshold_percent": 95})
    data = {**sample_data(), "gpus": [{
        "index": 0, "uuid": "GPU-1", "power_w": 290, "power_limit_w": 300, "temperature_c": 75, "fan_percent": 0,
        "ecc_corrected": 200, "ecc_uncorrected": 1, "pcie_degraded": True, "pcie_gen": 1, "pcie_gen_max": 4, "pcie_width": 4, "pcie_width_max": 16,
        "throttle_reasons": ["SW Power Cap"], "xid_errors": [{"code": 79}],
    }]}
    app.extensions["alerts"].evaluate_host(host["id"], "online", data)
    types = {row["alert_type"] for row in app.extensions["database"].query_all("SELECT * FROM alerts WHERE state='active'")}
    assert {"gpu_power_high", "gpu_fan_low", "gpu_ecc_error", "gpu_xid_error", "gpu_pcie_degraded", "gpu_throttling"} <= types
    normal = {**data, "gpus": [{"index": 0, "uuid": "GPU-1", "power_w": 10, "power_limit_w": 300, "temperature_c": 30, "fan_percent": 40, "ecc_corrected": 0, "ecc_uncorrected": 0, "pcie_degraded": False, "throttle_reasons": [], "xid_errors": []}]}
    app.extensions["alerts"].evaluate_host(host["id"], "online", normal)
    assert app.extensions["database"].query_one("SELECT COUNT(*) count FROM alerts WHERE state='active'")["count"] == 0


def test_swap_and_residual_gpu_alerts_emit_and_recover(app):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-swap", machine_id="machine-swap")
    app.extensions["monitor_config"].update({"alert_samples": 1, "swap_usage_threshold": 50})
    high = {
        **sample_data(),
        "memory": {"usage_percent": 30, "swap_total": 1024, "swap_used": 800, "swap_usage_percent": 78.1},
        "gpus": [{"index": 0, "uuid": "GPU-swap", "residual_memory_suspected": True, "residual_memory_mib": 512}],
    }
    app.extensions["alerts"].evaluate_host(host["id"], "online", high)
    active = {row["alert_type"] for row in app.extensions["database"].query_all("SELECT alert_type FROM alerts WHERE state='active'")}
    assert {"swap_usage_high", "gpu_residual_memory"} <= active

    normal = {
        **high,
        "memory": {"usage_percent": 30, "swap_total": 1024, "swap_used": 100, "swap_usage_percent": 9.8},
        "gpus": [{"index": 0, "uuid": "GPU-swap", "residual_memory_suspected": False, "residual_memory_mib": 0}],
    }
    app.extensions["alerts"].evaluate_host(host["id"], "online", normal)
    assert app.extensions["database"].query_one("SELECT COUNT(*) count FROM alerts WHERE state='active'")["count"] == 0


def test_assets_saved_views_and_csv_api(client, app, admin):
    host = app.extensions["hosts"].create({**host_payload(), "asset_location": "机房 A", "asset_owner": "张三", "warranty_expires": "2028-01-02"}, fingerprint="SHA256:key-one", machine_id="machine-one")
    data = {**sample_data(), "hardware": {"cpu_model": "AMD EPYC", "memory_total_bytes": 1024, "motherboard": "Board", "pci_devices": [{"bus": "0000:17:00.0", "description": "GPU slot"}]}, "gpus": [{"index": 0, "name": "RTX 4090"}]}
    app.extensions["hosts"].ingest_collection(host["id"], CollectionResult(True, data, {}))
    assets = client.get("/api/hardware-assets")
    assert assets.status_code == 200 and assets.get_json()["items"][0]["asset_owner"] == "张三"
    csv_response = client.get("/api/hardware-assets/export")
    assert csv_response.status_code == 200 and "GPU 0: RTX 4090" in csv_response.get_data(as_text=True)

    saved = client.post("/api/saved-views", json={"page": "dashboard", "name": "训练机器", "filters": {"status": "online", "tags": ["GPU"]}}, headers=csrf(admin))
    assert saved.status_code == 201
    listed = client.get("/api/saved-views?page=dashboard").get_json()["items"]
    assert listed[0]["filters"]["status"] == "online" and "filters_json" not in listed[0]
    invalid = client.post("/api/saved-views", json={"page": "dashboard", "name": "bad", "filters": {"command": "rm"}}, headers=csrf(admin))
    assert invalid.status_code == 400
    assert client.delete(f"/api/saved-views/{listed[0]['id']}", json={}, headers=csrf(admin)).status_code == 200


def test_fingerprint_confirmation_reconnects_and_requires_observed_value(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:old", machine_id="machine-one")
    observed = "SHA256:new"
    calls = []

    class FakeSSHClient:
        def __init__(self, candidate, *_args):
            self.candidate = candidate

        def connect(self):
            calls.append(self.candidate.get("fingerprint"))
            if self.candidate.get("fingerprint") != observed:
                raise AssertionError("confirmation must use exact observed fingerprint")
            return observed

        def run(self, *_args):
            return SimpleNamespace(exit_code=0, stderr="", stdout="node\nmachine-one\n")

        def close(self):
            pass

    monkeypatch.setattr("monitor.app.SSHClient", FakeSSHClient)
    elevated = client.post("/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin))
    assert elevated.status_code == 200
    response = client.post(f"/api/hosts/{host['id']}/fingerprint/confirm", json={"observed": observed}, headers=csrf(admin))
    assert response.status_code == 200
    assert calls == [observed]
    assert app.extensions["hosts"].get(host["id"])["fingerprint"] == observed


def test_health_inspection_api_and_fault_status_detail(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    monkeypatch.setattr(app.extensions["operations"], "health_inspection", lambda _host: {"checks": [{"key": "nvidia", "title": "NVIDIA 驱动", "status": "passed", "summary": "正常", "details": ""}], "passed": 1, "warnings": 0, "unavailable": 0, "inspected_at": "2026-08-16T12:00:00Z"})
    response = client.post(f"/api/hosts/{host['id']}/health-inspection", json={}, headers=csrf(admin))
    assert response.status_code == 200 and response.get_json()["passed"] == 1
    app.extensions["hosts"].status(host["id"], "auth_failed", error="密码错误", error_code="authentication_failed")
    faults = client.get("/api/faults")
    assert faults.status_code == 200 and faults.get_json()["items"][0]["issues"][0]["last_error"] == "密码错误"


def test_audit_changes_are_structurally_redacted_and_schema6_and_pwa(app, client, admin):
    audit_id = AuditService(app.extensions["database"]).write("test", actor=admin, changes={"password": {"before": "old", "after": "new"}, "nested": [{"sendkey": "SCT-secret"}]})
    row = app.extensions["database"].query_one("SELECT changes_json FROM audit_logs WHERE id=?", (audit_id,))
    assert "old" not in row["changes_json"] and "SCT-secret" not in row["changes_json"]
    assert app.extensions["database"].query_one("PRAGMA user_version")[0] == 6
    page = client.get("/")
    assert page.status_code == 200 and 'rel="manifest"' in page.get_data(as_text=True)
    assert client.get("/static/manifest.webmanifest").status_code == 200
    assert client.get("/static/service-worker.js").status_code == 200


def test_current_snapshot_export_does_not_contain_host_secrets(client, app, admin):
    secret = "snapshot-password-must-not-leak"
    app.extensions["hosts"].create(
        {**host_payload(), "auth_secret": secret, "sudo_password": "sudo-secret"},
        fingerprint="SHA256:key-snapshot",
        machine_id="machine-snapshot",
    )

    response = client.get("/api/snapshots/current")

    assert response.status_code == 200
    assert response.headers["Content-Disposition"].endswith(".json")
    text = response.get_data(as_text=True)
    assert secret not in text and "sudo-secret" not in text
    exported_host = json.loads(text)["hosts"][0]["host"]
    assert "auth_secret" not in exported_host and exported_host["auth_secret_configured"] is True

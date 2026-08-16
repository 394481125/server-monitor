from __future__ import annotations

import io
import json
from types import SimpleNamespace

from monitor.collector import (
    _parse_cpu,
    _parse_inode_filesystems,
    _parse_listening_ports,
    _parse_tcp_connections,
    flattened_metrics,
)
from monitor.services import host_transfer_rows, parse_host_import
from monitor.ssh_client import SSHFingerprintError
from monitor.utils import utc_iso

from .conftest import csrf
from .test_hosts_history import host_payload


def elevate(client, admin):
    response = client.post(
        "/api/auth/elevate",
        json={"password": "TemporaryPass456"},
        headers=csrf(admin),
    )
    assert response.status_code == 200, response.get_json()


def test_host_transfer_parses_json_and_csv_without_exporting_secrets():
    exported = host_transfer_rows(
        [{**host_payload(), "auth_secret": "plain-secret", "tags": ["GPU", "生产"], "enabled": True}],
        csv_mode=True,
    )
    assert exported[0]["auth_secret"] == ""
    assert exported[0]["tags"] == "GPU,生产"

    json_rows = parse_host_import(
        json.dumps({"hosts": [{**host_payload(), "enabled": True}]}).encode(),
        "hosts.json",
    )
    csv_rows = parse_host_import(
        "name,address,port,username,auth_type,auth_secret,tags,enabled\nnode-csv,10.0.0.2,2222,ops,password,secret,GPU;测试,true\n".encode(),
        "hosts.csv",
    )
    assert json_rows[0]["port"] == 22
    assert csv_rows[0]["port"] == 2222
    assert csv_rows[0]["tags"] == ["GPU", "测试"]
    assert csv_rows[0]["enabled"] is True


def test_host_import_export_and_partial_result_api(client, app, admin, monkeypatch):
    class FakeSSHClient:
        def __init__(self, candidate, *_args):
            self.candidate = candidate

        def connect(self):
            return f"SHA256:{self.candidate['address'].replace('.', '-')}"

        def run(self, *_args):
            suffix = self.candidate["address"].split(".")[-1]
            return SimpleNamespace(exit_code=0, stderr="", stdout=f"node-{suffix}\nmachine-{suffix}-identity\n")

        def close(self):
            pass

    monkeypatch.setattr("monitor.app.SSHClient", FakeSSHClient)
    elevate(client, admin)
    content = json.dumps(
        {
            "hosts": [
                {**host_payload("node-10", "10.0.0.10"), "tags": ["GPU"]},
                {**host_payload("node-10-duplicate", "10.0.0.10")},
            ]
        },
        ensure_ascii=False,
    ).encode()
    response = client.post(
        "/api/hosts/import",
        data={"file": (io.BytesIO(content), "hosts.json")},
        headers=csrf(admin),
        content_type="multipart/form-data",
    )
    assert response.status_code == 207, response.get_json()
    result = response.get_json()
    assert result["success_count"] == 1 and result["failure_count"] == 1
    assert app.extensions["hosts"].list()[0]["name"] == "node-10"

    exported = client.get("/api/hosts/export?format=json")
    assert exported.status_code == 200
    assert b"secret" not in exported.data
    payload = json.loads(exported.data)
    assert payload["credentials_included"] is False
    assert "auth_secret" not in payload["hosts"][0]

    template = client.get("/api/hosts/import-template?format=json")
    assert template.status_code == 200
    assert json.loads(template.data)["hosts"][0]["auth_secret"] == ""


def test_batch_ssh_test_reports_fingerprint_mismatch_without_trusting_it(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(
        host_payload(), fingerprint="SHA256:old", machine_id="machine-one"
    )

    class ChangedFingerprintSSH:
        def __init__(self, *_args):
            pass

        def connect(self):
            raise SSHFingerprintError(
                "SSH 主机指纹与已记录值不一致",
                expected="SHA256:old",
                observed="SHA256:new",
            )

        def close(self):
            pass

    monkeypatch.setattr("monitor.app.SSHClient", ChangedFingerprintSSH)
    elevate(client, admin)
    response = client.post(
        "/api/hosts/batch-test",
        json={"host_ids": [host["id"]]},
        headers=csrf(admin),
    )
    assert response.status_code == 200, response.get_json()
    item = response.get_json()["results"][0]
    assert item["status"] == "fingerprint_mismatch"
    assert item["expected"] == "SHA256:old" and item["observed"] == "SHA256:new"
    assert app.extensions["hosts"].get(host["id"])["fingerprint"] == "SHA256:old"


def test_iowait_inode_tcp_and_listener_parsers_feed_history_metrics():
    first = _parse_cpu("cpu 100 0 100 800 100 0 0 0", None)
    second = _parse_cpu(
        "cpu 150 0 150 900 150 0 0 0",
        {"cpu": {"cpu_counts": first["cpu_counts"]}},
    )
    assert second["iowait_percent"] == 20.0

    inodes = _parse_inode_filesystems(
        "Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/sda1 1000 900 100 90% /\n"
    )
    tcp = _parse_tcp_connections("total\t20\nestablished\t7\ntime_wait\t5")
    listeners = _parse_listening_ports("tcp\t22\t0.0.0.0:22\nudp\t53\t127.0.0.53:53")
    assert inodes["/"]["inode_usage_percent"] == 90.0
    assert tcp == {"total": 20, "established": 7, "time_wait": 5}
    assert [item["port"] for item in listeners] == [22, 53]

    metrics = flattened_metrics(
        {
            "cpu": second,
            "memory": {"usage_percent": 30},
            "filesystems": [{"mountpoint": "/", "usage_percent": 80, **inodes["/"]}],
            "tcp": tcp,
            "listening_ports": listeners,
        }
    )
    names = {(metric, object_key) for metric, object_key, _value in metrics}
    assert ("cpu_iowait", "") in names
    assert ("filesystem_inode_usage", "/") in names
    assert ("tcp_established", "") in names
    assert ("listening_ports", "") in names

    guest_first = _parse_cpu("cpu 100 0 100 800 0 0 0 0 500 200", None)
    guest_second = _parse_cpu("cpu 150 0 150 900 0 0 0 0 900 400", guest_first)
    assert guest_second["usage_percent"] == 50.0


def test_mountpoint_threshold_api_and_alert_lifecycle(client, app, admin):
    host = app.extensions["hosts"].create(
        host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one"
    )
    saved = client.put(
        f"/api/hosts/{host['id']}/mount-thresholds",
        json={"rules": [{"mountpoint": "/data", "usage_threshold": 90, "inode_threshold": 95}]},
        headers=csrf(admin),
    )
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["items"][0]["usage_threshold"] == 90

    app.extensions["monitor_config"].update({"alert_samples": 1, "alert_hysteresis": 3})
    hosts = app.extensions["hosts"]
    alerts = app.extensions["alerts"]
    high = {
        "collected_at": utc_iso(),
        "cpu": {"usage_percent": 10, "iowait_percent": 1},
        "memory": {"usage_percent": 20},
        "filesystems": [{"mountpoint": "/data", "usage_percent": 91, "inode_usage_percent": 94}],
        "network": [], "gpus": [], "listening_ports": [],
    }
    hosts.ingest_collection(host["id"], SimpleNamespace(core_ok=True, data=high, optional_errors={}, fingerprint=None, error=None))
    alerts.evaluate_host(host["id"], "online", high)
    active = app.extensions["database"].query_one(
        "SELECT * FROM alerts WHERE host_id=? AND alert_type='filesystem_usage_high' AND state='active'",
        (host["id"],),
    )
    assert active and "90.0%" in active["summary"]
    assert app.extensions["database"].query_one(
        "SELECT * FROM alerts WHERE host_id=? AND alert_type='filesystem_inode_high'",
        (host["id"],),
    ) is None

    normal = {**high, "collected_at": "2099-01-01T00:00:00.000Z", "filesystems": [{"mountpoint": "/data", "usage_percent": 80, "inode_usage_percent": 80}]}
    hosts.ingest_collection(host["id"], SimpleNamespace(core_ok=True, data=normal, optional_errors={}, fingerprint=None, error=None))
    alerts.evaluate_host(host["id"], "online", normal)
    assert app.extensions["database"].query_one("SELECT state FROM alerts WHERE id=?", (active["id"],))["state"] == "recovered"


def test_alert_server_filters_export_and_fault_aggregation(client, app, admin):
    host = app.extensions["hosts"].create(
        host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one"
    )
    app.extensions["hosts"].status(host["id"], "offline", error="timeout")
    app.extensions["alerts"].emit(
        f"host-offline:{host['id']}", host["id"], "host_offline", "critical", "主机离线测试"
    )

    filtered = client.get(f"/api/alerts?host_id={host['id']}&state=active&severity=critical&search=离线")
    assert filtered.status_code == 200
    assert filtered.get_json()["total"] == 1
    assert filtered.get_json()["items"][0]["host_name"] == "node-1"

    exported = client.get(f"/api/alerts/export?host_id={host['id']}&state=active")
    assert exported.status_code == 200
    assert "主机离线测试" in exported.get_data(as_text=True)

    faults = client.get("/api/faults")
    assert faults.status_code == 200
    assert faults.get_json()["total"] == 1
    assert faults.get_json()["items"][0]["host"]["id"] == host["id"]

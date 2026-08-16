from __future__ import annotations

from datetime import timedelta

import pytest

from monitor.collector import CollectionResult, _parse_block_devices, _parse_cpu, _parse_cpu_temperature, _parse_diskstats, _parse_network, _parse_docker, _parse_smart
from monitor.services import HistoryService, ServiceError
from monitor.utils import utc_iso, utc_now


def host_payload(name="node-1", address="10.0.0.1"):
    return {"name": name, "address": address, "port": 22, "username": "monitor", "auth_type": "password", "auth_secret": "secret"}


def test_host_identity_and_endpoint_are_unique(app):
    hosts = app.extensions["hosts"]
    first = hosts.create({**host_payload(), "sudo_password": "remote-sudo-password"}, fingerprint="SHA256:key-one", machine_id="machine-one")
    assert first["auth_secret_configured"] is True
    assert first["sudo_password_configured"] is True
    with pytest.raises(ServiceError, match="地址"):
        hosts.create(host_payload(name="duplicate"), fingerprint="SHA256:key-two", machine_id="machine-two")
    with pytest.raises(ServiceError, match="物理主机"):
        hosts.create(host_payload(name="alias", address="node-one.local"), fingerprint="SHA256:key-one", machine_id="machine-one")


def test_soft_delete_clears_secrets_and_releases_uniqueness(app):
    hosts = app.extensions["hosts"]
    first = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    hosts.soft_delete(first["id"])
    row = app.extensions["database"].query_one("SELECT * FROM hosts WHERE id=?", (first["id"],))
    assert row["deleted_at"]
    assert row["auth_secret"] is None
    replacement = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    assert replacement["id"] != first["id"]


def test_switching_ssh_authentication_clears_unused_credentials(app):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    hosts.update(
        host["id"],
        {"auth_type": "key", "private_key": "private-key"},
        fingerprint="SHA256:key-one",
        machine_id="machine-one",
    )
    key_row = app.extensions["database"].query_one("SELECT auth_secret,private_key,private_key_passphrase FROM hosts WHERE id=?", (host["id"],))
    assert key_row["auth_secret"] is None and key_row["private_key"] is not None

    hosts.update(
        host["id"],
        {"auth_type": "password", "auth_secret": "replacement-password"},
        fingerprint="SHA256:key-one",
        machine_id="machine-one",
    )
    password_row = app.extensions["database"].query_one("SELECT auth_secret,private_key,private_key_passphrase FROM hosts WHERE id=?", (host["id"],))
    assert password_row["auth_secret"] is not None
    assert password_row["private_key"] is None and password_row["private_key_passphrase"] is None


def test_sudo_password_rejects_newlines(app):
    with pytest.raises(ServiceError, match="sudo_password"):
        app.extensions["hosts"].create(
            {**host_payload(), "sudo_password": "invalid\npassword"},
            fingerprint="SHA256:key-one",
            machine_id="machine-one",
        )


def test_status_requires_three_failed_cycles_and_one_success(app):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    for cycle in (1, 2):
        outcome = hosts.ingest_collection(host["id"], CollectionResult(False, {}, {}, error="timeout"))
        assert outcome["status"] == "unknown"
        assert outcome["failure_cycles"] == cycle
    outcome = hosts.ingest_collection(host["id"], CollectionResult(False, {}, {}, error="timeout"))
    assert outcome["status"] == "offline"
    data = sample_data()
    outcome = hosts.ingest_collection(host["id"], CollectionResult(True, data, {}))
    assert outcome["status"] == "online"
    assert hosts.get(host["id"])["failure_cycles"] == 0


def sample_data(timestamp=None):
    return {
        "collected_at": timestamp or utc_iso(),
        "cpu": {"usage_percent": 20.0},
        "memory": {"usage_percent": 30.0},
        "filesystems": [{"mountpoint": "/", "usage_percent": 40.0}],
        "network": [],
        "gpus": [],
    }


def test_counter_parsers_do_not_emit_negative_rates():
    previous = {"network_counters": {"eth0": {"rx_bytes": 1000, "tx_bytes": 2000}}}
    text = "eth0: 900 0 0 0 0 0 0 0 1900 0 0 0 0 0 0 0"
    network = _parse_network(text, previous, 10)[0]
    assert network["rx_rate"] is None and network["tx_rate"] is None
    disk_text = "8 0 sda 5 0 10 0 5 0 10 0 0 10 0 0"
    prior_disk = {"disk_counters": {"sda": {"reads": 6, "sectors_read": 11, "writes": 6, "sectors_written": 11, "io_ms": 11}}}
    disk = _parse_diskstats(disk_text, prior_disk, 10)[0]
    assert disk["read_bytes_rate"] is None


def test_block_device_parser_keeps_physical_disks_and_drops_partitions():
    devices = _parse_block_devices("sda disk 100000\nsda1 part 50000\nnvme0n1 disk 200000\nnvme0n1p1 part 100000\nloop0 loop 30000")
    assert devices == [{"name": "sda", "size": 100000}, {"name": "nvme0n1", "size": 200000}]


def test_cpu_first_sample_unknown_then_delta():
    first = "cpu  100 0 100 800 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0"
    parsed = _parse_cpu(first, None)
    assert parsed["usage_percent"] is None
    second = "cpu  150 0 150 900 0 0 0 0\ncpu0 75 0 75 450 0 0 0 0"
    parsed_second = _parse_cpu(second, {"cpu_counts": parsed["cpu_counts"]})
    assert parsed_second["usage_percent"] == 50.0
    nested_second = _parse_cpu(second, {"cpu": {"cpu_counts": parsed["cpu_counts"]}})
    assert nested_second["usage_percent"] == 50.0


def test_history_aggregation_is_idempotent_and_deletes_source_after_commit(app):
    database = app.extensions["database"]
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    base = utc_now() - timedelta(hours=2)
    for index, value in enumerate((10.0, 20.0, 30.0)):
        database.execute(
            "INSERT INTO metric_points(host_id,metric,object_key,kind,ts,value) VALUES(?,?,?,?,?,?)",
            (host["id"], "cpu_usage", "", "raw", utc_iso(base + timedelta(seconds=index * 10)), value),
        )
    history = HistoryService(database)
    first = history.aggregate(now=utc_now(), mid_seconds=60, long_seconds=300)
    assert first["mid"] >= 1
    assert database.query_one("SELECT COUNT(*) count FROM metric_points WHERE kind='raw'")["count"] == 0
    count = database.query_one("SELECT COUNT(*) count FROM metric_points WHERE kind='mid'")["count"]
    history.aggregate(now=utc_now(), mid_seconds=60, long_seconds=300)
    assert database.query_one("SELECT COUNT(*) count FROM metric_points WHERE kind='mid'")["count"] == count


def test_history_rolls_through_short_and_medium_retention_tiers(app):
    database = app.extensions["database"]
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    now = utc_now()
    for age, value in ((timedelta(minutes=30), 30.0), (timedelta(hours=8), 80.0)):
        database.execute(
            "INSERT INTO metric_points(host_id,metric,object_key,kind,ts,value) VALUES(?,?,?,?,?,?)",
            (host["id"], "cpu_usage", "", "raw", utc_iso(now - age), value),
        )

    counts = HistoryService(database).aggregate(
        now=now,
        mid_seconds=60,
        long_seconds=300,
        raw_retention_minutes=15,
        mid_retention_hours=6,
    )

    assert counts["mid"] == 2 and counts["long"] == 1
    assert database.query_one("SELECT COUNT(*) count FROM metric_points WHERE kind='raw'")["count"] == 0
    assert database.query_one("SELECT COUNT(*) count FROM metric_points WHERE kind='mid'")["count"] == 1
    assert database.query_one("SELECT COUNT(*) count FROM metric_points WHERE kind='long'")["count"] == 1
    points = hosts.history(host["id"], "cpu_usage", "", utc_iso(now - timedelta(hours=9)), utc_iso(now))
    assert [point["value"] for point in points] == [80.0, 30.0]


def test_history_cleanup_removes_only_expired_terminal_records(app):
    database = app.extensions["database"]
    now = utc_now()
    old_log = utc_iso(now - timedelta(days=31))
    old_collection = utc_iso(now - timedelta(hours=2))
    recent = utc_iso(now - timedelta(minutes=5))
    database.execute("INSERT INTO tasks(id,task_type,state,created_at) VALUES(?,?,?,?)", ("old-collection", "collection", "success", old_collection))
    database.execute("INSERT INTO tasks(id,task_type,state,created_at) VALUES(?,?,?,?)", ("recent-collection", "collection", "success", recent))
    database.execute(
        "INSERT INTO audit_logs(ts,action,success,summary) VALUES(?,?,?,?)",
        (old_log, "old", 1, "old audit"),
    )
    database.execute(
        "INSERT INTO schedule_jobs(id,physical_id,gpu_uuid,mode,command_summary,state,attempt,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("old-job", "physical", "gpu", "tmux", "command", "success", 1, old_log, old_log),
    )
    database.execute(
        "INSERT INTO schedule_jobs(id,physical_id,gpu_uuid,mode,command_summary,state,attempt,started_at) VALUES(?,?,?,?,?,?,?,?)",
        ("running-job", "physical", "gpu", "tmux", "command", "running", 1, old_log),
    )
    database.execute("INSERT INTO notifications(channel,success,created_at) VALUES(?,?,?)", ("test", 1, old_log))
    database.execute(
        "INSERT INTO alerts(alert_key,alert_type,state,severity,summary,created_at,recovered_at) VALUES(?,?,?,?,?,?,?)",
        ("recovered", "test", "recovered", "info", "done", old_log, old_log),
    )

    counts = HistoryService(database).cleanup(
        metric_retention_days=7,
        log_retention_days=30,
        collection_task_retention_minutes=60,
        now=now,
    )

    assert counts["collection_tasks"] == 1
    assert counts["logs"] == 1 and counts["schedule_jobs"] == 1
    assert counts["notifications"] == 1 and counts["recovered_alerts"] == 1
    assert database.query_one("SELECT id FROM tasks WHERE id='recent-collection'")
    assert database.query_one("SELECT id FROM schedule_jobs WHERE id='running-job'")


def test_disabled_host_does_not_collect(app):
    hosts = app.extensions["hosts"]
    host = hosts.create({**host_payload(), "enabled": False}, fingerprint="SHA256:key-one", machine_id="machine-one")
    outcome = hosts.ingest_collection(host["id"], CollectionResult(True, sample_data(), {}))
    assert outcome["status"] == "disabled"
    assert hosts.latest(host["id"]) is None


def test_optional_smart_and_docker_parsers_keep_capability_status():
    smart, error = _parse_smart("__DEVICE__:/dev/sda\nSMART overall-health self-assessment test result: PASSED\nCurrent Drive Temperature: 42 C")
    assert not error and smart[0]["health"] == "良好" and smart[0]["temperature_c"] == 42
    smart, error = _parse_smart("__DEVICE__:/dev/sdb\nSMART overall-health self-assessment test result: FAILED")
    assert smart[0]["health"] == "故障"
    docker, error = _parse_docker('{"ID":"abc","Names":"demo","State":"running"}', '{"ID":"abc","Name":"demo","CPUPerc":"2.0%","MemPerc":"3.0%"}')
    assert not error and docker[0]["cpu_percent"] == 2 and docker[0]["memory_percent"] == 3


def test_cpu_temperature_parser_reads_recognized_sysfs_sensors():
    sysfs = "coretemp\tPackage id 0\t51000\ncoretemp\tCore 1\t55000\nacpitz\ttemp1\t90000"
    assert _parse_cpu_temperature("", sysfs) == 55.0


def test_nullable_scheduler_process_guard_keeps_inherit_semantics(app):
    hosts = app.extensions["hosts"]
    host = hosts.create(
        {**host_payload(), "scheduler_process_guard": None},
        fingerprint="SHA256:key-one",
        machine_id="machine-one",
    )
    assert host["scheduler_process_guard"] is None


def test_docker_disabled_is_passed_to_remote_collector(app, monkeypatch):
    from monitor.collector import Collector
    from types import SimpleNamespace

    commands = []

    class FakeSSHClient:
        def __init__(self, *_args):
            pass

        def connect(self):
            return "SHA256:key-one"

        def run(self, command, *_args):
            commands.append(command)
            return SimpleNamespace(exit_code=1, stdout="", stderr="expected parser stop")

        def close(self):
            pass

    monkeypatch.setattr("monitor.collector.SSHClient", FakeSSHClient)
    result = Collector(app.extensions["secret_box"], app.extensions["monitor_config"].all()).collect(
        {**host_payload(), "docker_enabled": False}
    )
    assert result.core_ok is False
    assert commands and commands[0].startswith("SERVER_MONITOR_DOCKER_ENABLED=0 ")

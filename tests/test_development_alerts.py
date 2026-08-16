from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from monitor.db import Database
from monitor.development import DevelopmentService
from monitor.operations import OperationError, _parse_development_stack, _parse_gpu_diagnostics

from .conftest import csrf, login
from .test_hosts_history import host_payload


class FakeRuns:
    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.commands: list[str] = []

    def run(self, _host, command, _timeout, _limit=None):
        self.commands.append(command)
        output = self.outputs.pop(0) if self.outputs else ""
        return SimpleNamespace(
            stdout=output,
            stderr="",
            exit_code=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )


class FakeConfig:
    @staticmethod
    def all():
        return {
            "collection_timeout": 15, "install_timeout": 120, "schedule_output_limit": 1024 * 1024,
            "scan_timeout_seconds": 60, "scan_max_depth": 8, "scan_result_limit": 100,
            "scan_minimum_mib": 1024, "environment_inventory_timeout": 60,
        }


STACK_OUTPUT = """__SM_OS__\tubuntu\t24.04
__SM_TOOL__\tpython3\t/usr/bin/python3\tPython 3.12.3
__SM_TOOL__\tpython3.12\t/usr/bin/python3.12\tPython 3.12.3
__SM_TOOL__\tconda\t/opt/conda/bin/conda\tconda 24.5
__SM_TOOL__\tuv\t/home/ops/.local/bin/uv\tuv 0.4
__SM_DRIVER__\t550.90
__SM_RECOMMENDED_DRIVER__\tnvidia-driver-550
__SM_NVCC__\t12.4
__SM_CUDNN__\tlibcudnn9-cuda-12\t9.2.0
__SM_CUDNN_LIBRARY__\tlibcudnn.so.9\t/lib/x86_64-linux-gnu/libcudnn.so.9
"""

NO_PYTHON_STACK = """__SM_OS__\tubuntu\t24.04
__SM_TOOL__\tconda\t/opt/conda/bin/conda\tconda 24.5
__SM_TOOL__\tuv\t/home/ops/.local/bin/uv\tuv 0.4
"""


def test_stack_and_gpu_diagnostic_parsers_are_capability_aware():
    stack = _parse_development_stack(STACK_OUTPUT + "__SM_RECOMMENDED_DRIVER_NOTE__\t自动推荐值暂不可用（远端 ubuntu-drivers 超过 4 秒，已跳过）\n")
    assert stack["os"] == {"id": "ubuntu", "version": "24.04"}
    assert stack["gpu"]["recommended_driver"] == "nvidia-driver-550"
    assert stack["cuda"]["nvcc_version"] == "12.4"
    assert stack["cuda"]["cudnn_libraries"][0]["name"] == "libcudnn.so.9"
    assert {item["command"] for item in stack["python_versions"]} == {"python3", "python3.12"}
    assert stack["gpu"]["recommendation_note"] == "自动推荐值暂不可用（远端 ubuntu-drivers 超过 4 秒，已跳过）"

    diagnostics = _parse_gpu_diagnostics(
        "__SM_GPU_AVAILABLE__\t1\n"
        "__SM_GPU__\t0\tGPU-1\tRTX 4090\t550.90\t61\t24564\t800\t96\n"
        "__SM_ECC_BEGIN__\nECC Mode: N/A\n__SM_SECTION_END__\n"
        "__SM_TOPOLOGY_BEGIN__\nGPU0 X\n__SM_SECTION_END__\n"
        "__SM_NVLINK_BEGIN__\nLink 0: 25 GB/s\n__SM_SECTION_END__\n"
    )
    assert diagnostics["available"] is True
    assert diagnostics["gpus"][0]["high_util_low_memory"] is True
    assert diagnostics["fragmentation"]["available"] is False
    assert "Link 0" in diagnostics["nvlink"]


def test_optional_development_probes_are_bounded_and_degrade_to_warnings():
    runs = FakeRuns([STACK_OUTPUT])
    service = DevelopmentService(runs, FakeConfig())
    service.development_stack({})
    command = runs.commands[0]
    assert "timeout --signal=TERM --kill-after=1s" in command
    assert "sm_run 4s ubuntu-drivers devices" in command
    assert '"$HOME/miniconda3/bin/conda"' in command
    assert '"$HOME/anaconda/bin/conda"' in command
    assert "/usr/local/cuda/bin/nvcc" in command
    assert "__SM_WARNING__" in command
    assert "NVIDIA 驱动状态读取失败" in command

    diagnostics = _parse_gpu_diagnostics("__SM_GPU_AVAILABLE__\t1\n__SM_DIAGNOSTIC_WARNING__\tECC 探测超时，已跳过\n")
    assert "ECC 探测超时，已跳过" in diagnostics["notes"]


def test_environment_and_system_plans_reject_shell_injection():
    runs = FakeRuns([STACK_OUTPUT, STACK_OUTPUT, STACK_OUTPUT, STACK_OUTPUT])
    service = DevelopmentService(runs, FakeConfig())
    plan = service.environment_plan(
        {},
        {
            "backend": "venv",
            "action": "create",
            "path": "/srv/envs/train",
            "python": "python3.12",
            "packages": ["numpy==2.1", "pandas>=2"],
            "pytorch": "cu124",
        },
    )
    assert "/usr/bin/python3.12 -m venv" in plan["script"]
    assert "download.pytorch.org/whl/cu124" in plan["script"]
    assert plan["remote_execution"] is True

    with pytest.raises(OperationError):
        service.environment_plan({}, {"backend": "venv", "action": "create", "path": "/srv/envs/x;reboot", "python": "python3.12", "packages": ["ok;id"]})
    with pytest.raises(OperationError):
        service.system_plan({}, {"kind": "apt", "action": "install", "package": "curl;id"})

    driver = service.system_plan({}, {"kind": "gpu-driver"})
    assert "nvidia-driver-550" in driver["script"]
    assert driver["remote_execution"] is False


def test_environment_backends_and_system_plan_matrix_are_bounded():
    runs = FakeRuns([STACK_OUTPUT] * 12)
    service = DevelopmentService(runs, FakeConfig())

    conda = service.environment_plan(
        {}, {"backend": "conda", "action": "create", "path": "/srv/envs/conda", "python": "3.12", "packages": [], "pytorch": "none"}
    )
    assert 'conda create -y -p "$target" python=3.12' in conda["script"]
    assert conda["remote_execution"] is True

    uv = service.environment_plan(
        {}, {"backend": "uv", "action": "install", "path": "/srv/envs/uv", "python": "python3.12", "packages": ["ruff"], "pytorch": "none", "confirmed_path": True}
    )
    assert 'uv pip install --python "$target/bin/python" ruff' in uv["script"]

    remove = service.environment_plan(
        {}, {"backend": "venv", "action": "remove", "path": "/srv/envs/old", "python": "python3.12", "packages": [], "pytorch": "none", "confirmed_path": True}
    )
    assert "rm -rf -- \"$target\"" in remove["script"]
    assert remove["remote_execution"] is False

    for action in ("update", "upgrade", "autofix", "install", "remove", "purge"):
        payload = {"kind": "apt", "action": action}
        if action in {"install", "remove", "purge"}:
            payload["package"] = "build-essential"
        plan = service.system_plan({}, payload)
        assert plan["remote_execution"] is False
        assert "APT" in plan["title"]

    assert "cuda-toolkit-12-4" in service.system_plan({}, {"kind": "cuda", "version": "12.4"})["script"]
    assert "libcudnn9-cuda-12" in service.system_plan({}, {"kind": "cudnn", "version": "9-cuda12"})["script"]


def test_environment_backup_and_dependency_conflict_detection():
    service = DevelopmentService(FakeRuns([]), FakeConfig())
    plan = service.environment_backup_plan({"backend": "venv", "path": "/srv/envs/train"})
    assert plan["remote_execution"] is False
    assert "tar -C" in plan["script"] and "sha256sum" in plan["script"]
    assert "/srv/envs/train-backup" in plan["script"]
    with pytest.raises(OperationError, match="过于宽泛"):
        service.environment_backup_plan({"backend": "conda", "path": "/opt"})
    with pytest.raises(OperationError, match="后端"):
        service.environment_backup_plan({"backend": "pipenv", "path": "/srv/envs/train"})

    executor = DevelopmentService(FakeRuns([STACK_OUTPUT, "__SM_DEPENDENCY_CONFLICT__\nconflicting-package"]), FakeConfig())
    result = executor.execute_environment_plan({}, {
        "backend": "venv", "action": "install", "path": "/srv/envs/train", "python": "python3.12",
        "packages": ["numpy"], "pytorch": "none", "confirmed_path": True,
    })
    assert result["dependency_conflict"] is True
    assert "pip check" in result["plan"]["script"]


def test_venv_requires_detected_python_and_delete_requires_script_mode():
    service = DevelopmentService(FakeRuns([NO_PYTHON_STACK]), FakeConfig())
    with pytest.raises(OperationError, match="未检测到可用 Python"):
        service.environment_plan(
            {}, {"backend": "venv", "action": "create", "path": "/srv/envs/missing", "python": "python3", "packages": [], "pytorch": "none"}
        )


def test_directory_scans_quote_path_and_sort_results():
    runs = FakeRuns(["4096\t/srv/data\n", "10\t1.0\t/srv/data/a\x00100\t2.0\t/srv/data/b\x00"])
    service = DevelopmentService(runs, FakeConfig())
    assert service.directory_usage({}, "/srv/data")["bytes"] == 4096
    result = service.large_files({}, "/srv/data", 1024 * 1024, 10)
    assert [item["path"] for item in result["items"]] == ["/srv/data/b", "/srv/data/a"]
    assert "du -s -x -B1 --apparent-size" in runs.commands[0]
    assert "timeout --signal=TERM --kill-after=2s 60s" in runs.commands[0]
    assert "-xdev -maxdepth 8" in runs.commands[1]
    with pytest.raises(OperationError):
        service.large_files({}, "/srv/data\nreboot", 1024 * 1024, 10)


def test_directory_scans_return_partial_results_after_remote_soft_timeout():
    runs = FakeRuns([
        "__SM_SCAN_STATUS__\t124\n",
        "104857600\t1.0\t/srv/data/model.bin\x00__SM_SCAN_STATUS__\t124\x00",
    ])
    service = DevelopmentService(runs, FakeConfig())
    usage = service.directory_usage({}, "/srv/data", 30)
    assert usage["bytes"] is None
    assert usage["partial"] is True and usage["timed_out"] is True
    result = service.large_files({}, "/srv/data", 1024 * 1024, 25, 4, 30)
    assert result["items"][0]["path"] == "/srv/data/model.bin"
    assert result["partial"] is True and result["timed_out"] is True
    assert result["max_depth"] == 4 and result["timeout_seconds"] == 30


def test_conda_yaml_export_and_rebuild_plan_are_bounded():
    runs = FakeRuns([STACK_OUTPUT, "name: train\ndependencies:\n  - python=3.12\n", STACK_OUTPUT])
    service = DevelopmentService(runs, FakeConfig())
    exported = service.export_conda_environment({}, "/srv/envs/train")
    assert "python=3.12" in exported
    assert "/opt/conda/bin/conda env export -p /srv/envs/train --no-builds" in runs.commands[1]
    plan = service.conda_yaml_plan({}, {"path": "/srv/envs/rebuilt", "yaml": exported})
    assert "conda env create -p \"$target\"" in plan["script"]
    assert "name: train" not in plan["script"]
    with pytest.raises(OperationError):
        service.conda_yaml_plan({}, {"path": "/srv/envs/rebuilt", "yaml": "x" * (512 * 1024 + 1)})


def test_environment_inventory_uses_discovered_conda_absolute_path():
    runs = FakeRuns([
        STACK_OUTPUT,
        "__SM_ENV__\t/srv/projects/.venv\tPython 3.12.4\n"
        "__SM_CONDA__\t/opt/conda/envs/train\tPython 3.12.4\n"
        "__SM_CONDA_PACKAGE__\t/opt/conda/envs/train\tpython\t3.12.4\n"
        "__SM_CONDA__\t/opt/conda/envs/train\tPython 3.12.4\n"
        "__SM_CONDA_PACKAGE__\t/opt/conda/envs/train\tpython\t3.12.4\n",
    ])
    service = DevelopmentService(runs, FakeConfig())
    result = service.environment_inventory({}, "/srv/projects")
    assert [item["backend"] for item in result["items"]] == ["venv/uv", "conda"]
    assert result["items"][1]["packages"] == [{"name": "python", "version": "3.12.4"}]
    assert result["tooling"]["tools"]["conda"]["path"] == "/opt/conda/bin/conda"
    assert "conda_path=/opt/conda/bin/conda" in runs.commands[1]
    assert 'find "$base/envs" -mindepth 1 -maxdepth 1' in runs.commands[1]
    assert 'conda-meta/python-[0-9]*.json' in runs.commands[1]
    assert '"$python" --version' not in runs.commands[1]


def test_alert_acknowledge_and_soft_clear_api(client, app, admin):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    alert_id = app.extensions["alerts"].emit("test-alert", host["id"], "host_offline", "critical", "测试告警")
    acknowledged = client.post(f"/api/alerts/{alert_id}/acknowledge", json={}, headers=csrf(admin))
    assert acknowledged.status_code == 200
    assert acknowledged.get_json()["alert"]["acknowledged_at"]

    cleared = client.delete(f"/api/alerts/{alert_id}", json={}, headers=csrf(admin))
    assert cleared.status_code == 200
    assert cleared.get_json()["alert"]["cleared_at"]
    assert client.get("/api/alerts").get_json()["total"] == 0
    included = client.get("/api/alerts?include_cleared=1").get_json()
    assert included["total"] == 1
    assert app.extensions["database"].query_one("SELECT action FROM audit_logs WHERE action='alert_cleared'")


def test_alert_notification_setting_requires_permission_and_boolean(client, app, admin):
    disabled = client.patch(
        "/api/alerts/notification-setting",
        json={"enabled": False},
        headers=csrf(admin),
    )
    assert disabled.status_code == 200
    assert disabled.get_json() == {"enabled": False}
    assert app.extensions["monitor_config"].all()["toast_enabled"] is False
    assert client.get("/api/alerts").get_json()["toast_enabled"] is False
    audit_row = app.extensions["database"].query_one(
        "SELECT action,changes_json FROM audit_logs WHERE action='alert_notification_setting_updated'"
    )
    assert audit_row and '"after": false' in audit_row["changes_json"]

    invalid = client.patch(
        "/api/alerts/notification-setting",
        json={"enabled": "false"},
        headers=csrf(admin),
    )
    assert invalid.status_code == 400
    missing_csrf = client.patch("/api/alerts/notification-setting", json={"enabled": True})
    assert missing_csrf.status_code == 403

    created = client.post(
        "/api/users",
        json={"username": "alert-toggle-viewer", "password": "ViewerPass123", "role": "viewer"},
        headers=csrf(admin),
    )
    assert created.status_code == 201
    viewer_client = app.test_client()
    first = login(viewer_client, "alert-toggle-viewer", "ViewerPass123")
    changed = viewer_client.post(
        "/api/auth/change-password",
        json={"current_password": "ViewerPass123", "new_password": "ViewerPass456"},
        headers=csrf(first),
    )
    assert changed.status_code == 200
    viewer = login(viewer_client, "alert-toggle-viewer", "ViewerPass456")
    denied = viewer_client.patch(
        "/api/alerts/notification-setting",
        json={"enabled": True},
        headers=csrf(viewer),
    )
    assert denied.status_code == 403


def test_bulk_alert_actions_respect_filters_permissions_and_audit(client, app, admin):
    host_a = app.extensions["hosts"].create(host_payload(address="10.0.0.21"), fingerprint="SHA256:key-a", machine_id="machine-a")
    host_b = app.extensions["hosts"].create(host_payload(address="10.0.0.22"), fingerprint="SHA256:key-b", machine_id="machine-b")
    warning_a = app.extensions["alerts"].emit("bulk-warning-a", host_a["id"], "gpu_power_high", "warning", "A warning")
    critical_a = app.extensions["alerts"].emit("bulk-critical-a", host_a["id"], "host_offline", "critical", "A critical")
    warning_b = app.extensions["alerts"].emit("bulk-warning-b", host_b["id"], "gpu_power_high", "warning", "B warning")

    acknowledged = client.post(
        "/api/alerts/bulk-acknowledge",
        json={"filters": {"severity": "warning"}},
        headers=csrf(admin),
    )
    assert acknowledged.status_code == 200
    assert acknowledged.get_json()["count"] == 2
    rows = {row["id"]: dict(row) for row in app.extensions["database"].query_all("SELECT * FROM alerts")}
    assert rows[warning_a]["acknowledged_at"] and rows[warning_b]["acknowledged_at"]
    assert rows[critical_a]["acknowledged_at"] is None

    cleared = client.post(
        "/api/alerts/bulk-clear",
        json={"filters": {"host_id": host_a["id"], "severity": "warning"}},
        headers=csrf(admin),
    )
    assert cleared.status_code == 200
    assert cleared.get_json()["count"] == 1
    rows = {row["id"]: dict(row) for row in app.extensions["database"].query_all("SELECT * FROM alerts")}
    assert rows[warning_a]["cleared_at"]
    assert rows[warning_b]["cleared_at"] is None and rows[critical_a]["cleared_at"] is None
    assert app.extensions["database"].query_one("SELECT id FROM audit_logs WHERE action='alerts_bulk_acknowledged'")
    assert app.extensions["database"].query_one("SELECT id FROM audit_logs WHERE action='alerts_bulk_cleared'")

    created = client.post(
        "/api/users",
        json={"username": "alert-viewer", "password": "ViewerPass123", "role": "viewer"},
        headers=csrf(admin),
    )
    assert created.status_code == 201
    viewer_client = app.test_client()
    first = login(viewer_client, "alert-viewer", "ViewerPass123")
    changed = viewer_client.post(
        "/api/auth/change-password",
        json={"current_password": "ViewerPass123", "new_password": "ViewerPass456"},
        headers=csrf(first),
    )
    assert changed.status_code == 200
    viewer = login(viewer_client, "alert-viewer", "ViewerPass456")
    denied = viewer_client.post("/api/alerts/bulk-clear", json={"filters": {}}, headers=csrf(viewer))
    assert denied.status_code == 403


def test_development_and_storage_routes_enforce_elevation(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    service = app.extensions["development"]
    monkeypatch.setattr(service, "development_stack", lambda _host: {"os": {"id": "ubuntu"}})
    monkeypatch.setattr(service, "gpu_diagnostics", lambda _host: {"available": True, "gpus": []})
    monkeypatch.setattr(service, "directory_usage", lambda _host, path, timeout: {"path": path, "bytes": 123, "timeout_seconds": int(timeout)})
    monkeypatch.setattr(service, "large_files", lambda _host, path, minimum, limit, depth, timeout: {"path": path, "minimum_bytes": int(minimum), "items": [], "max_depth": int(depth), "timeout_seconds": int(timeout)})
    monkeypatch.setattr(service, "execute_environment_plan", lambda _host, payload: {"ok": True, "stdout": "done", "stderr": "", "plan": {"backend": "venv", "action": payload["action"]}})

    assert client.get(f"/api/hosts/{host['id']}/development/stack").status_code == 200
    assert client.get("/api/development/hosts").get_json()["items"][0]["allow_install"] is True
    assert client.get(f"/api/hosts/{host['id']}/development/gpu-diagnostics").status_code == 200
    assert client.get(f"/api/hosts/{host['id']}/files/usage?path=/srv/data").get_json()["bytes"] == 123
    blocked = client.post(
        f"/api/hosts/{host['id']}/development/environment-execute",
        json={"backend": "venv", "action": "create"},
        headers=csrf(admin),
    )
    assert blocked.status_code == 403 and blocked.get_json()["requires_elevation"] is True
    elevated = client.post("/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin))
    assert elevated.status_code == 200
    executed = client.post(
        f"/api/hosts/{host['id']}/development/environment-execute",
        json={"backend": "venv", "action": "create"},
        headers=csrf(admin),
    )
    assert executed.status_code == 200 and executed.get_json()["stdout"] == "done"


def test_environment_backup_plan_api_only_returns_script(client, app, admin):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:key-backup", machine_id="machine-backup")
    response = client.post(
        f"/api/hosts/{host['id']}/development/environment-backup-plan",
        json={"backend": "conda", "path": "/srv/envs/train"},
        headers=csrf(admin),
    )
    assert response.status_code == 200
    assert response.get_json()["plan"]["remote_execution"] is False
    assert "tar -C" in response.get_json()["plan"]["script"]
    assert app.extensions["database"].query_one("SELECT action FROM audit_logs WHERE action='environment_backup_plan_generated'")


def test_database_migrates_existing_alert_table(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE alerts(id INTEGER PRIMARY KEY, alert_key TEXT NOT NULL, host_id INTEGER, object_key TEXT, alert_type TEXT NOT NULL, state TEXT NOT NULL, severity TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL, last_sent_at TEXT, recovered_at TEXT)")
    connection.commit()
    connection.close()
    Database(path).initialize()
    connection = sqlite3.connect(path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(alerts)")}
        assert {"acknowledged_at", "acknowledged_by", "cleared_at"} <= columns
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        connection.close()

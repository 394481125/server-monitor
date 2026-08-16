from __future__ import annotations

from types import SimpleNamespace

import pytest

from monitor.app import _tmux_attach_command
from monitor.operations import OperationError, OperationService
from monitor.ssh_client import SSHConnectionError, SSHFingerprintError

from .conftest import csrf
from .test_hosts_history import host_payload


class FakeOperations(OperationService):
    def __init__(self, config, database):
        super().__init__(None, config, database)
        self.calls = []

    def run(self, host, command, timeout, limit=None, stdin_data=None):
        self.calls.append((command, timeout, limit, stdin_data))
        if command.startswith("ps -eo"):
            return SimpleNamespace(
                exit_code=0,
                stderr="",
                stdout=(
                    "  42   1 root S 1.0 2.0 Mon Jan 01 00:00:00 2024 /usr/bin/python worker.py\n"
                    "   2   0 root S 0.0 0.0 Mon Jan 01 00:00:00 2024 [kthreadd]\n"
                    "\n__SERVER_MONITOR_CWD__\n"
                    "42\t/srv/worker jobs\n"
                    "2\t\n"
                ),
            )
        if command.startswith("ps -p"):
            return SimpleNamespace(exit_code=0, stderr="", stdout="Mon Jan 01 00:00:00 2024\n")
        if command.startswith(". /etc/os-release"):
            return SimpleNamespace(exit_code=0, stderr="", stdout="ubuntu")
        if command.startswith("sudo"):
            return SimpleNamespace(exit_code=0, stderr="", stdout="")
        if "apt-get install" in command:
            if "sudo -n" in command and getattr(self, "sudo_requires_password", False):
                return SimpleNamespace(exit_code=1, stderr="sudo: a password is required", stdout="")
            return SimpleNamespace(exit_code=0, stderr="", stdout="")
        if "command -v" in command:
            return SimpleNamespace(exit_code=0, stderr="", stdout="tmux:available\n")
        if "stress-ng" in command:
            return SimpleNamespace(exit_code=0, stderr="", stdout="1234\n")
        return SimpleNamespace(exit_code=0, stderr="", stdout="")


def test_tmux_attach_enables_available_utf8_locale_and_quotes_session_name():
    command = _tmux_attach_command("session name's")

    assert "locale -a" in command
    assert "export LANG=C.UTF-8 LC_ALL=C.UTF-8" in command
    assert "tmux attach-session -t 'session name'\"'\"'s'" in command
    assert command.endswith("\n")


def test_process_parser_and_pid_reuse_guard(app):
    config = app.extensions["monitor_config"]
    database = app.extensions["database"]
    service = FakeOperations(config, database)
    rows = service.processes({})
    assert rows[0]["pid"] == 42
    assert rows[0]["started"] == "Mon Jan 01 00:00:00 2024"
    assert rows[0]["cwd"] == "/srv/worker jobs"
    assert len(rows) == 1
    assert "/proc/[0-9]*" in service.calls[0][0]
    visible_rows = service.processes({}, hide_kernel=False)
    assert len(visible_rows) == 2
    assert visible_rows[1]["cwd"] is None
    with pytest.raises(OperationError):
        service.terminate_process({}, 42, "Tue Jan 02 00:00:00 2024")


def test_process_api_returns_working_directory(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(
        host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one"
    )
    monkeypatch.setattr(
        app.extensions["operations"],
        "processes",
        lambda *_args: [{"pid": 42, "cwd": "/srv/worker"}],
    )

    response = client.get(f"/api/hosts/{host['id']}/processes")

    assert response.status_code == 200
    assert response.get_json()["items"][0]["cwd"] == "/srv/worker"


def test_tool_install_requires_verification(app):
    service = FakeOperations(app.extensions["monitor_config"], app.extensions["database"])
    assert service.installation_command({}, "tmux") == "LC_ALL=C sudo -n apt-get install -y tmux"
    assert service.install_tool({}, "tmux") == "LC_ALL=C sudo -n apt-get install -y tmux"


def test_tool_install_uses_saved_sudo_password_only_after_noninteractive_failure(app):
    service = FakeOperations(app.extensions["monitor_config"], app.extensions["database"])
    service.secrets = app.extensions["secret_box"]
    service.sudo_requires_password = True
    host = {"sudo_password": service.secrets.encrypt("remote-sudo-password")}

    assert service.install_tool(host, "tmux") == "LC_ALL=C sudo -n apt-get install -y tmux"

    password_call = next(call for call in service.calls if "sudo -S -p ''" in call[0])
    assert password_call[3] == "remote-sudo-password\n"
    assert "remote-sudo-password" not in password_call[0]


def test_tool_install_explains_missing_remote_sudo_password(app):
    service = FakeOperations(app.extensions["monitor_config"], app.extensions["database"])
    service.sudo_requires_password = True

    with pytest.raises(OperationError, match="远端 sudo 需要密码"):
        service.install_tool({}, "tmux")


def test_stress_parameters_and_task_record(app):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    service = FakeOperations(app.extensions["monitor_config"], app.extensions["database"])
    task = service.start_stress(host, 1, 1, 50, 1)
    row = app.extensions["database"].query_one("SELECT * FROM stress_jobs WHERE id=?", (task,))
    assert row["state"] == "running" and row["duration_seconds"] == 60
    with pytest.raises(OperationError):
        service.start_stress(host, 0, 0, 50, 1)
    with pytest.raises(OperationError):
        service.start_stress(host, 1, 1, 81, 1)


def test_stress_command_omits_disabled_worker_types(app):
    hosts = app.extensions["hosts"]
    host = hosts.create(host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one")
    service = FakeOperations(app.extensions["monitor_config"], app.extensions["database"])
    service.start_stress(host, 1, 0, 0, 1)
    cpu_only = service.calls[-1][0]
    assert "--cpu 1" in cpu_only and "--vm" not in cpu_only

    service.start_stress(host, 0, 2, 50, 1)
    memory_only = service.calls[-1][0]
    assert "--cpu" not in memory_only
    assert "--vm 2 --vm-bytes 25%" in memory_only


def test_api_dashboard_and_settings_write(client, admin):
    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    settings = dashboard.get_json()["settings"]
    assert settings["gpu_util_threshold"] == 10
    assert settings["gpu_memory_threshold"] == 10
    response = client.patch("/api/settings", json={"collection_interval": 11, "serverchan_events": ["host_offline"]}, headers=csrf(admin))
    assert response.status_code == 200, response.get_json()
    settings = client.get("/api/settings").get_json()["settings"]
    assert settings["collection_interval"] == 11
    assert settings["serverchan_events"] == ["host_offline"]
    assert settings["metric_raw_retention_minutes"] == 15
    assert settings["metric_mid_retention_hours"] == 6
    assert settings["collection_task_retention_minutes"] == 60
    assert settings["database_total_bytes"] >= settings["database_size_bytes"]
    scan_settings = client.get("/api/scan-settings")
    assert scan_settings.status_code == 200
    assert scan_settings.get_json()["settings"]["scan_timeout_seconds"] == 60
    platform = client.get("/api/platform-status")
    assert platform.status_code == 200
    assert platform.get_json()["uptime_seconds"] >= 0


def test_api_host_create_retests_identity_server_side(client, admin, monkeypatch):
    class FakeSSHClient:
        def __init__(self, *_args):
            pass

        def connect(self):
            return "SHA256:server-observed"

        def run(self, *_args):
            return SimpleNamespace(exit_code=0, stderr="", stdout="remote-node\nmachine-from-server\n/usr/bin/tmux\n")

        def close(self):
            pass

    monkeypatch.setattr("monitor.app.SSHClient", FakeSSHClient)
    response = client.post(
        "/api/hosts",
        json={**host_payload(), "identity": {"fingerprint": "SHA256:client-forged", "machine_id": "forged"}},
        headers=csrf(admin),
    )
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["host"]["fingerprint"] == "SHA256:server-observed"


def test_api_returns_actionable_error_when_ssh_is_unreachable(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(
        host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one"
    )

    def fail(_host):
        raise SSHConnectionError("SSH 连接失败: 目标端口拒绝连接")

    monkeypatch.setattr(app.extensions["operations"], "tmux_sessions", fail)
    response = client.get(f"/api/hosts/{host['id']}/tmux")
    assert response.status_code == 400
    assert response.get_json()["error"] == "SSH 连接失败: 目标端口拒绝连接"


def test_host_capability_flags_are_enforced_by_rest_routes(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(
        {
            **host_payload(),
            "allow_tmux": False,
            "allow_process": False,
            "allow_install": False,
            "allow_stress": False,
        },
        fingerprint="SHA256:key-one",
        machine_id="machine-one",
    )
    monkeypatch.setattr(
        app.extensions["operations"],
        "tmux_sessions",
        lambda *_args: pytest.fail("能力关闭时不应执行远端 Tmux 命令"),
    )
    assert client.get(f"/api/hosts/{host['id']}/tmux").status_code == 400
    assert client.get(f"/api/hosts/{host['id']}/processes").status_code == 400
    assert client.get(f"/api/hosts/{host['id']}/tools/tmux/install-plan").status_code == 400

    elevated = client.post(
        "/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin)
    )
    assert elevated.status_code == 200
    stress = client.post(
        f"/api/hosts/{host['id']}/stress",
        json={"cpu_workers": 1, "memory_workers": 0, "memory_percent": 0, "duration_minutes": 1},
        headers=csrf(admin),
    )
    assert stress.status_code == 400


def test_host_update_retests_identity_server_side(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(
        host_payload(), fingerprint="SHA256:key-one", machine_id="machine-one"
    )

    class FakeSSHClient:
        def __init__(self, *_args):
            pass

        def connect(self):
            return "SHA256:server-retested"

        def run(self, *_args):
            return SimpleNamespace(
                exit_code=0,
                stderr="",
                stdout="remote-node\nmachine-after-update\n/usr/bin/tmux\n",
            )

        def close(self):
            pass

    monkeypatch.setattr("monitor.app.SSHClient", FakeSSHClient)
    blocked = client.patch(
        f"/api/hosts/{host['id']}",
        json={"address": "10.0.0.2"},
        headers=csrf(admin),
    )
    assert blocked.status_code == 403
    elevated = client.post(
        "/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin)
    )
    assert elevated.status_code == 200
    response = client.patch(
        f"/api/hosts/{host['id']}",
        json={
            "address": "10.0.0.2",
            "identity": {"fingerprint": "SHA256:client-forged", "machine_id": "forged"},
            "confirmed_physical_replacement": True,
        },
        headers=csrf(admin),
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["host"]["fingerprint"] == "SHA256:server-retested"
    assert response.get_json()["host"]["machine_id"] == "machine-after-update"


def test_host_fingerprint_change_requires_explicit_confirmation(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(
        host_payload(), fingerprint="SHA256:old", machine_id="machine-old"
    )
    observed = "SHA256:new"

    class FakeSSHClient:
        def __init__(self, candidate, *_args):
            self.candidate = candidate

        def connect(self):
            if self.candidate["fingerprint"] != observed:
                raise SSHFingerprintError("SSH 主机指纹与已记录值不一致", expected=self.candidate["fingerprint"], observed=observed)
            return observed

        def run(self, *_args):
            return SimpleNamespace(exit_code=0, stderr="", stdout="replacement-node\nmachine-new\n")

        def close(self):
            pass

    monkeypatch.setattr("monitor.app.SSHClient", FakeSSHClient)
    elevated = client.post(
        "/api/auth/elevate", json={"password": "TemporaryPass456"}, headers=csrf(admin)
    )
    assert elevated.status_code == 200

    mismatch = client.post(
        f"/api/hosts/{host['id']}/test", json={"auth_secret": "replacement-password"}, headers=csrf(admin)
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["fingerprint_mismatch"] is True
    assert mismatch.get_json()["expected"] == "SHA256:old"
    assert mismatch.get_json()["observed"] == observed

    physical_identity_changed = client.patch(
        f"/api/hosts/{host['id']}",
        json={"auth_secret": "replacement-password", "confirmed_fingerprint": observed},
        headers=csrf(admin),
    )
    assert physical_identity_changed.status_code == 409
    assert physical_identity_changed.get_json()["physical_identity_changed"] is True

    confirmed = client.patch(
        f"/api/hosts/{host['id']}",
        json={"auth_secret": "replacement-password", "confirmed_fingerprint": observed, "confirmed_physical_replacement": True},
        headers=csrf(admin),
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    assert confirmed.get_json()["host"]["fingerprint"] == observed


def test_disabled_host_manual_refresh_is_rejected(client, app, admin):
    host = app.extensions["hosts"].create(
        {**host_payload(), "enabled": False},
        fingerprint="SHA256:key-one",
        machine_id="machine-one",
    )
    response = client.post(
        f"/api/hosts/{host['id']}/refresh", json={}, headers=csrf(admin)
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "该主机已禁用采集"


def test_frontend_console_contract_and_csp(client):
    page = client.get("/")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'class="global-header"' in html
    assert 'id="mobile-menu-button"' in html
    assert html.index("/static/vendor/xterm/xterm.js") < html.index("/static/app.js")
    assert html.index("/static/vendor/xterm/addon-fit.js") < html.index("/static/app.js")
    assert 'name="csp-style-nonce"' in html
    assert 'style=' not in html
    assert "style-src 'self'" in page.headers["Content-Security-Policy"]
    assert "style-src-elem 'self' 'nonce-" in page.headers["Content-Security-Policy"]
    assert "style-src-attr 'unsafe-inline'" in page.headers["Content-Security-Policy"]

    script = client.get("/static/app.js").get_data(as_text=True)
    assert "function ensureElevated" in script
    assert "function showGpuConfig" in script
    assert "function confirmFingerprintChange" in script
    assert "function gpuCardSummary" in script
    assert "function physicalDisks" in script
    assert "使用中" in script and "storage-list" in script
    assert "ResizeObserver" in script
    assert "new window.Terminal" in script
    assert "terminal.onData(send)" in script
    assert "terminal.write(event.data)" in script
    assert "terminal.textContent += event.data" not in script
    assert "documentOverride: terminalDocument" in script
    assert "data-sudo-password" in script
    assert "data-key-passphrase" in script
    assert "搜索 PID、用户、目录或命令" in script
    assert "<th>工作目录</th>" in script
    assert "setInterval(() => { if (!document.hidden) renderDashboard(true); }, state.refreshMs)" in script
    assert client.get("/static/vendor/xterm/xterm.js").status_code == 200
    assert client.get("/static/vendor/xterm/addon-fit.js").status_code == 200
    assert client.get("/static/vendor/xterm/xterm.css").status_code == 200
    assert client.get("/favicon.ico").status_code == 204

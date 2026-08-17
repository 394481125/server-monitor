from __future__ import annotations

import json
import getpass
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor.collector import Collector
from monitor.config import ConfigStore
from monitor.db import Database
from monitor.operations import OperationService
from monitor.security import SecretBox
from monitor.services import BackupService, HostService
from monitor.ssh_client import SSHClient, SSHFingerprintError


def main() -> None:
    key_path = Path(os.environ.get("SERVER_MONITOR_ACCEPTANCE_KEY", "/tmp/server-monitor-sshd/client_key"))
    address = os.environ.get("SERVER_MONITOR_ACCEPTANCE_HOST", "127.0.0.1")
    port = int(os.environ.get("SERVER_MONITOR_ACCEPTANCE_PORT", "22222"))
    username = os.environ.get("SERVER_MONITOR_ACCEPTANCE_USER", getpass.getuser())
    client_key = key_path.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="server-monitor-acceptance-", dir="/tmp") as temporary:
        temp = Path(temporary)
        database = Database(temp / "monitor.sqlite3")
        database.initialize()
        secret_box = SecretBox(temp / "master.key")
        config = ConfigStore(database)
        hosts = HostService(database, secret_box, config)
        candidate = {
            "address": address,
            "port": port,
            "username": username,
            "auth_type": "key",
            "private_key": secret_box.encrypt(client_key),
            "private_key_passphrase": None,
            "fingerprint": None,
        }
        client = SSHClient(candidate, secret_box, config.all())
        fingerprint = client.connect()
        identity = client.run("hostname; cat /etc/machine-id", 10)
        stdin_probe = client.run("IFS= read -r value; [ \"$value\" = stdin-ok ]", 10, stdin_data="stdin-ok\n")
        sftp = client.open_sftp()
        sftp_root = f"/tmp/server-monitor-sftp-{uuid.uuid4().hex[:8]}"
        sftp.mkdir(sftp_root)
        try:
            with sftp.open(f"{sftp_root}/probe.txt", "wb") as remote:
                remote.write(b"sftp-ok")
            with sftp.open(f"{sftp_root}/probe.txt", "rb") as remote:
                sftp_payload = remote.read().decode("utf-8")
            sftp_entries = {item.filename for item in sftp.listdir_attr(sftp_root)}
        finally:
            sftp.remove(f"{sftp_root}/probe.txt")
            sftp.rmdir(sftp_root)
            sftp.close()
        client.close()
        assert identity.exit_code == 0
        assert stdin_probe.exit_code == 0
        assert sftp_payload == "sftp-ok"
        assert "probe.txt" in sftp_entries
        identity_lines = identity.stdout.splitlines()
        machine_id = identity_lines[1].strip()

        host = hosts.create(
            {
                "name": "本机真实 SSH 验收",
                "address": address,
                "port": port,
                "username": username,
                "auth_type": "key",
                "private_key": client_key,
                "allow_tmux": True,
                "allow_process": True,
            },
            fingerprint=fingerprint,
            machine_id=machine_id,
        )
        secured_host = hosts.get(host["id"], include_secrets=True)
        collector = Collector(secret_box, config.all())
        first = collector.collect(secured_host)
        assert first.core_ok, first.error
        hosts.ingest_collection(host["id"], first)
        time.sleep(0.3)
        second = collector.collect(secured_host, first.data)
        assert second.core_ok, second.error
        outcome = hosts.ingest_collection(host["id"], second)
        assert outcome["status"] in {"online", "degraded"}
        assert second.data["cpu"]["logical_cores"] > 0
        assert second.data["cpu"]["usage_percent"] is not None
        assert second.data["memory"]["total"] > 0
        assert second.data["filesystems"]
        assert second.data["network"]

        mismatch = dict(secured_host)
        mismatch["fingerprint"] = "SHA256:wrong"
        try:
            SSHClient(mismatch, secret_box, config.all()).connect()
        except SSHFingerprintError:
            fingerprint_rejected = True
        else:
            fingerprint_rejected = False
        assert fingerprint_rejected

        operations = OperationService(secret_box, config, database)
        tools = operations.detect_tools(secured_host)
        assert tools["tmux"] == "available"
        assert set(tools.values()) <= {"available", "missing"}
        processes = operations.processes(secured_host)
        assert any(process["pid"] > 1 for process in processes)
        assert any(process["cwd"] for process in processes if process["pid"] > 1)

        session = f"server-monitor-accept-{uuid.uuid4().hex[:8]}"
        operations.tmux_create(secured_host, session)
        try:
            sessions = operations.tmux_sessions(secured_host)
            assert session in {item["name"] for item in sessions}
            snapshot = operations.tmux_snapshot(secured_host, session)
            assert isinstance(snapshot, str)
        finally:
            operations.tmux_kill(secured_host, session)

        dispatch = operations.dispatch_gpu(
            secured_host,
            {"command": "printf direct-ok", "cwd": None, "shell": "/bin/bash", "env": {}, "mode": "direct"},
            {"uuid": "GPU-acceptance"},
            str(uuid.uuid4()),
        )
        assert dispatch.success and dispatch.stdout == "direct-ok", dispatch

        probe = secret_box.encrypt("restore-probe-secret")
        backup_service = BackupService(database, temp)
        backup = backup_service.create(temp / "backups", 2)
        restored = backup_service.verify_restore(backup, temp / "restore", secret_box, probe)
        assert restored.exists()

        report = {
            "fingerprint": fingerprint,
            "fingerprint_mismatch_rejected": fingerprint_rejected,
            "ssh_stdin": "passed",
            "sftp": "passed",
            "machine_id_detected": bool(machine_id),
            "host_status": outcome["status"],
            "logical_cpus": second.data["cpu"]["logical_cores"],
            "cpu_usage_percent": second.data["cpu"]["usage_percent"],
            "memory_bytes": second.data["memory"]["total"],
            "filesystem_count": len(second.data["filesystems"]),
            "network_count": len(second.data["network"]),
            "gpu_count": len(second.data["gpus"]),
            "tools": tools,
            "process_count": len(processes),
            "tmux_lifecycle": "passed",
            "direct_shell": "passed",
            "backup_restore_with_key": "passed",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

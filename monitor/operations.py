from __future__ import annotations

import shlex
import uuid
import re
from typing import Any

from .gpu_scheduler import DispatchResult
from .security import redact
from .ssh_client import SSHClient, SSHError, SSHTimeout
from .utils import command_summary, utc_iso


class OperationError(ValueError):
    pass


_REMOTE_PATH_LIMIT = 512
_PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+~<>=!,-]{0,199}$")
_APT_PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,119}$")
_PYTHON_SELECTOR = re.compile(r"^(?:python3|(?:python)?3\.(8|9|10|11|12|13))$")
_PYTORCH_PRESETS: dict[str, tuple[str, ...]] = {
    "none": (),
    "cpu": ("torch", "torchvision", "torchaudio", "--index-url=https://download.pytorch.org/whl/cpu"),
    "cu118": ("torch", "torchvision", "torchaudio", "--index-url=https://download.pytorch.org/whl/cu118"),
    "cu121": ("torch", "torchvision", "torchaudio", "--index-url=https://download.pytorch.org/whl/cu121"),
    "cu124": ("torch", "torchvision", "torchaudio", "--index-url=https://download.pytorch.org/whl/cu124"),
}


def _remote_path(value: Any, label: str = "路径") -> str:
    path = str(value or "").strip()
    if not path or len(path) > _REMOTE_PATH_LIMIT or "\x00" in path or any(char in path for char in "\r\n"):
        raise OperationError(f"{label}无效")
    if not path.startswith("/"):
        raise OperationError(f"{label}必须是绝对路径")
    normalized = "/" + "/".join(part for part in path.split("/") if part and part != ".")
    if "/../" in f"{normalized}/" or normalized in {"/", "/.."}:
        raise OperationError(f"{label}不能包含上级目录")
    return normalized


def _package_specs(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else str(value or "").replace(",", " ").split()
    if not isinstance(raw, list) or len(raw) > 30:
        raise OperationError("依赖包数量无效")
    packages = [str(item).strip() for item in raw if str(item).strip()]
    if any(not _PACKAGE_PATTERN.fullmatch(item) or item.startswith("-") for item in packages):
        raise OperationError("依赖包名称或版本约束无效")
    return packages


def _parse_development_stack(output: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "os": {"id": "", "version": ""},
        "tools": {},
        "python_versions": [],
        "cuda": {"nvcc_version": None, "cudnn_packages": [], "cudnn_libraries": []},
        "gpu": {"driver_version": None, "recommended_driver": None, "recommendation_note": None},
        "warnings": [],
    }
    for line in output.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        if parts[0] == "__SM_OS__" and len(parts) >= 3:
            result["os"] = {"id": parts[1], "version": parts[2]}
        elif parts[0] == "__SM_TOOL__" and len(parts) >= 4:
            name, path, version = parts[1], parts[2], parts[3]
            result["tools"][name] = {"available": bool(path), "path": path or None, "version": version or None}
            if name.startswith("python3") and path:
                result["python_versions"].append({"command": name, "path": path, "version": version or name})
        elif parts[0] == "__SM_NVCC__" and len(parts) >= 2:
            result["cuda"]["nvcc_version"] = parts[1] or None
        elif parts[0] == "__SM_CUDNN__" and len(parts) >= 3:
            result["cuda"]["cudnn_packages"].append({"package": parts[1], "version": parts[2]})
        elif parts[0] == "__SM_CUDNN_LIBRARY__" and len(parts) >= 3:
            result["cuda"]["cudnn_libraries"].append({"name": parts[1], "path": parts[2]})
        elif parts[0] == "__SM_DRIVER__" and len(parts) >= 2:
            result["gpu"]["driver_version"] = parts[1] or None
        elif parts[0] == "__SM_RECOMMENDED_DRIVER__" and len(parts) >= 2:
            result["gpu"]["recommended_driver"] = parts[1] or None
        elif parts[0] == "__SM_RECOMMENDED_DRIVER_NOTE__" and len(parts) >= 2:
            result["gpu"]["recommendation_note"] = parts[1] or None
        elif parts[0] == "__SM_WARNING__" and len(parts) >= 2:
            result["warnings"].append(parts[1])
    return result


def _parse_gpu_diagnostics(output: str) -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "gpus": [], "ecc": "", "topology": "", "nvlink": "", "notes": []}
    sections: dict[str, list[str]] = {"ecc": [], "topology": [], "nvlink": []}
    section: str | None = None
    for line in output.splitlines():
        if line == "__SM_ECC_BEGIN__":
            section = "ecc"
            continue
        if line == "__SM_TOPOLOGY_BEGIN__":
            section = "topology"
            continue
        if line == "__SM_NVLINK_BEGIN__":
            section = "nvlink"
            continue
        if line == "__SM_SECTION_END__":
            section = None
            continue
        if section:
            sections[section].append(line)
            continue
        parts = line.split("\t")
        if parts[0] == "__SM_GPU_AVAILABLE__" and len(parts) >= 2:
            result["available"] = parts[1] == "1"
        elif parts[0] == "__SM_GPU__" and len(parts) >= 9:
            values = parts[1:]
            try:
                total, used, util = (float(values[5]), float(values[6]), float(values[7]))
            except (TypeError, ValueError):
                continue
            result["gpus"].append({
                "index": values[0], "uuid": values[1], "name": values[2], "driver_version": values[3],
                "temperature_c": values[4], "memory_total_mib": total, "memory_used_mib": used,
                "utilization_percent": util,
            })
        elif parts[0] == "__SM_DIAGNOSTIC_WARNING__" and len(parts) >= 2:
            result["notes"].append(parts[1])
    result["ecc"] = "\n".join(sections["ecc"]).strip()
    result["topology"] = "\n".join(sections["topology"]).strip()
    result["nvlink"] = "\n".join(sections["nvlink"]).strip()
    if not result["available"]:
        result["notes"].append("目标主机未检测到 nvidia-smi，无法读取 NVIDIA GPU 健康状态。")
    if result["available"] and not result["nvlink"]:
        result["notes"].append("该驱动或 GPU 未提供 NVLink 状态，属于正常能力缺失。")
    for gpu in result["gpus"]:
        memory_ratio = (gpu["memory_used_mib"] / gpu["memory_total_mib"] * 100) if gpu["memory_total_mib"] else 0
        gpu["high_util_low_memory"] = gpu["utilization_percent"] >= 80 and memory_ratio < 10
        if gpu["high_util_low_memory"]:
            result["notes"].append(f"GPU {gpu['index']} 利用率高但显存占用低，可能是数据加载或同步等待瓶颈，需结合训练日志确认。")
    result["fragmentation"] = {
        "available": False,
        "reason": "nvidia-smi 不提供进程内分配器碎片数据；需要在 PyTorch 进程内采集 allocator 统计，平台不会伪造该指标。",
    }
    return result


class OperationService:
    def __init__(self, secrets: Any, config: Any, database: Any):
        self.secrets = secrets
        self.config = config
        self.database = database

    def _client(self, host: dict[str, Any]) -> SSHClient:
        return SSHClient(host, self.secrets, self.config.all())

    def run(self, host: dict[str, Any], command: str, timeout: int, limit: int | None = None, stdin_data: str | None = None) -> Any:
        client = self._client(host)
        try:
            return client.run(command, timeout, limit, stdin_data=stdin_data)
        finally:
            client.close()

    def dispatch_gpu(self, host: dict[str, Any], effective: dict[str, Any], gpu: dict[str, Any], task_id: str) -> DispatchResult:
        command = effective["command"]
        environment = {"CUDA_VISIBLE_DEVICES": gpu["uuid"], "SERVER_MONITOR_TASK_ID": task_id, **effective.get("env", {})}
        exports = "; ".join(f"export {key}={shlex.quote(value)}" for key, value in environment.items())
        cwd = f"cd {shlex.quote(effective['cwd'])} && " if effective.get("cwd") else ""
        shell = shlex.quote(effective["shell"])
        marker = f"server-monitor-{task_id}"
        script = f"{exports}; {cwd}setsid -w {shell} -lc {shlex.quote(command)} {shlex.quote(marker)}"
        mode = effective["mode"]
        settings = self.config.all()
        client = self._client(host)
        try:
            if mode == "tmux":
                session = f"sm-{host['physical_id'][:8]}-{gpu['uuid'][-8:]}-{task_id[:8]}"
                wrapper = f"{script}; task_rc=$?; sleep 2; exit $task_rc"
                tmux_cmd = f"tmux new-session -d -s {shlex.quote(session)} {shlex.quote(wrapper)} && tmux has-session -t {shlex.quote(session)}"
                result = client.run(tmux_cmd, settings["gpu_submit_timeout"], settings["schedule_output_limit"])
                if result.exit_code == 0:
                    return DispatchResult(True, exit_code=0, stdout=result.stdout, stderr=result.stderr, stdout_truncated=result.stdout_truncated, stderr_truncated=result.stderr_truncated)
                return DispatchResult(False, unknown_execution=True, exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr, error="Tmux 会话创建或确认失败", stdout_truncated=result.stdout_truncated, stderr_truncated=result.stderr_truncated)
            result = client.run(script, settings["gpu_direct_timeout"], settings["schedule_output_limit"])
            if result.exit_code == 0:
                return DispatchResult(True, exit_code=0, stdout=result.stdout, stderr=result.stderr, stdout_truncated=result.stdout_truncated, stderr_truncated=result.stderr_truncated)
            return DispatchResult(False, unknown_execution=True, exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr, error=f"直接 Shell 退出码 {result.exit_code}", stdout_truncated=result.stdout_truncated, stderr_truncated=result.stderr_truncated)
        except SSHTimeout:
            # Best effort cleanup only for the uniquely tagged direct task.
            if mode == "direct":
                try:
                    marker = shlex.quote("SERVER_MONITOR_TASK_ID=" + task_id)
                    cleanup = (
                        "for proc in /proc/[0-9]*; do "
                        f"tr '\\0' '\\n' <\"$proc/environ\" 2>/dev/null | grep -Fxq {marker} || continue; "
                        "pid=${proc##*/}; pgid=$(ps -o pgid= -p \"$pid\" 2>/dev/null | tr -d ' '); "
                        "[ -n \"$pgid\" ] && kill -TERM -- \"-$pgid\" 2>/dev/null; done; true"
                    )
                    client.run(cleanup, 10)
                except SSHError:
                    pass
            return DispatchResult(False, unknown_execution=True, error="远端执行超时")
        except SSHError as exc:
            return DispatchResult(False, confirmed_not_started=exc.remote_started is False, unknown_execution=exc.remote_started is None, error=str(exc))
        finally:
            client.close()

    def tmux_sessions(self, host: dict[str, Any]) -> list[dict[str, str]]:
        result = self.run(host, "tmux list-sessions -F '#{session_name}|#{session_windows}|#{session_created}'", self.config.all()["collection_timeout"])
        if result.exit_code != 0:
            if "not found" in result.stderr.lower():
                raise OperationError("Tmux 未安装")
            return []
        return [{"name": parts[0], "windows": parts[1], "created": parts[2]} for line in result.stdout.splitlines() if len((parts := line.split("|", 2))) == 3]

    def tmux_snapshot(self, host: dict[str, Any], session: str) -> str:
        result = self.run(host, f"tmux capture-pane -p -t {shlex.quote(session)}", self.config.all()["collection_timeout"])
        if result.exit_code != 0:
            raise OperationError(redact(result.stderr) or "读取 Tmux 快照失败")
        return result.stdout

    def tmux_create(self, host: dict[str, Any], name: str) -> None:
        if not name or len(name) > 100:
            raise OperationError("Tmux 会话名称无效")
        result = self.run(host, f"tmux new-session -d -s {shlex.quote(name)}", self.config.all()["collection_timeout"])
        if result.exit_code != 0:
            raise OperationError(redact(result.stderr) or "创建 Tmux 会话失败")

    def tmux_rename(self, host: dict[str, Any], old: str, new: str) -> None:
        result = self.run(host, f"tmux rename-session -t {shlex.quote(old)} {shlex.quote(new)}", self.config.all()["collection_timeout"])
        if result.exit_code != 0:
            raise OperationError(redact(result.stderr) or "重命名 Tmux 会话失败")

    def tmux_kill(self, host: dict[str, Any], name: str) -> None:
        result = self.run(host, f"tmux kill-session -t {shlex.quote(name)}", self.config.all()["collection_timeout"])
        if result.exit_code != 0:
            raise OperationError(redact(result.stderr) or "删除 Tmux 会话失败")

    def processes(self, host: dict[str, Any], hide_kernel: bool = True) -> list[dict[str, Any]]:
        marker = "__SERVER_MONITOR_CWD__"
        command = (
            "ps -eo pid=,ppid=,user=,stat=,%cpu=,%mem=,lstart=,args= --sort=-%cpu || exit $?; "
            f"printf '\\n{marker}\\n'; "
            "for proc in /proc/[0-9]*; do "
            "pid=${proc##*/}; cwd=$(readlink \"$proc/cwd\" 2>/dev/null) || cwd=; "
            "printf '%s\\t%s\\n' \"$pid\" \"$cwd\"; "
            "done"
        )
        result = self.run(host, command, self.config.all()["collection_timeout"])
        if result.exit_code != 0:
            raise OperationError(redact(result.stderr) or "读取进程失败")
        process_output, separator, cwd_output = result.stdout.partition(f"\n{marker}\n")
        cwd_by_pid: dict[int, str] = {}
        if separator:
            for line in cwd_output.splitlines():
                pid_text, delimiter, cwd = line.partition("\t")
                if delimiter and pid_text.isdigit():
                    cwd_by_pid[int(pid_text)] = cwd
        rows: list[dict[str, Any]] = []
        for line in process_output.splitlines():
            parts = line.strip().split(maxsplit=11)
            if len(parts) < 12:
                continue
            raw_command = parts[11]
            if hide_kernel and raw_command.startswith("["):
                continue
            pid = int(parts[0])
            rows.append({"pid": pid, "ppid": int(parts[1]), "user": parts[2], "state": parts[3], "cpu": float(parts[4]), "memory": float(parts[5]), "started": " ".join(parts[6:11]), "cwd": cwd_by_pid.get(pid) or None, "command": command_summary(raw_command)})
        return rows

    def terminate_process(self, host: dict[str, Any], pid: int, started: str, signal: str = "TERM") -> None:
        if signal not in {"TERM", "KILL"}:
            raise OperationError("只支持 SIGTERM 或 SIGKILL")
        if pid <= 1:
            raise OperationError("不允许终止系统核心进程")
        check = self.run(host, f"ps -p {int(pid)} -o lstart=,args=", self.config.all()["collection_timeout"])
        if check.exit_code != 0 or not check.stdout.strip():
            raise OperationError("进程不存在")
        actual = " ".join(check.stdout.split()[:5])
        if actual != " ".join(started.split()[:5]):
            raise OperationError("PID 已被复用，拒绝终止")
        result = self.run(host, f"kill -{signal} {int(pid)}", self.config.all()["collection_timeout"])
        if result.exit_code != 0:
            raise OperationError(redact(result.stderr) or "终止进程失败")

    def detect_tools(self, host: dict[str, Any]) -> dict[str, str]:
        command = "for x in tmux smartctl sensors stress-ng nvidia-smi docker; do command -v $x >/dev/null 2>&1 && echo $x:available || echo $x:missing; done"
        result = self.run(host, command, self.config.all()["collection_timeout"])
        return dict(line.split(":", 1) for line in result.stdout.splitlines() if ":" in line)

    def installation_command(self, host: dict[str, Any], tool: str) -> str:
        packages = {
            "tmux": {"deb": "tmux", "rpm": "tmux"},
            "smartctl": {"deb": "smartmontools", "rpm": "smartmontools"},
            "sensors": {"deb": "lm-sensors", "rpm": "lm_sensors"},
            "stress-ng": {"deb": "stress-ng", "rpm": "stress-ng"},
            "docker": {"deb": "docker.io"},
        }
        if tool not in packages:
            raise OperationError("不支持自动安装该工具")
        result = self.run(host, ". /etc/os-release 2>/dev/null; printf '%s' \"${ID:-}\"", self.config.all()["collection_timeout"])
        os_id = result.stdout.strip().lower()
        if os_id in {"ubuntu", "debian"}:
            return f"LC_ALL=C sudo -n apt-get install -y {shlex.quote(packages[tool]['deb'])}"
        if os_id in {"rocky", "almalinux", "rhel"}:
            package = packages[tool].get("rpm")
            if not package:
                raise OperationError("该发行版的 Docker 软件源不统一，请按 Docker 官方文档手动安装")
            return f"LC_ALL=C sudo -n dnf install -y {shlex.quote(package)}"
        raise OperationError("该发行版不支持一键安装，请手动安装")

    def install_tool(self, host: dict[str, Any], tool: str) -> str:
        command = self.installation_command(host, tool)
        try:
            result = self.run(host, command, self.config.all()["install_timeout"], self.config.all()["schedule_output_limit"])
            if result.exit_code != 0 and self._sudo_needs_password(result.stderr):
                encrypted_password = host.get("sudo_password")
                if not encrypted_password:
                    raise OperationError("远端 sudo 需要密码。请编辑主机并保存“远端 sudo 密码”，或为该精确安装命令配置 NOPASSWD 权限")
                password = self.secrets.decrypt(encrypted_password) if self.secrets else None
                if not password:
                    raise OperationError("已保存的远端 sudo 密码无法读取，请在主机编辑页重新保存")
                password_command = command.replace("sudo -n", "sudo -S -p ''", 1)
                result = self.run(
                    host,
                    password_command,
                    self.config.all()["install_timeout"],
                    self.config.all()["schedule_output_limit"],
                    stdin_data=password + "\n",
                )
        except SSHTimeout as exc:
            status = self.run(host, "pgrep -af '(apt-get|apt|dpkg|dnf|rpm)' || true", self.config.all()["collection_timeout"])
            suffix = "，远端包管理进程仍在运行" if status.stdout.strip() else "，远端执行状态未知"
            raise OperationError("工具安装等待超时" + suffix) from exc
        if result.exit_code != 0:
            if self._sudo_auth_failed(result.stderr):
                raise OperationError("已保存的远端 sudo 密码不正确，或该账号无权执行此安装命令")
            raise OperationError(redact(result.stderr) or "工具安装失败")
        status = self.detect_tools(host).get(tool)
        if status != "available":
            raise OperationError("安装命令已结束，但工具仍不可用")
        return command

    @staticmethod
    def _sudo_needs_password(message: str) -> bool:
        value = message.lower()
        return any(marker in value for marker in ("password is required", "a password is required", "no tty present", "需要密码"))

    @staticmethod
    def _sudo_auth_failed(message: str) -> bool:
        value = message.lower()
        return any(marker in value for marker in ("incorrect password", "sorry, try again", "no password was provided", "密码不正确", "密码错误"))

    def start_stress(self, host: dict[str, Any], cpu_workers: int, memory_workers: int, memory_percent: int, duration_minutes: int) -> str:
        if not 1 <= duration_minutes <= 30 or not 0 <= memory_percent <= 80 or not 0 <= cpu_workers <= 256 or not 0 <= memory_workers <= 256 or cpu_workers + memory_workers == 0:
            raise OperationError("压力测试参数无效")
        task_id = str(uuid.uuid4())
        seconds = duration_minutes * 60
        per_worker_memory = memory_percent / memory_workers if memory_workers else 0
        marker = f"server-monitor-{task_id}"
        stress_parts = ["timeout", f"{seconds}s", "stress-ng"]
        if cpu_workers:
            stress_parts.extend(["--cpu", str(cpu_workers)])
        if memory_workers:
            stress_parts.extend(["--vm", str(memory_workers), "--vm-bytes", f"{per_worker_memory:g}%"])
        stress_parts.extend(["--timeout", f"{seconds}s"])
        stress = shlex.join(stress_parts)
        command = f"nohup setsid bash -c {shlex.quote('exec -a ' + marker + ' ' + stress)} >/tmp/{marker}.log 2>&1 </dev/null & echo $!"
        result = self.run(host, command, self.config.all()["collection_timeout"])
        if result.exit_code != 0 or not result.stdout.strip().isdigit():
            raise OperationError(redact(result.stderr) or "压力测试启动失败")
        self.database.execute("INSERT INTO stress_jobs(id,host_id,state,cpu_workers,memory_workers,memory_percent,duration_seconds,remote_pid,started_at) VALUES(?,?,?,?,?,?,?,?,?)", (task_id, host["id"], "running", cpu_workers, memory_workers, memory_percent, seconds, int(result.stdout.strip()), utc_iso()))
        return task_id

    def stop_stress(self, host: dict[str, Any], task_id: str) -> None:
        row = self.database.query_one("SELECT remote_pid,state FROM stress_jobs WHERE id=? AND host_id=?", (task_id, host["id"]))
        if not row:
            raise OperationError("压力测试任务不存在")
        self.run(host, f"kill -TERM -- -{int(row['remote_pid'])} 2>/dev/null || true", self.config.all()["collection_timeout"])
        self.database.execute("UPDATE stress_jobs SET state='stopped',finished_at=? WHERE id=?", (utc_iso(), task_id))

    def stress_status(self, host: dict[str, Any], task_id: str) -> dict[str, Any]:
        row = self.database.query_one("SELECT * FROM stress_jobs WHERE id=? AND host_id=?", (task_id, host["id"]))
        if not row:
            raise OperationError("压力测试任务不存在")
        result = dict(row)
        if result["state"] == "running" and result["remote_pid"]:
            check = self.run(host, f"kill -0 -- -{int(result['remote_pid'])} 2>/dev/null", self.config.all()["collection_timeout"])
            if check.exit_code != 0:
                self.database.execute("UPDATE stress_jobs SET state='finished',finished_at=? WHERE id=?", (utc_iso(), task_id))
                result["state"] = "finished"
                result["finished_at"] = utc_iso()
        return result

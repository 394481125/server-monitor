from __future__ import annotations

import json
import shlex
import uuid
import re
from typing import Any

from .gpu_scheduler import DispatchResult
from .security import redact
from .ssh_client import SSHClient, SSHConnectionPool, SSHError, SSHTimeout
from .utils import command_summary, utc_iso


class OperationError(ValueError):
    pass


_REMOTE_PATH_LIMIT = 512
_PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+~<>=!,-]{0,199}$")
_APT_PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,119}$")
_PYTHON_SELECTOR = re.compile(r"^(?:python3|(?:python)?3\.(8|9|10|11|12|13))$")
_NETWORK_TARGET = re.compile(r"^(?!-)[A-Za-z0-9._:-]{1,253}$")
_DOCKER_OBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SYSTEMD_UNITS = (
    "nvidia-persistenced.service",
    "docker.service",
    "containerd.service",
    "ssh.service",
    "sshd.service",
    "chronyd.service",
    "systemd-timesyncd.service",
)
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
    def __init__(self, secrets: Any, config: Any, database: Any, connection_pool: SSHConnectionPool | None = None):
        self.secrets = secrets
        self.config = config
        self.database = database
        self.connection_pool = connection_pool

    def _client(self, host: dict[str, Any]) -> SSHClient:
        if self.connection_pool:
            return self.connection_pool.client(host)  # type: ignore[return-value]
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
            "ps -eo pid=,ppid=,user=,stat=,%cpu=,%mem=,rss=,lstart=,args= --sort=-%cpu || exit $?; "
            f"printf '\\n{marker}\\n'; "
            "for proc in /proc/[0-9]*; do "
            "pid=${proc##*/}; cwd=$(readlink \"$proc/cwd\" 2>/dev/null) || cwd=; "
            "swap=$(awk '/^VmSwap:/{print $2}' \"$proc/status\" 2>/dev/null); "
            "read_bytes=$(awk '/^read_bytes:/{print $2}' \"$proc/io\" 2>/dev/null); "
            "write_bytes=$(awk '/^write_bytes:/{print $2}' \"$proc/io\" 2>/dev/null); "
            "printf '%s\\t%s\\t%s\\t%s\\t%s\\n' \"$pid\" \"$cwd\" \"${swap:-0}\" \"${read_bytes:-0}\" \"${write_bytes:-0}\"; "
            "done"
        )
        result = self.run(host, command, self.config.all()["collection_timeout"])
        if result.exit_code != 0:
            raise OperationError(redact(result.stderr) or "读取进程失败")
        process_output, separator, detail_output = result.stdout.partition(f"\n{marker}\n")
        details_by_pid: dict[int, dict[str, Any]] = {}
        if separator:
            for line in detail_output.splitlines():
                detail_parts = line.split("\t")
                if len(detail_parts) != 5 or not detail_parts[0].isdigit():
                    continue

                def bounded_number(value: str) -> int:
                    try:
                        return max(0, int(value))
                    except ValueError:
                        return 0

                details_by_pid[int(detail_parts[0])] = {
                    "cwd": detail_parts[1] or None,
                    "swap_bytes": bounded_number(detail_parts[2]) * 1024,
                    "read_bytes": bounded_number(detail_parts[3]),
                    "write_bytes": bounded_number(detail_parts[4]),
                }
        rows: list[dict[str, Any]] = []
        for line in process_output.splitlines():
            parts = line.strip().split(maxsplit=12)
            if len(parts) < 13:
                continue
            raw_command = parts[12]
            if hide_kernel and raw_command.startswith("["):
                continue
            pid = int(parts[0])
            detail = details_by_pid.get(pid, {})
            try:
                rss_bytes = max(0, int(parts[6])) * 1024
            except ValueError:
                rss_bytes = 0
            rows.append({
                "pid": pid,
                "ppid": int(parts[1]),
                "user": parts[2],
                "state": parts[3],
                "cpu": float(parts[4]),
                "memory": float(parts[5]),
                "rss_bytes": rss_bytes,
                "swap_bytes": detail.get("swap_bytes", 0),
                "read_bytes": detail.get("read_bytes", 0),
                "write_bytes": detail.get("write_bytes", 0),
                "started": " ".join(parts[7:12]),
                "cwd": detail.get("cwd"),
                "command": command_summary(raw_command),
                "zombie": "Z" in parts[3],
            })
        by_pid = {row["pid"]: row for row in rows}
        for row in rows:
            depth = 0
            parent = row["ppid"]
            visited = {row["pid"]}
            while parent in by_pid and parent not in visited and depth < 12:
                visited.add(parent)
                depth += 1
                parent = by_pid[parent]["ppid"]
            row["tree_depth"] = depth
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
        specs = (
            ("tmux", "tmux"), ("htop", "htop"), ("ncdu", "ncdu"), ("nvtop", "nvtop"),
            ("sysstat", "iostat"), ("iotop", "iotop"), ("smartmontools", "smartctl"),
            ("ethtool", "ethtool"), ("iproute2", "ss"), ("lsof", "lsof"), ("jq", "jq"),
            ("git", "git"), ("rsync", "rsync"), ("unzip", "unzip"),
            ("build-essential", "gcc"), ("cmake", "cmake"), ("btop", "btop"),
            ("iperf3", "iperf3"), ("tree", "tree"), ("vim", "vim"),
            ("sensors", "sensors"), ("stress-ng", "stress-ng"), ("nvidia-smi", "nvidia-smi"),
            ("docker", "docker"),
        )
        command = "; ".join(
            f"command -v {shlex.quote(executable)} >/dev/null 2>&1 && echo {shlex.quote(name)}:available || echo {shlex.quote(name)}:missing"
            for name, executable in specs
        )
        result = self.run(host, command, self.config.all()["collection_timeout"])
        return dict(line.split(":", 1) for line in result.stdout.splitlines() if ":" in line)

    def detect_tool_versions(self, host: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Return bounded executable/version information for the tools page."""
        specs = {
            "tmux": ("tmux", "tmux"), "htop": ("htop", "htop"), "ncdu": ("ncdu", "ncdu"),
            "nvtop": ("nvtop", "nvtop"), "sysstat": ("iostat", "sysstat"), "iotop": ("iotop", "iotop"),
            "smartmontools": ("smartctl", "smartmontools"), "ethtool": ("ethtool", "ethtool"),
            "iproute2": ("ss", "iproute2"), "lsof": ("lsof", "lsof"), "jq": ("jq", "jq"),
            "git": ("git", "git"), "rsync": ("rsync", "rsync"), "unzip": ("unzip", "unzip"),
            "build-essential": ("gcc", "build-essential"), "cmake": ("cmake", "cmake"),
            "btop": ("btop", "btop"), "iperf3": ("iperf3", "iperf3"), "tree": ("tree", "tree"),
            "vim": ("vim", "vim"), "sensors": ("sensors", "lm-sensors"),
            "stress-ng": ("stress-ng", "stress-ng"), "nvidia-smi": ("nvidia-smi", None),
            "docker": ("docker", "docker.io"), "rustdesk": ("rustdesk", None),
            "rustdesktop": ("rustdesk", None), "todesk": ("todesk", None),
        }
        lines = []
        for name, (executable, package) in specs.items():
            command = shlex.quote(executable)
            if package:
                package_expr = shlex.quote(package)
                candidate = (
                    f"if command -v apt-cache >/dev/null 2>&1; then "
                    f"target=$(apt-cache policy {package_expr} 2>/dev/null | awk '/Candidate:/ {{print $2; exit}}'); "
                    f"elif command -v dnf >/dev/null 2>&1; then "
                    f"target=$(dnf repoquery --qf '%{{evr}}' {package_expr} 2>/dev/null | head -n 1); else target=''; fi"
                )
            else:
                candidate = "target=''"
            lines.append(
                f"if command -v {command} >/dev/null 2>&1; then status=available; version=$({command} --version 2>&1 | head -n 1); "
                f"else status=missing; version=''; fi; {candidate}; "
                f"printf '%s\\t%s\\t%s\\t%s\\n' {shlex.quote(name)} \"$status\" \"$version\" \"$target\""
            )
        result = self.run(host, "; ".join(lines), self.config.all()["collection_timeout"], 64 * 1024)
        parsed: dict[str, dict[str, Any]] = {}
        for line in result.stdout.splitlines():
            name, separator, rest = line.partition("\t")
            if not separator:
                continue
            status, _, rest = rest.partition("\t")
            version, _, target = rest.partition("\t")
            parsed[name] = {
                "status": status,
                "version": version[:200] or None,
                "target_version": target[:120] or None,
                "installable": name not in {"rustdesk", "rustdesktop", "todesk"},
            }
        return parsed

    @staticmethod
    def tool_target_version(tool: str) -> str:
        if tool in {"rustdesk", "rustdesktop", "todesk"}:
            return "不支持网页一键安装（请人工部署）"
        return "系统软件源最新版本（未锁定）"

    def systemd_services(self, host: dict[str, Any]) -> list[dict[str, Any]]:
        units = " ".join(shlex.quote(unit) for unit in _SYSTEMD_UNITS)
        command = (
            "if ! command -v systemctl >/dev/null 2>&1; then echo '__MISSING__'; exit 0; fi; "
            f"for unit in {units}; do "
            "load=$(systemctl show \"$unit\" -p LoadState --value 2>/dev/null); "
            "active=$(systemctl show \"$unit\" -p ActiveState --value 2>/dev/null); "
            "sub=$(systemctl show \"$unit\" -p SubState --value 2>/dev/null); "
            "enabled=$(systemctl is-enabled \"$unit\" 2>/dev/null || true); "
            "printf '%s\\t%s\\t%s\\t%s\\t%s\\n' \"$unit\" \"${load:-not-found}\" \"${active:-unknown}\" \"${sub:-unknown}\" \"${enabled:-unknown}\"; done"
        )
        result = self.run(host, command, self.config.all()["collection_timeout"], 128 * 1024)
        if "__MISSING__" in result.stdout:
            return []
        items = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 5:
                items.append({"unit": parts[0], "load": parts[1], "active": parts[2], "sub": parts[3], "enabled": parts[4]})
        return items

    def service_logs(self, host: dict[str, Any], unit: str, lines: int = 100, keyword: str = "") -> dict[str, Any]:
        if unit not in _SYSTEMD_UNITS:
            raise OperationError("不允许读取该 systemd 服务")
        if not 20 <= lines <= 500:
            raise OperationError("日志行数必须在 20～500 之间")
        keyword = str(keyword or "").strip()
        if len(keyword) > 100 or any(char in keyword for char in "\r\n\x00"):
            raise OperationError("日志关键词无效")
        command = f"journalctl -u {shlex.quote(unit)} -n {lines} --no-pager -o short-iso 2>&1"
        result = self.run(host, command, self.config.all()["collection_timeout"], 512 * 1024)
        output = result.stdout if result.stdout.strip() else result.stderr
        selected = [line for line in output.splitlines() if not keyword or keyword.lower() in line.lower()]
        return {"unit": unit, "keyword": keyword, "lines": selected[-lines:], "truncated": result.stdout_truncated or result.stderr_truncated}

    @staticmethod
    def service_restart_plan(unit: str) -> str:
        if unit not in _SYSTEMD_UNITS:
            raise OperationError("不允许生成该 systemd 服务的重启脚本")
        return f"#!/usr/bin/env bash\nset -Eeuo pipefail\nsudo systemctl restart {shlex.quote(unit)}\nsystemctl --no-pager --full status {shlex.quote(unit)}\n"

    def network_diagnostic(self, host: dict[str, Any], target: str, mode: str, port: Any = None) -> dict[str, Any]:
        target = str(target or "").strip()
        if not _NETWORK_TARGET.fullmatch(target) or "/" in target:
            raise OperationError("诊断目标必须是单个 IP 或主机名")
        if mode == "ping":
            command = f"LC_ALL=C ping -n -c 2 -W 2 -- {shlex.quote(target)} 2>&1"
        elif mode == "port":
            try:
                port_number = int(port)
            except (TypeError, ValueError) as exc:
                raise OperationError("端口必须是整数") from exc
            if not 1 <= port_number <= 65535:
                raise OperationError("端口必须在 1～65535 之间")
            command = (
                "if command -v nc >/dev/null 2>&1; then "
                f"nc -vz -w 3 -- {shlex.quote(target)} {port_number} 2>&1; "
                "else "
                f"timeout 4 bash -c 'exec 3<>/dev/tcp/$1/$2' _ {shlex.quote(target)} {port_number} 2>&1; fi"
            )
        else:
            raise OperationError("网络诊断模式无效")
        result = self.run(host, command, 8, 64 * 1024)
        return {"mode": mode, "target": target, "port": int(port) if mode == "port" else None, "success": result.exit_code == 0, "output": redact((result.stdout or result.stderr).strip())[:8000]}

    def docker_inventory(self, host: dict[str, Any]) -> dict[str, Any]:
        marker = "__SERVER_MONITOR_DOCKER__"
        command = r"""LC_ALL=C sh -s <<'SERVER_MONITOR_DOCKER_EOF'
set +e
section() { printf '\n__SERVER_MONITOR_DOCKER__:%s\n' "$1"; }
section info; docker info --format '{{json .}}' 2>&1
section images; docker image ls --no-trunc --format '{{json .}}' 2>&1
section volumes; docker volume ls --format '{{json .}}' 2>&1
section compose; if docker compose version >/dev/null 2>&1; then docker compose ls --format json 2>&1; fi
SERVER_MONITOR_DOCKER_EOF"""
        result = self.run(host, command, self.config.all()["collection_timeout"], 1024 * 1024)
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in result.stdout.splitlines():
            if line.startswith(marker + ":"):
                current = line.split(":", 1)[1]
                sections.setdefault(current, [])
            elif current:
                sections[current].append(line)
        def objects(name: str) -> list[dict[str, Any]]:
            values = []
            for line in sections.get(name, []):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    values.append(value)
                elif isinstance(value, list):
                    values.extend(item for item in value if isinstance(item, dict))
            return values
        info_rows = objects("info")
        error_text = "\n".join(sections.get("info", []))
        return {"available": bool(info_rows), "error": None if info_rows else redact(error_text)[:1000], "info": info_rows[0] if info_rows else {}, "images": objects("images")[:500], "volumes": objects("volumes")[:500], "compose": objects("compose")[:200]}

    def docker_logs(self, host: dict[str, Any], container: str, lines: int = 100, keyword: str = "") -> dict[str, Any]:
        container = str(container or "").strip()
        if not _DOCKER_OBJECT.fullmatch(container):
            raise OperationError("容器名称或 ID 无效")
        if not 20 <= lines <= 500:
            raise OperationError("日志行数必须在 20～500 之间")
        keyword = str(keyword or "").strip()
        if len(keyword) > 100 or any(char in keyword for char in "\r\n\x00"):
            raise OperationError("日志关键词无效")
        result = self.run(host, f"docker logs --tail {lines} --timestamps {shlex.quote(container)} 2>&1", self.config.all()["collection_timeout"], 512 * 1024)
        output = [line for line in result.stdout.splitlines() if not keyword or keyword.lower() in line.lower()]
        return {"container": container, "keyword": keyword, "lines": output[-lines:], "truncated": result.stdout_truncated}

    def health_inspection(self, host: dict[str, Any]) -> dict[str, Any]:
        marker = "__SERVER_MONITOR_CHECK__"
        command = r"""LC_ALL=C sh -s <<'SERVER_MONITOR_CHECK_EOF'
set +e
check() { printf '\n__SERVER_MONITOR_CHECK__:%s\n' "$1"; }
check nvidia
if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi -L 2>&1; printf '__RC__:%s\n' "$?"; else echo '__MISSING__'; fi
check inode
df -Pi -x tmpfs -x devtmpfs 2>&1
check smart
if command -v smartctl >/dev/null 2>&1; then
  smartctl_path=$(command -v smartctl)
  smartctl --scan 2>/dev/null | awk '{print $1}' | head -n 32 | while read device; do
    echo "__DEVICE__:$device"
    smart_output=$("$smartctl_path" -H "$device" 2>&1)
    case "$smart_output" in
      *"Permission denied"*|*"permission required"*|*"Operation not permitted"*)
        if command -v sudo >/dev/null 2>&1; then smart_output=$(sudo -n "$smartctl_path" -H "$device" 2>&1); fi
        ;;
    esac
    printf '%s\n' "$smart_output"
  done
else echo '__MISSING__'; fi
check dmesg
if command -v dmesg >/dev/null 2>&1; then
  dmesg_output=$(dmesg --level=err,crit,alert,emerg 2>&1)
  case "$dmesg_output" in
    *"Permission denied"*|*"Operation not permitted"*)
      if command -v journalctl >/dev/null 2>&1; then
        journal_output=$(journalctl -k -p err..alert -n 40 --no-pager 2>&1)
        case "$journal_output" in
          *"insufficient permissions"*|*"not permitted to see messages"*|*"No journal files were opened"*) printf '%s\n%s\n' "$dmesg_output" "$journal_output" ;;
          *) printf '__SOURCE__:journalctl\n%s\n' "$journal_output" ;;
        esac
      else
        printf '%s\n' "$dmesg_output"
      fi
      ;;
    *) printf '%s\n' "$dmesg_output" | tail -n 40 ;;
  esac
else echo '__MISSING__'; fi
check nouveau
if command -v lsmod >/dev/null 2>&1; then lsmod | awk '$1=="nouveau"{print}'; else echo '__MISSING__'; fi
check secure_boot
if command -v mokutil >/dev/null 2>&1; then mokutil --sb-state 2>&1; else echo '__MISSING__'; fi
check nvidia_persistenced
if command -v systemctl >/dev/null 2>&1; then systemctl is-active nvidia-persistenced.service 2>&1; systemctl is-enabled nvidia-persistenced.service 2>&1; else echo '__MISSING__'; fi
check nvidia_modules
if command -v lsmod >/dev/null 2>&1; then lsmod | awk '$1 ~ /^nvidia(_uvm|_drm|_modeset)?$/' 2>&1; else echo '__MISSING__'; fi
check kernel
uname -r 2>&1
check nfs
if command -v findmnt >/dev/null 2>&1; then findmnt -rn -t nfs,nfs4 2>&1; else echo '__MISSING__'; fi
check time_sync
if command -v timedatectl >/dev/null 2>&1; then timedatectl show -p NTPSynchronized -p NTP --value 2>&1; else echo '__MISSING__'; fi
SERVER_MONITOR_CHECK_EOF"""
        result = self.run(host, command, self.config.all()["collection_timeout"], self.config.all()["schedule_output_limit"])
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in result.stdout.splitlines():
            if line.startswith(marker + ":"):
                current = line.split(":", 1)[1]
                sections.setdefault(current, [])
            elif current:
                sections[current].append(line)
        checks: list[dict[str, Any]] = []

        def add(key: str, title: str, status: str, summary: str, details: str = "") -> None:
            checks.append({"key": key, "title": title, "status": status, "summary": summary, "details": details[:4000]})

        nvidia = "\n".join(sections.get("nvidia", []))
        if "__MISSING__" in nvidia:
            add("nvidia", "NVIDIA 驱动", "unavailable", "未安装 nvidia-smi")
        elif "__RC__:0" in nvidia:
            add("nvidia", "NVIDIA 驱动", "passed", "nvidia-smi 响应正常", nvidia.replace("__RC__:0", "").strip())
        else:
            add("nvidia", "NVIDIA 驱动", "warning", "nvidia-smi 执行失败", nvidia)
        inode_text = "\n".join(sections.get("inode", []))
        inode_values = []
        for line in inode_text.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 6 and parts[-2].endswith("%"):
                try:
                    inode_values.append((int(parts[-2].rstrip("%")), parts[-1]))
                except ValueError:
                    pass
        worst = max(inode_values, default=(0, "-"))
        add("inode", "文件系统 inode", "warning" if worst[0] >= 90 else "passed", f"最高 inode 使用率 {worst[0]}%（{worst[1]}）", inode_text)
        smart = "\n".join(sections.get("smart", []))
        permission_pattern = r"permission denied|permission required|operation not permitted|password is required|not allowed to execute|insufficient permissions|not permitted to see messages"
        if re.search(permission_pattern, smart, re.I):
            add("smart", "磁盘 SMART", "unavailable", "当前 SSH 用户无权读取 SMART；可为 smartctl 配置只读 sudo 权限", smart)
        elif "__MISSING__" in smart:
            add("smart", "磁盘 SMART", "unavailable", "未安装 smartctl")
        elif re.search(r"FAILED|failure", smart, re.I):
            add("smart", "磁盘 SMART", "warning", "SMART 报告磁盘健康异常", smart)
        elif re.search(r"PASSED|SMART Health Status:\s*OK", smart, re.I):
            add("smart", "磁盘 SMART", "passed", "SMART 健康检查通过", smart)
        else:
            add("smart", "磁盘 SMART", "unavailable", "SMART 无权限或没有可读设备", smart)
        dmesg = "\n".join(sections.get("dmesg", []))
        dmesg_details = "\n".join(
            line for line in dmesg.splitlines()
            if line.strip() not in {"__SOURCE__:journalctl", "-- No entries --"}
        ).strip()
        if "__MISSING__" in dmesg:
            add("dmesg", "内核错误", "unavailable", "远端未安装 dmesg")
        elif re.search(permission_pattern, dmesg, re.I) or "No journal files were opened" in dmesg:
            add("dmesg", "内核错误", "unavailable", "当前 SSH 用户无权读取 dmesg 或内核日志", dmesg)
        elif dmesg_details:
            source = "journalctl 内核日志" if "__SOURCE__:journalctl" in dmesg else "dmesg"
            add("dmesg", "内核错误", "warning", f"最近 {source} 包含错误级事件", dmesg_details)
        else:
            add("dmesg", "内核错误", "passed", "未发现错误级内核事件")
        nouveau = "\n".join(sections.get("nouveau", []))
        nouveau_active = bool(nouveau.strip() and "__MISSING__" not in nouveau)
        add("nouveau", "Nouveau 冲突", "warning" if nouveau_active else "passed", "检测到 nouveau 内核模块" if nouveau_active else "未检测到 nouveau 模块", nouveau)
        secure_boot = "\n".join(sections.get("secure_boot", []))
        if "__MISSING__" in secure_boot:
            add("secure_boot", "Secure Boot", "unavailable", "未安装 mokutil，无法自动判断")
        else:
            enabled = "enabled" in secure_boot.lower()
            add("secure_boot", "Secure Boot", "warning" if enabled else "passed", "Secure Boot 已启用，驱动模块需正确签名" if enabled else "Secure Boot 未启用", secure_boot)
        persistenced = "\n".join(sections.get("nvidia_persistenced", []))
        if "__MISSING__" in persistenced:
            add("nvidia_persistenced", "nvidia-persistenced", "unavailable", "未安装 systemctl，无法判断服务状态")
        else:
            active = any(line.strip() == "active" for line in persistenced.splitlines())
            enabled = any(line.strip() in {"enabled", "static"} for line in persistenced.splitlines())
            add("nvidia_persistenced", "nvidia-persistenced", "passed" if active else "warning", "服务正在运行" if active else f"服务未运行（开机启用：{'是' if enabled else '否'}）", persistenced)
        modules = "\n".join(sections.get("nvidia_modules", []))
        if "__MISSING__" in modules:
            add("nvidia_modules", "NVIDIA 内核模块", "unavailable", "未安装 lsmod")
        else:
            add("nvidia_modules", "NVIDIA 内核模块", "passed" if re.search(r"^nvidia\s", modules, re.M) else "warning", "已加载 nvidia 模块" if re.search(r"^nvidia\s", modules, re.M) else "未发现 nvidia 模块", modules)
        kernel = "\n".join(sections.get("kernel", [])).strip()
        add("kernel", "内核版本", "passed" if kernel else "unavailable", kernel or "无法读取内核版本", kernel)
        nfs = "\n".join(sections.get("nfs", []))
        add("nfs", "NFS 挂载", "passed", "未发现 NFS 挂载" if not nfs.strip() or "__MISSING__" in nfs else "检测到 NFS 挂载", nfs)
        time_sync = "\n".join(sections.get("time_sync", []))
        if "__MISSING__" in time_sync:
            add("time_sync", "NTP 时间同步", "unavailable", "未安装 timedatectl")
        else:
            synced = "yes" in time_sync.lower() or "true" in time_sync.lower()
            add("time_sync", "NTP 时间同步", "passed" if synced else "warning", "NTP 已同步" if synced else "NTP 尚未确认同步", time_sync)
        return {
            "checks": checks,
            "passed": sum(1 for item in checks if item["status"] == "passed"),
            "warnings": sum(1 for item in checks if item["status"] == "warning"),
            "unavailable": sum(1 for item in checks if item["status"] == "unavailable"),
            "inspected_at": utc_iso(),
        }

    def installation_command(self, host: dict[str, Any], tool: str) -> str:
        packages = {
            "tmux": {"deb": "tmux", "rpm": "tmux"},
            "htop": {"deb": "htop", "rpm": "htop"},
            "ncdu": {"deb": "ncdu", "rpm": "ncdu"},
            "nvtop": {"deb": "nvtop", "rpm": "nvtop"},
            "sysstat": {"deb": "sysstat", "rpm": "sysstat"},
            "iotop": {"deb": "iotop", "rpm": "iotop"},
            "smartmontools": {"deb": "smartmontools", "rpm": "smartmontools"},
            "ethtool": {"deb": "ethtool", "rpm": "ethtool"},
            "iproute2": {"deb": "iproute2", "rpm": "iproute"},
            "lsof": {"deb": "lsof", "rpm": "lsof"},
            "jq": {"deb": "jq", "rpm": "jq"},
            "git": {"deb": "git", "rpm": "git"},
            "rsync": {"deb": "rsync", "rpm": "rsync"},
            "unzip": {"deb": "unzip", "rpm": "unzip"},
            "build-essential": {"deb": "build-essential", "rpm": "gcc"},
            "cmake": {"deb": "cmake", "rpm": "cmake"},
            "btop": {"deb": "btop", "rpm": "btop"},
            "iperf3": {"deb": "iperf3", "rpm": "iperf3"},
            "tree": {"deb": "tree", "rpm": "tree"},
            "vim": {"deb": "vim", "rpm": "vim-enhanced"},
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

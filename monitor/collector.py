from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .ssh_client import SSHClient, SSHError
from .utils import utc_iso


SECTION = "__SERVER_MONITOR_SECTION__"
CORE_COMMAND = r"""LC_ALL=C sh -s <<'SERVER_MONITOR_EOF'
set +e
section() { printf '\n__SERVER_MONITOR_SECTION__:%s\n' "$1"; }
section identity; hostname; cat /etc/machine-id 2>/dev/null || true
section proc_stat; cat /proc/stat
section meminfo; cat /proc/meminfo
section loadavg; cat /proc/loadavg
section uptime; cat /proc/uptime
section filesystems; df -Pk -x tmpfs -x devtmpfs
section inode_filesystems; df -Pi -x tmpfs -x devtmpfs
section netdev; cat /proc/net/dev
section diskstats; cat /proc/diskstats
section block_devices
if command -v lsblk >/dev/null 2>&1; then lsblk -bdn -o NAME,TYPE,SIZE 2>/dev/null; fi
section tools
for tool in tmux smartctl sensors stress-ng nvidia-smi docker ss; do
  if command -v "$tool" >/dev/null 2>&1; then echo "$tool:available"; else echo "$tool:missing"; fi
done
section gpu
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,uuid,name,driver_version,utilization.gpu,memory.total,memory.used,temperature.gpu,power.draw,fan.speed --format=csv,noheader,nounits 2>&1
fi
section gpu_processes
if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory --format=csv,noheader,nounits 2>&1; fi
section temperatures
if command -v sensors >/dev/null 2>&1; then sensors -u 2>&1; fi
section temperature_sysfs
for hwmon in /sys/class/hwmon/hwmon*; do
  [ -r "$hwmon/name" ] || continue
  chip=$(cat "$hwmon/name" 2>/dev/null)
  case "$chip" in
    coretemp|k10temp|zenpower)
      for input in "$hwmon"/temp*_input; do
        [ -r "$input" ] || continue
        base=${input%_input}
        label=$(cat "${base}_label" 2>/dev/null)
        [ -n "$label" ] || continue
        printf '%s\t%s\t%s\n' "$chip" "$label" "$(cat "$input" 2>/dev/null)"
      done
      ;;
  esac
done
section tcp_connections
if command -v ss >/dev/null 2>&1; then
  ss -Htan 2>/dev/null | awk '{ total += 1; if ($1 == "ESTAB") established += 1; if ($1 == "TIME-WAIT") time_wait += 1 } END { printf "total\\t%d\\nestablished\\t%d\\ntime_wait\\t%d\\n", total, established, time_wait }'
fi
section listening_ports
if command -v ss >/dev/null 2>&1; then
  { ss -Hltn 2>/dev/null | awk '{ endpoint=$4; port=endpoint; sub(/^.*:/, "", port); if (port ~ /^[0-9]+$/) print "tcp\\t" port "\\t" endpoint }'; ss -Hlun 2>/dev/null | awk '{ endpoint=$4; port=endpoint; sub(/^.*:/, "", port); if (port ~ /^[0-9]+$/) print "udp\\t" port "\\t" endpoint }'; } | sort -u | head -n 256
fi
section listening_port_count
if command -v ss >/dev/null 2>&1; then
  { ss -Hltn 2>/dev/null | awk '{ endpoint=$4; port=endpoint; sub(/^.*:/, "", port); if (port ~ /^[0-9]+$/) print "tcp\\t" port "\\t" endpoint }'; ss -Hlun 2>/dev/null | awk '{ endpoint=$4; port=endpoint; sub(/^.*:/, "", port); if (port ~ /^[0-9]+$/) print "udp\\t" port "\\t" endpoint }'; } | sort -u | wc -l
fi
section docker
if [ "${SERVER_MONITOR_DOCKER_ENABLED:-1}" = "1" ] && command -v docker >/dev/null 2>&1; then docker ps --format '{{json .}}' 2>&1; fi
section docker_stats
if [ "${SERVER_MONITOR_DOCKER_ENABLED:-1}" = "1" ] && command -v docker >/dev/null 2>&1; then docker stats --no-stream --format '{{json .}}' 2>&1; fi
section smart
if command -v smartctl >/dev/null 2>&1 && command -v lsblk >/dev/null 2>&1; then
  smartctl_path=$(command -v smartctl)
  lsblk -dn -o PATH,TYPE 2>/dev/null | while read device type; do
    [ "$type" = disk ] || continue
    echo "__DEVICE__:$device"
    smart_output=$("$smartctl_path" -H -A "$device" 2>&1)
    case "$smart_output" in
      *"Permission denied"*|*"permission required"*|*"Operation not permitted"*)
        smart_output=$(sudo -n "$smartctl_path" -H -A "$device" 2>&1)
        ;;
    esac
    printf '%s\n' "$smart_output"
  done
fi
SERVER_MONITOR_EOF"""


@dataclass
class CollectionResult:
    core_ok: bool
    data: dict[str, Any]
    optional_errors: dict[str, str]
    fingerprint: str | None = None
    error: str | None = None


def _sections(output: str) -> dict[str, str]:
    current: str | None = None
    found: dict[str, list[str]] = {}
    for line in output.splitlines():
        if line.startswith(SECTION + ":"):
            current = line.split(":", 1)[1]
            found.setdefault(current, [])
        elif current is not None:
            found[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in found.items()}


def _number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_cpu(text: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    lines = [line.split() for line in text.splitlines() if line.startswith("cpu")]
    if not lines:
        raise ValueError("/proc/stat 不包含 CPU 数据")
    counts: dict[str, list[int]] = {}
    for parts in lines:
        values = [int(value) for value in parts[1:] if value.isdigit()]
        counts[parts[0]] = values
    # Collection snapshots keep CPU counters under the ``cpu`` metric. Keep
    # accepting the flat shape for callers and older persisted snapshots.
    previous_cpu = (previous or {}).get("cpu", previous or {})
    previous_counts = previous_cpu.get("cpu_counts", {})

    def total(values: list[int]) -> int:
        # guest and guest_nice are already included in user and nice.
        return sum(values[:8])

    def delta_percent(name: str, values: list[int], index: int, *, invert: bool = False) -> float | None:
        old = previous_counts.get(name)
        if not old or len(old) != len(values):
            return None
        total_now = total(values)
        total_old = total(old)
        total_delta = total_now - total_old
        value_delta = values[index] - old[index] if len(values) > index else 0
        if total_delta <= 0 or value_delta < 0:
            return None
        percent = value_delta * 100 / total_delta
        if invert:
            percent = 100 - percent
        return round(max(0.0, min(100.0, percent)), 2)

    def usage(name: str, values: list[int]) -> float | None:
        old = previous_counts.get(name)
        if not old or len(old) != len(values):
            return None
        total_now = total(values)
        total_old = total(old)
        idle_now = values[3] + (values[4] if len(values) > 4 else 0)
        idle_old = old[3] + (old[4] if len(old) > 4 else 0)
        total_delta = total_now - total_old
        idle_delta = idle_now - idle_old
        if total_delta <= 0 or idle_delta < 0:
            return None
        return round(max(0.0, min(100.0, (total_delta - idle_delta) * 100 / total_delta)), 2)

    return {
        "usage_percent": usage("cpu", counts["cpu"]),
        "iowait_percent": delta_percent("cpu", counts["cpu"], 4),
        "per_core_percent": {name: usage(name, values) for name, values in counts.items() if name != "cpu"},
        "logical_cores": len([name for name in counts if name.startswith("cpu") and name != "cpu"]),
        "cpu_counts": counts,
    }


def _parse_meminfo(text: str) -> dict[str, Any]:
    raw: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            raw[match.group(1)] = int(match.group(2)) * 1024
    total = raw.get("MemTotal")
    available = raw.get("MemAvailable")
    if not total or available is None:
        raise ValueError("/proc/meminfo 不完整")
    swap_total = raw.get("SwapTotal", 0)
    swap_free = raw.get("SwapFree", 0)
    used = max(0, total - available)
    return {
        "total": total,
        "available": available,
        "used": used,
        "usage_percent": round(used * 100 / total, 2),
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
        "swap_usage_percent": round((swap_total - swap_free) * 100 / swap_total, 2) if swap_total else 0,
    }


def _parse_filesystems(text: str) -> list[dict[str, Any]]:
    filesystems: list[dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            total, used, available = (int(value) * 1024 for value in parts[1:4])
        except ValueError:
            continue
        filesystems.append(
            {
                "filesystem": parts[0],
                "mountpoint": " ".join(parts[5:]),
                "total": total,
                "used": used,
                "available": available,
                "usage_percent": round(used * 100 / total, 2) if total else 0,
            }
        )
    if not filesystems:
        raise ValueError("df 输出为空或无法解析")
    return filesystems


def _parse_inode_filesystems(text: str) -> dict[str, dict[str, Any]]:
    filesystems: dict[str, dict[str, Any]] = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            total, used, available = (int(value) for value in parts[1:4])
        except ValueError:
            continue
        mountpoint = " ".join(parts[5:])
        filesystems[mountpoint] = {
            "inode_total": total,
            "inode_used": used,
            "inode_available": available,
            "inode_usage_percent": round(used * 100 / total, 2) if total else 0,
        }
    return filesystems


def _parse_tcp_connections(text: str) -> dict[str, int] | None:
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or parts[0] not in {"total", "established", "time_wait"}:
            continue
        try:
            values[parts[0]] = max(0, int(parts[1]))
        except ValueError:
            continue
    if not values:
        return None
    return {key: values.get(key, 0) for key in ("total", "established", "time_wait")}


def _parse_listening_ports(text: str) -> list[dict[str, Any]]:
    listeners: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[0] not in {"tcp", "udp"}:
            continue
        try:
            port = int(parts[1])
        except ValueError:
            continue
        if not 1 <= port <= 65535:
            continue
        item = (parts[0], port, parts[2])
        if item in seen:
            continue
        seen.add(item)
        listeners.append({"protocol": parts[0], "port": port, "address": parts[2]})
    return listeners


def _parse_count(text: str) -> int | None:
    try:
        return max(0, int(text.strip()))
    except (TypeError, ValueError):
        return None


def _parse_network(text: str, previous: dict[str, Any] | None, elapsed: float | None) -> list[dict[str, Any]]:
    old = (previous or {}).get("network_counters", {})
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, values = line.split(":", 1)
        name = name.strip()
        if name == "lo":
            continue
        fields = values.split()
        if len(fields) < 12:
            continue
        try:
            rx_bytes, rx_errors, rx_drop = int(fields[0]), int(fields[2]), int(fields[3])
            tx_bytes, tx_errors, tx_drop = int(fields[8]), int(fields[10]), int(fields[11])
        except ValueError:
            continue
        current = {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes}
        prior = old.get(name)
        rx_rate = tx_rate = None
        if prior and elapsed and elapsed > 0:
            rx_delta = rx_bytes - prior.get("rx_bytes", rx_bytes)
            tx_delta = tx_bytes - prior.get("tx_bytes", tx_bytes)
            if rx_delta >= 0:
                rx_rate = round(rx_delta / elapsed, 2)
            if tx_delta >= 0:
                tx_rate = round(tx_delta / elapsed, 2)
        rows.append({"name": name, **current, "rx_rate": rx_rate, "tx_rate": tx_rate, "rx_errors": rx_errors, "rx_dropped": rx_drop, "tx_errors": tx_errors, "tx_dropped": tx_drop})
    if not rows:
        raise ValueError("/proc/net/dev 无可用网卡")
    return rows


def _parse_gpu(text: str, process_text: str) -> list[dict[str, Any]]:
    processes: dict[str, list[dict[str, Any]]] = {}
    for line in process_text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 4 and parts[0].startswith("GPU-"):
            processes.setdefault(parts[0], []).append({"pid": parts[1], "name": parts[2], "memory_mib": _number(parts[3])})
    gpus: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 10 or not parts[1].startswith("GPU-"):
            continue
        total = _number(parts[5])
        used = _number(parts[6])
        gpus.append(
            {
                "index": int(parts[0]),
                "uuid": parts[1],
                "name": parts[2],
                "driver": parts[3],
                "utilization_percent": _number(parts[4]),
                "memory_total_mib": total,
                "memory_used_mib": used,
                "memory_percent": round(used * 100 / total, 2) if total and used is not None else None,
                "temperature_c": _number(parts[7]),
                "power_w": _number(parts[8]),
                "fan_percent": _number(parts[9]),
                "processes": processes.get(parts[1], []),
            }
        )
    return gpus


def _parse_cpu_temperature(text: str, sysfs_text: str = "") -> float | None:
    chip_supported = False
    label_supported = False
    values: list[float] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if raw_line and not raw_line[0].isspace() and ":" not in line:
            chip_supported = bool(re.search(r"(?:coretemp|k10temp|zenpower)", line, re.I))
            label_supported = False
        elif chip_supported and line.endswith(":"):
            label_supported = bool(re.search(r"(?:package|core|tdie|tctl)", line, re.I))
        elif chip_supported and label_supported and re.match(r"temp\d+_input:", line):
            value = _number(line.split(":", 1)[1].strip())
            if value is not None:
                values.append(value)
    for raw_line in sysfs_text.splitlines():
        parts = raw_line.split("\t", 2)
        if len(parts) != 3:
            continue
        chip, label, raw_value = parts
        if not re.search(r"^(?:coretemp|k10temp|zenpower)$", chip, re.I):
            continue
        if not re.search(r"(?:package|core|tdie|tctl)", label, re.I):
            continue
        value = _number(raw_value)
        if value is None:
            continue
        value = value / 1000 if abs(value) > 1000 else value
        if 0 <= value <= 200:
            values.append(value)
    return max(values) if values else None


def _parse_diskstats(text: str, previous: dict[str, Any] | None, elapsed: float | None) -> list[dict[str, Any]]:
    old = (previous or {}).get("disk_counters", {})
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        try:
            reads, sectors_read, writes, sectors_written, io_ms = int(parts[3]), int(parts[5]), int(parts[7]), int(parts[9]), int(parts[12])
        except ValueError:
            continue
        current = {"reads": reads, "sectors_read": sectors_read, "writes": writes, "sectors_written": sectors_written, "io_ms": io_ms}
        rates = {"read_bytes_rate": None, "write_bytes_rate": None, "read_ops_rate": None, "write_ops_rate": None, "busy_percent": None}
        prior = old.get(name)
        if prior and elapsed and elapsed > 0:
            deltas = {key: current[key] - int(prior.get(key, current[key])) for key in current}
            if all(value >= 0 for value in deltas.values()):
                rates = {
                    "read_bytes_rate": round(deltas["sectors_read"] * 512 / elapsed, 2),
                    "write_bytes_rate": round(deltas["sectors_written"] * 512 / elapsed, 2),
                    "read_ops_rate": round(deltas["reads"] / elapsed, 2),
                    "write_ops_rate": round(deltas["writes"] / elapsed, 2),
                    "busy_percent": round(min(100.0, deltas["io_ms"] / (elapsed * 10)), 2),
                }
        rows.append({"name": name, **current, **rates})
    return rows


def _parse_block_devices(text: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1] != "disk":
            continue
        try:
            size = int(parts[2])
        except ValueError:
            continue
        if size > 0:
            devices.append({"name": parts[0], "size": size})
    return devices


def _parse_docker(text: str, stats_text: str = "") -> tuple[list[dict[str, Any]], str | None]:
    containers: list[dict[str, Any]] = []
    if not text:
        return containers, None
    if "permission denied" in text.lower():
        return containers, "无权限"
    for line in text.splitlines():
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            if "Cannot connect" in line or "not running" in line:
                return containers, "Docker 未运行"
    stats: dict[str, dict[str, Any]] = {}
    for line in stats_text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            if "permission denied" in line.lower():
                return containers, "无权限"
            continue
        key = item.get("ID") or item.get("Name")
        if key:
            stats[key] = item
    for container in containers:
        current = stats.get(container.get("ID")) or stats.get(container.get("Names")) or {}
        cpu = str(current.get("CPUPerc", "")).rstrip("%")
        memory = str(current.get("MemPerc", "")).rstrip("%")
        container["cpu_percent"] = _number(cpu)
        container["memory_percent"] = _number(memory)
        container["memory_usage"] = current.get("MemUsage")
        container["network_io"] = current.get("NetIO")
    return containers, None


def _parse_smart(text: str) -> tuple[list[dict[str, Any]], str | None]:
    devices: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    permission_error = False
    for line in text.splitlines():
        if line.startswith("__DEVICE__:"):
            if current:
                devices.append(current)
            current = {"device": line.split(":", 1)[1], "health": "未知", "temperature_c": None, "reason": None}
            continue
        if current is None:
            continue
        lower = line.lower()
        if any(marker in lower for marker in ("permission denied", "permission required", "operation not permitted", "password is required", "not allowed to execute")):
            current["reason"] = "无权限"
            permission_error = True
        if re.search(r"overall-health.*passed|smart health status:\s*ok", line, re.I):
            current["health"] = "良好"
        elif re.search(r"overall-health.*failed|smart health status:\s*failed", line, re.I):
            current["health"] = "故障"
        elif re.search(r"prefail|pre-fail", line, re.I) and not re.search(r"old_age|always", line, re.I):
            current["health"] = "警告"
        temperature = re.search(r"Current Drive Temperature:\s*(\d+)", line, re.I)
        if not temperature and "Temperature_Celsius" in line:
            numbers = re.findall(r"\b\d+\b", line)
            temperature = re.match(r"(\d+)", numbers[-1]) if numbers else None
        if temperature:
            current["temperature_c"] = float(temperature.group(1))
    if current:
        devices.append(current)
    return devices, "无权限" if permission_error and all(item["health"] == "未知" for item in devices) else None


class Collector:
    def __init__(self, secret_box: Any, settings: dict[str, Any]):
        self.secret_box = secret_box
        self.settings = settings

    def collect(self, host: dict[str, Any], previous: dict[str, Any] | None = None) -> CollectionResult:
        client = SSHClient(host, self.secret_box, self.settings)
        started = time.monotonic()
        try:
            fingerprint = client.connect()
            docker_enabled = "1" if host.get("docker_enabled", True) else "0"
            command = f"SERVER_MONITOR_DOCKER_ENABLED={docker_enabled} {CORE_COMMAND}"
            result = client.run(command, host.get("timeout_seconds") or self.settings["collection_timeout"])
            if result.exit_code != 0 and not result.stdout:
                return CollectionResult(False, {}, {}, fingerprint, result.stderr or "远端采集命令失败")
            parts = _sections(result.stdout)
            cpu = _parse_cpu(parts.get("proc_stat", ""), previous)
            memory = _parse_meminfo(parts.get("meminfo", ""))
            filesystems = _parse_filesystems(parts.get("filesystems", ""))
            inode_filesystems = _parse_inode_filesystems(parts.get("inode_filesystems", ""))
            for filesystem in filesystems:
                filesystem.update(inode_filesystems.get(filesystem["mountpoint"], {}))
            previous_time = (previous or {}).get("collected_monotonic")
            elapsed = time.monotonic() - previous_time if previous_time else None
            network = _parse_network(parts.get("netdev", ""), previous, elapsed)
            disks_io = _parse_diskstats(parts.get("diskstats", ""), previous, elapsed)
            identity_lines = parts.get("identity", "").splitlines()
            tools = {
                key: ("可用" if value == "available" else "未安装")
                for line in parts.get("tools", "").splitlines()
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            load_parts = parts.get("loadavg", "").split()
            uptime_parts = parts.get("uptime", "").split()
            optional_errors: dict[str, str] = {}
            gpus: list[dict[str, Any]] = []
            if tools.get("nvidia-smi") == "可用":
                gpus = _parse_gpu(parts.get("gpu", ""), parts.get("gpu_processes", ""))
                if not gpus and parts.get("gpu", ""):
                    optional_errors["gpu"] = "输出解析失败"
            docker, docker_error = _parse_docker(parts.get("docker", ""), parts.get("docker_stats", ""))
            if docker_error:
                optional_errors["docker"] = docker_error
            smart, smart_error = _parse_smart(parts.get("smart", ""))
            if tools.get("smartctl") == "可用" and smart_error:
                optional_errors["smart"] = smart_error
            tcp = _parse_tcp_connections(parts.get("tcp_connections", ""))
            listening_ports = _parse_listening_ports(parts.get("listening_ports", ""))
            listening_port_count = _parse_count(parts.get("listening_port_count", ""))
            if tools.get("ss") != "可用":
                optional_errors["sockets"] = "ss 未安装，未采集 TCP 与监听端口"
            data = {
                "collected_at": utc_iso(),
                "collected_monotonic": time.monotonic(),
                "identity": {"hostname": identity_lines[0] if identity_lines else None, "machine_id": identity_lines[1] if len(identity_lines) > 1 else None},
                "cpu": cpu,
                "memory": memory,
                "load": {"one": _number(load_parts[0]) if len(load_parts) > 0 else None, "five": _number(load_parts[1]) if len(load_parts) > 1 else None, "fifteen": _number(load_parts[2]) if len(load_parts) > 2 else None},
                "uptime_seconds": _number(uptime_parts[0]) if uptime_parts else None,
                "filesystems": filesystems,
                "disks_io": disks_io,
                "block_devices": _parse_block_devices(parts.get("block_devices", "")),
                "disk_counters": {row["name"]: {key: row[key] for key in ("reads", "sectors_read", "writes", "sectors_written", "io_ms")} for row in disks_io},
                "network": network,
                "network_counters": {row["name"]: {"rx_bytes": row["rx_bytes"], "tx_bytes": row["tx_bytes"]} for row in network},
                "tcp": tcp,
                "listening_ports": listening_ports,
                "listening_port_count": listening_port_count,
                "gpus": gpus,
                "cpu_temperature_c": _parse_cpu_temperature(parts.get("temperatures", ""), parts.get("temperature_sysfs", "")),
                "docker": docker,
                "smart": smart,
                "tools": tools,
                "optional_errors": optional_errors,
                "duration_seconds": round(time.monotonic() - started, 3),
            }
            return CollectionResult(True, data, optional_errors, fingerprint)
        except SSHError as exc:
            return CollectionResult(False, {}, {}, None, str(exc))
        except (ValueError, KeyError, IndexError) as exc:
            return CollectionResult(False, {}, {}, None, f"核心指标解析失败: {exc}")
        finally:
            client.close()


def flattened_metrics(data: dict[str, Any]) -> list[tuple[str, str, float]]:
    values: list[tuple[str, str, float]] = []
    for metric, value in (
        ("cpu_usage", data.get("cpu", {}).get("usage_percent")),
        ("cpu_iowait", data.get("cpu", {}).get("iowait_percent")),
        ("memory_usage", data.get("memory", {}).get("usage_percent")),
    ):
        if value is not None:
            values.append((metric, "", float(value)))
    for filesystem in data.get("filesystems", []):
        values.append(("filesystem_usage", filesystem["mountpoint"], float(filesystem["usage_percent"])))
        if filesystem.get("inode_usage_percent") is not None:
            values.append(("filesystem_inode_usage", filesystem["mountpoint"], float(filesystem["inode_usage_percent"])))
    for metric, value in (data.get("tcp") or {}).items():
        values.append((f"tcp_{metric}", "", float(value)))
    if "listening_ports" in data:
        count = data.get("listening_port_count")
        values.append(("listening_ports", "", float(len(data.get("listening_ports") or []) if count is None else count)))
    if data.get("cpu_temperature_c") is not None:
        values.append(("cpu_temperature", "", float(data["cpu_temperature_c"])))
    for disk in data.get("disks_io", []):
        for metric, value in (("disk_read_rate", disk["read_bytes_rate"]), ("disk_write_rate", disk["write_bytes_rate"]), ("disk_busy", disk["busy_percent"])):
            if value is not None:
                values.append((metric, disk["name"], float(value)))
    for disk in data.get("smart", []):
        if disk.get("temperature_c") is not None:
            values.append(("disk_temperature", disk["device"], float(disk["temperature_c"])))
    for interface in data.get("network", []):
        for metric, value in (("network_rx_rate", interface["rx_rate"]), ("network_tx_rate", interface["tx_rate"])):
            if value is not None:
                values.append((metric, interface["name"], float(value)))
    for gpu in data.get("gpus", []):
        for metric, value in (("gpu_utilization", gpu["utilization_percent"]), ("gpu_memory", gpu["memory_percent"]), ("gpu_temperature", gpu["temperature_c"])):
            if value is not None:
                values.append((metric, gpu["uuid"], float(value)))
    return values

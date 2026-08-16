from __future__ import annotations

import re
import shlex
import base64
from typing import Any

from .operations import (
    OperationError,
    _APT_PACKAGE_PATTERN,
    _PACKAGE_PATTERN,
    _PYTHON_SELECTOR,
    _PYTORCH_PRESETS,
    _parse_development_stack,
    _parse_gpu_diagnostics,
    _remote_path,
)
from .security import redact


def _bash(script: str) -> str:
    return "bash -lc " + shlex.quote(script)


def _bounded_scan_value(value: Any, label: str, default: int, minimum: int, maximum: int) -> int:
    if value in {None, ""}:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OperationError(f"{label}无效") from exc
    if not minimum <= parsed <= maximum:
        raise OperationError(f"{label}须在 {minimum}～{maximum} 之间")
    return parsed


class DevelopmentService:
    """Read-only inventory and narrowly generated development environment plans."""

    def __init__(self, operations: Any, config: Any):
        self.operations = operations
        self.config = config

    def development_stack(self, host: dict[str, Any], timeout_seconds: int | None = None) -> dict[str, Any]:
        script = r'''set +e
sm_run() {
  duration=$1
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=1s "$duration" "$@"
  else
    "$@"
  fi
}
sm_timed_out() { [ "$1" -eq 124 ] || [ "$1" -eq 137 ]; }
sm_first_executable() {
  for candidate in "$@"; do
    if [ -x "$candidate" ]; then
      printf "%s" "$candidate"
      return 0
    fi
  done
  return 1
}
if [ -r /etc/os-release ]; then . /etc/os-release; fi
printf "__SM_OS__\t%s\t%s\n" "${ID:-unknown}" "${VERSION_ID:-unknown}"
for x in python3 python3.8 python3.9 python3.10 python3.11 python3.12 python3.13 conda uv nvidia-smi nvcc ubuntu-drivers apt-get dpkg-query; do
  p=$(command -v "$x" 2>/dev/null || true)
  if [ -z "$p" ]; then
    case "$x" in
      conda) p=$(sm_first_executable "$HOME/miniconda3/bin/conda" "$HOME/miniconda/bin/conda" "$HOME/anaconda3/bin/conda" "$HOME/anaconda/bin/conda" "$HOME/mambaforge/bin/conda" "$HOME/miniforge3/bin/conda" /opt/conda/bin/conda /opt/miniconda3/bin/conda /opt/anaconda3/bin/conda /usr/local/miniconda3/bin/conda /usr/local/anaconda3/bin/conda || true) ;;
      uv) p=$(sm_first_executable "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /usr/local/bin/uv || true) ;;
      nvcc) p=$(sm_first_executable /usr/local/cuda/bin/nvcc /usr/local/cuda-*/bin/nvcc /opt/cuda/bin/nvcc /opt/cuda-*/bin/nvcc || true) ;;
    esac
  fi
  v=""
  if [ -n "$p" ]; then
    version_output=$(sm_run 2s "$p" --version 2>&1)
    rc=$?
    v=$(printf "%s\n" "$version_output" | head -n 1)
    if sm_timed_out "$rc"; then
      printf "__SM_WARNING__\t%s version 探测超时，已跳过\n" "$x"
      v=""
    fi
  fi
  printf "__SM_TOOL__\t%s\t%s\t%s\n" "$x" "$p" "$v"
done
nvidia_smi_path=$(command -v nvidia-smi 2>/dev/null || true)
if [ -n "$nvidia_smi_path" ]; then
  driver_output=$(sm_run 4s "$nvidia_smi_path" --query-gpu=driver_version --format=csv,noheader,nounits 2>/dev/null)
  driver_rc=$?
  if sm_timed_out "$driver_rc"; then
    printf "__SM_WARNING__\tNVIDIA 驱动版本探测超时，已跳过\n"
  elif [ "$driver_rc" -ne 0 ]; then
    printf "__SM_WARNING__\tNVIDIA 驱动状态读取失败，请运行 GPU 健康自检\n"
  else
    printf "%s\n" "$driver_output" | awk '/^[0-9]+(\.[0-9]+)+$/{print "__SM_DRIVER__\t" $1; exit}'
  fi
fi
if command -v ubuntu-drivers >/dev/null 2>&1; then
  recommended_output=$(sm_run 4s ubuntu-drivers devices 2>/dev/null)
  recommended_rc=$?
  if sm_timed_out "$recommended_rc"; then
    printf "__SM_RECOMMENDED_DRIVER_NOTE__\t自动推荐值暂不可用（远端 ubuntu-drivers 超过 4 秒，已跳过）\n"
  else
    printf "%s\n" "$recommended_output" | awk '/driver[[:space:]]*:[[:space:]].*recommended/{print $3; exit}' | sed "s/^/__SM_RECOMMENDED_DRIVER__\t/"
  fi
fi
nvcc_path=$(command -v nvcc 2>/dev/null || sm_first_executable /usr/local/cuda/bin/nvcc /usr/local/cuda-*/bin/nvcc /opt/cuda/bin/nvcc /opt/cuda-*/bin/nvcc || true)
if [ -n "$nvcc_path" ]; then
  nvcc_output=$(sm_run 3s "$nvcc_path" --version 2>/dev/null)
  nvcc_rc=$?
  if sm_timed_out "$nvcc_rc"; then
    printf "__SM_WARNING__\tCUDA nvcc 探测超时，已跳过\n"
  else
    printf "%s\n" "$nvcc_output" | sed -n 's/.*release \([^,]*\).*/__SM_NVCC__\t\1/p' | tail -n 1
  fi
fi
if command -v dpkg-query >/dev/null 2>&1; then
  cudnn_output=$(sm_run 3s dpkg-query -W -f='${binary:Package}\t${Version}\n' 'libcudnn*' 2>/dev/null)
  cudnn_rc=$?
  if sm_timed_out "$cudnn_rc"; then
    printf "__SM_WARNING__\tcuDNN 软件包探测超时，已跳过\n"
  else
    printf "%s\n" "$cudnn_output" | sed '/^$/d;s/^/__SM_CUDNN__\t/'
  fi
fi
if command -v ldconfig >/dev/null 2>&1; then
  cudnn_libraries_output=$(sm_run 3s ldconfig -p 2>/dev/null)
  cudnn_libraries_rc=$?
  if sm_timed_out "$cudnn_libraries_rc"; then
    printf "__SM_WARNING__\tcuDNN 动态库探测超时，已跳过\n"
  else
    printf "%s\n" "$cudnn_libraries_output" | awk '/libcudnn\.so/{print "__SM_CUDNN_LIBRARY__\t" $1 "\t" $NF; count++; if (count >= 20) exit}'
  fi
fi
'''
        timeout_seconds = timeout_seconds or self.config.all()["collection_timeout"]
        result = self.operations.run(
            host,
            _bash(script),
            timeout_seconds,
            self.config.all()["schedule_output_limit"],
        )
        return _parse_development_stack(result.stdout)

    def gpu_diagnostics(self, host: dict[str, Any]) -> dict[str, Any]:
        script = r'''set +e
sm_run() {
  duration=$1
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=1s "$duration" "$@"
  else
    "$@"
  fi
}
sm_timed_out() { [ "$1" -eq 124 ] || [ "$1" -eq 137 ]; }
if ! command -v nvidia-smi >/dev/null 2>&1; then
  printf "__SM_GPU_AVAILABLE__\t0\n"
  exit 0
fi
printf "__SM_GPU_AVAILABLE__\t1\n"
gpu_output=$(sm_run 4s nvidia-smi --query-gpu=index,uuid,name,driver_version,temperature.gpu,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits 2>/dev/null)
gpu_rc=$?
if sm_timed_out "$gpu_rc"; then
  printf "__SM_DIAGNOSTIC_WARNING__\tGPU 基础状态探测超时，已跳过\n"
else
  printf "%s\n" "$gpu_output" | sed '/^$/d;s/^/__SM_GPU__\t/'
fi
ecc_output=$(sm_run 4s nvidia-smi -q -d ECC 2>/dev/null)
ecc_rc=$?
printf "__SM_ECC_BEGIN__\n"
[ -n "$ecc_output" ] && printf "%s\n" "$ecc_output"
printf "__SM_SECTION_END__\n"
sm_timed_out "$ecc_rc" && printf "__SM_DIAGNOSTIC_WARNING__\tECC 探测超时，已跳过\n"
topology_output=$(sm_run 4s nvidia-smi topo -m 2>/dev/null)
topology_rc=$?
printf "__SM_TOPOLOGY_BEGIN__\n"
[ -n "$topology_output" ] && printf "%s\n" "$topology_output"
printf "__SM_SECTION_END__\n"
sm_timed_out "$topology_rc" && printf "__SM_DIAGNOSTIC_WARNING__\tGPU 拓扑探测超时，已跳过\n"
nvlink_output=$(sm_run 4s nvidia-smi nvlink -s 2>/dev/null)
nvlink_rc=$?
printf "__SM_NVLINK_BEGIN__\n"
[ -n "$nvlink_output" ] && printf "%s\n" "$nvlink_output"
printf "__SM_SECTION_END__\n"
sm_timed_out "$nvlink_rc" && printf "__SM_DIAGNOSTIC_WARNING__\tNVLink 探测超时，已跳过\n"
'''
        result = self.operations.run(
            host,
            _bash(script),
            self.config.all()["collection_timeout"],
            min(self.config.all()["schedule_output_limit"], 512 * 1024),
        )
        return _parse_gpu_diagnostics(result.stdout)

    def environment_inventory(self, host: dict[str, Any], root: str) -> dict[str, Any]:
        root = _remote_path(root, "扫描目录")
        settings = self.config.all()
        timeout_seconds = _bounded_scan_value(
            settings.get("environment_inventory_timeout"), "环境盘点时限", 60, 10, 120,
        )
        stack = self.development_stack(host, timeout_seconds)
        conda_path = str(stack.get("tools", {}).get("conda", {}).get("path") or "")
        script = r'''set +e
sm_run() {
  duration=$1
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=1s "$duration" "$@"
  else
    "$@"
  fi
}
root=''' + shlex.quote(root) + r'''
conda_path=''' + shlex.quote(conda_path) + r'''
emit_conda_environment() {
  path=$1
  [ -d "$path/conda-meta" ] || return 0
  version=""
  for python_metadata in "$path"/conda-meta/python-[0-9]*.json; do
    [ -f "$python_metadata" ] || continue
    version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/Python \1/p' "$python_metadata" | head -n 1)
    break
  done
  printf "__SM_CONDA__\t%s\t%s\n" "$path" "$version"
  for package in python pytorch pytorch-cuda torch torchvision torchaudio tensorflow numpy pandas scipy cudatoolkit cuda-version; do
    for metadata in "$path"/conda-meta/"$package"-[0-9]*.json; do
      [ -f "$metadata" ] || continue
      name=$(sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$metadata" | head -n 1)
      package_version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$metadata" | head -n 1)
      [ "$name" = "$package" ] && printf "__SM_CONDA_PACKAGE__\t%s\t%s\t%s\n" "$path" "$name" "$package_version"
    done
  done
}
if [ -d "$root" ]; then
  find "$root" -xdev -maxdepth 4 -type f -name pyvenv.cfg -print0 2>/dev/null | while IFS= read -r -d "" cfg; do
    env_dir=${cfg%/pyvenv.cfg}
    [ -d "$env_dir/conda-meta" ] && continue
    version=$(sed -n 's/^version = \(.*\)$/Python \1/p' "$cfg" | head -n 1)
    printf "__SM_ENV__\t%s\t%s\n" "$env_dir" "$version"
  done
fi
conda_base=""
[ -x "$conda_path" ] && conda_base=${conda_path%/bin/conda}
for base in "$conda_base" "$HOME/miniconda3" "$HOME/miniconda" "$HOME/anaconda3" "$HOME/anaconda" "$HOME/mambaforge" "$HOME/miniforge3" "$HOME/.conda" /opt/conda /opt/miniconda3 /opt/anaconda3 /usr/local/miniconda3 /usr/local/anaconda3; do
  [ -d "$base/conda-meta" ] && emit_conda_environment "$base"
  if [ -d "$base/envs" ]; then
    find "$base/envs" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | while IFS= read -r path; do
      emit_conda_environment "$path"
    done
  fi
done
if [ -r "$HOME/.conda/environments.txt" ]; then
  while IFS= read -r path; do
    case "$path" in /*) emit_conda_environment "$path";; esac
  done < "$HOME/.conda/environments.txt"
fi
'''
        result = self.operations.run(
            host,
            _bash(script),
            timeout_seconds,
            settings["schedule_output_limit"],
        )
        environments: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        conda_by_path: dict[str, dict[str, Any]] = {}
        conda_packages_seen: set[tuple[str, str, str]] = set()
        for line in result.stdout.splitlines():
            parts = line.split("\t", 3)
            if parts[0] == "__SM_ENV__" and len(parts) == 3:
                key = ("venv", parts[1])
                if key not in seen:
                    environments.append({"backend": "venv/uv", "path": parts[1], "python": parts[2] or None})
                    seen.add(key)
            elif parts[0] == "__SM_CONDA__" and len(parts) == 3 and parts[1].startswith("/"):
                key = ("conda", parts[1])
                if key not in seen:
                    item = {"backend": "conda", "path": parts[1], "python": parts[2] or None, "packages": []}
                    environments.append(item)
                    conda_by_path[parts[1]] = item
                    seen.add(key)
            elif parts[0] == "__SM_CONDA_PACKAGE__" and len(parts) == 4:
                item = conda_by_path.get(parts[1])
                package_key = (parts[1], parts[2], parts[3])
                if item is not None and package_key not in conda_packages_seen and len(item["packages"]) < 80:
                    item["packages"].append({"name": parts[2], "version": parts[3]})
                    conda_packages_seen.add(package_key)
        return {"root": root, "items": environments, "tooling": stack}

    def directory_usage(self, host: dict[str, Any], path: str, timeout_seconds: int | None = None) -> dict[str, Any]:
        path = _remote_path(path, "统计目录")
        default_timeout = self.config.all().get("scan_timeout_seconds", 60)
        timeout_seconds = _bounded_scan_value(timeout_seconds, "扫描时限", default_timeout, 10, 120)
        script = f'''set +e
path={shlex.quote(path)}
if command -v timeout >/dev/null 2>&1; then
  output=$(timeout --signal=TERM --kill-after=2s {timeout_seconds}s du -s -x -B1 --apparent-size -- "$path" 2>/dev/null)
  scan_rc=$?
else
  output=$(du -s -x -B1 --apparent-size -- "$path" 2>/dev/null)
  scan_rc=$?
fi
printf "%s\\n" "$output"
printf "__SM_SCAN_STATUS__\\t%s\\n" "$scan_rc"
'''
        result = self.operations.run(
            host,
            _bash(script),
            timeout_seconds + 8,
            64 * 1024,
        )
        status = result.exit_code
        output_lines: list[str] = []
        for line in result.stdout.splitlines():
            if line.startswith("__SM_SCAN_STATUS__\t"):
                try:
                    status = int(line.split("\t", 1)[1])
                except ValueError:
                    status = 1
            elif line.strip():
                output_lines.append(line)
        first = output_lines[-1].strip().split(None, 1) if output_lines else []
        timed_out = status in {124, 137}
        if not first or not first[0].isdigit():
            if timed_out:
                return {
                    "path": path,
                    "bytes": None,
                    "partial": True,
                    "timed_out": True,
                    "timeout_seconds": timeout_seconds,
                    "warning": f"目录在 {timeout_seconds} 秒内未统计完成，请缩小扫描目录或提高扫描时限",
                }
            raise OperationError(redact(result.stderr) or "目录不存在、无权读取或远端未返回有效容量")
        partial = status != 0 or result.stdout_truncated
        warning = None
        if timed_out:
            warning = f"扫描达到 {timeout_seconds} 秒时限，容量为已完成部分"
        elif partial:
            warning = "部分子目录无权读取或输出被截断，容量可能不完整"
        return {
            "path": path,
            "bytes": int(first[0]),
            "partial": partial,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "warning": warning,
        }

    def large_files(
        self,
        host: dict[str, Any],
        path: str,
        minimum_bytes: int | None = None,
        limit: int | None = None,
        max_depth: int | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        path = _remote_path(path, "扫描目录")
        settings = self.config.all()
        minimum_bytes = _bounded_scan_value(
            minimum_bytes, "最小文件大小", settings.get("scan_minimum_mib", 1024) * 1024 * 1024,
            1024 * 1024, 10 * 1024 * 1024 * 1024,
        )
        limit = _bounded_scan_value(limit, "结果数", settings.get("scan_result_limit", 100), 1, 200)
        max_depth = _bounded_scan_value(max_depth, "扫描深度", settings.get("scan_max_depth", 8), 1, 12)
        timeout_seconds = _bounded_scan_value(
            timeout_seconds, "扫描时限", settings.get("scan_timeout_seconds", 60), 10, 120,
        )
        script = f'''set +e
if command -v timeout >/dev/null 2>&1; then
  timeout --signal=TERM --kill-after=2s {timeout_seconds}s find {shlex.quote(path)} -xdev -maxdepth {max_depth} -type f -size +{minimum_bytes - 1}c -printf '%s\\t%T@\\t%p\\0' 2>/dev/null
  scan_rc=$?
else
  find {shlex.quote(path)} -xdev -maxdepth {max_depth} -type f -size +{minimum_bytes - 1}c -printf '%s\\t%T@\\t%p\\0' 2>/dev/null
  scan_rc=$?
fi
printf '\\0__SM_SCAN_STATUS__\\t%s\\0' "$scan_rc"
'''
        result = self.operations.run(
            host,
            _bash(script),
            timeout_seconds + 8,
            min(self.config.all()["schedule_output_limit"], 2 * 1024 * 1024),
        )
        items: list[dict[str, Any]] = []
        status = result.exit_code
        for record in result.stdout.split("\x00"):
            parts = record.split("\t", 2)
            if record.startswith("__SM_SCAN_STATUS__\t"):
                try:
                    status = int(record.split("\t", 1)[1])
                except ValueError:
                    status = 1
            elif len(parts) == 3 and parts[0].isdigit():
                items.append({"bytes": int(parts[0]), "mtime": parts[1], "path": parts[2]})
        items.sort(key=lambda item: item["bytes"], reverse=True)
        timed_out = status in {124, 137}
        partial = status != 0 or result.stdout_truncated
        warning = None
        if timed_out:
            warning = f"扫描达到 {timeout_seconds} 秒时限，以下为已发现的部分结果"
        elif partial:
            warning = "部分目录无权读取或输出达到上限，以下结果可能不完整"
        return {
            "path": path,
            "minimum_bytes": minimum_bytes,
            "items": items[:limit],
            "max_depth": max_depth,
            "timeout_seconds": timeout_seconds,
            "partial": partial,
            "timed_out": timed_out,
            "warning": warning,
            "truncated": len(items) > limit or result.stdout_truncated or timed_out,
        }

    def apt_packages(self, host: dict[str, Any], search: str = "") -> list[dict[str, str]]:
        search = str(search or "").strip()
        if len(search) > 80 or not re.fullmatch(r"[A-Za-z0-9.+:~_-]*", search):
            raise OperationError("APT 搜索条件无效")
        filter_command = f" | grep -i -- {shlex.quote(search)}" if search else ""
        command = (
            "dpkg-query -W -f='${binary:Package}\\t${Version}\\t${Status}\\n' 2>/dev/null"
            + filter_command
            + " | head -n 200"
        )
        result = self.operations.run(host, command, self.config.all()["collection_timeout"], 256 * 1024)
        if result.exit_code not in {0, 1}:
            raise OperationError(redact(result.stderr) or "读取 APT 包列表失败")
        items = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                items.append({"package": parts[0], "version": parts[1], "status": parts[2]})
        return items

    def environment_plan(self, host: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        backend = str(payload.get("backend", ""))
        action = str(payload.get("action", "create"))
        if backend not in {"venv", "conda", "uv"} or action not in {"create", "remove", "install"}:
            raise OperationError("虚拟环境后端或操作无效")
        target = _remote_path(payload.get("path"), "虚拟环境路径")
        if target in {"/", "/home", "/root", "/usr", "/opt", "/tmp"} or len(target.strip("/").split("/")) < 2:
            raise OperationError("虚拟环境路径过于宽泛，至少需要两级目录")
        stack = self.development_stack(host)
        tools = stack["tools"]
        conda_path = str(tools.get("conda", {}).get("path") or "")
        uv_path = str(tools.get("uv", {}).get("path") or "")
        if backend == "conda" and not conda_path:
            raise OperationError("目标主机未安装 conda，请先生成 Miniconda 安装方案")
        if backend == "uv" and not uv_path:
            raise OperationError("目标主机未安装 uv，请先生成 uv 安装方案")
        if backend == "venv" and not stack["python_versions"]:
            raise OperationError("目标主机未检测到可用 Python 3")
        if action in {"remove", "install"} and payload.get("confirmed_path") is not True:
            raise OperationError("删除或依赖操作需要再次确认目标路径")
        packages = self._packages(payload.get("packages", []))
        preset = str(payload.get("pytorch", "none"))
        if preset not in _PYTORCH_PRESETS:
            raise OperationError("PyTorch 兼容预设无效")
        python_selector = str(payload.get("python", ""))
        python_command = ""
        if backend in {"venv", "uv"}:
            if not _PYTHON_SELECTOR.fullmatch(python_selector):
                raise OperationError("Python 版本必须选择检测到的 3.x 版本")
            available = {item["command"]: item for item in stack["python_versions"]}
            candidate = python_selector if python_selector.startswith("python") else f"python{python_selector}"
            if candidate not in available:
                raise OperationError("目标主机未检测到所选 Python 版本")
            python_command = available[candidate]["path"]
        elif python_selector and not re.fullmatch(r"3\.(8|9|10|11|12|13)", python_selector):
            raise OperationError("conda Python 版本无效")
        package_args = [*packages, *_PYTORCH_PRESETS[preset]]
        package_text = " ".join(shlex.quote(item) for item in package_args)
        conda_binary = shlex.quote(conda_path) if conda_path else ""
        uv_binary = shlex.quote(uv_path) if uv_path else ""
        lines = ["#!/usr/bin/env bash", "set -Eeuo pipefail", f"target={shlex.quote(target)}"]
        if action == "create":
            if backend == "venv":
                lines.append(f"{shlex.quote(python_command)} -m venv \"$target\"")
            elif backend == "uv":
                lines.append(f"{uv_binary} venv --python {shlex.quote(python_selector)} \"$target\"")
            else:
                lines.append(f"{conda_binary} create -y -p \"$target\" python={shlex.quote(python_selector or '3')}")
        elif action == "remove":
            if backend == "conda":
                lines.append(f"{conda_binary} env remove -y -p \"$target\"")
            else:
                lines.extend([
                    "test -f \"$target/pyvenv.cfg\"",
                    "case \"$target\" in /|/home|/root|/usr|/opt|/tmp) echo 'refusing broad path' >&2; exit 2;; esac",
                    "rm -rf -- \"$target\"",
                ])
        if package_text and action in {"create", "install"}:
            if backend == "conda":
                lines.append(f"{conda_binary} run -p \"$target\" python -m pip install {package_text}")
            elif backend == "uv":
                lines.append(f"{uv_binary} pip install --python \"$target/bin/python\" {package_text}")
            else:
                lines.append(f"\"$target/bin/python\" -m pip install {package_text}")
        lines.append("echo '环境方案执行完成；请重新盘点确认版本。'")
        return {
            "kind": "environment",
            "backend": backend,
            "action": action,
            "path": target,
            "script": "\n".join(lines) + "\n",
            "stack": stack,
            "remote_execution": action != "remove",
        }

    @staticmethod
    def _packages(value: Any) -> list[str]:
        raw = value if isinstance(value, list) else str(value or "").replace(",", " ").split()
        if not isinstance(raw, list) or len(raw) > 30:
            raise OperationError("依赖包数量无效")
        packages = [str(item).strip() for item in raw if str(item).strip()]
        if any(not _PACKAGE_PATTERN.fullmatch(item) or item.startswith("-") for item in packages):
            raise OperationError("依赖包名称或版本约束无效")
        return packages

    def execute_environment_plan(self, host: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        plan = self.environment_plan(host, payload)
        if not plan["remote_execution"]:
            raise OperationError("删除环境仅允许生成脚本并由管理员人工执行")
        result = self.operations.run(
            host,
            _bash(plan["script"]),
            self.config.all()["install_timeout"],
            self.config.all()["schedule_output_limit"],
        )
        return {
            "ok": result.exit_code == 0,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": redact(result.stderr),
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "plan": plan,
        }

    def export_conda_environment(self, host: dict[str, Any], path: str) -> str:
        path = _remote_path(path, "conda 环境路径")
        conda_path = str(self.development_stack(host).get("tools", {}).get("conda", {}).get("path") or "")
        if not conda_path:
            raise OperationError("目标主机未安装 conda")
        result = self.operations.run(
            host,
            f"{shlex.quote(conda_path)} env export -p {shlex.quote(path)} --no-builds",
            self.config.all()["collection_timeout"],
            self.config.all()["schedule_output_limit"],
        )
        if result.exit_code != 0:
            raise OperationError(redact(result.stderr) or "导出 conda 环境失败")
        if not result.stdout.strip():
            raise OperationError("conda 未返回环境 YAML")
        return result.stdout

    def conda_yaml_plan(self, host: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        target = _remote_path(payload.get("path"), "conda 环境路径")
        if target in {"/", "/home", "/root", "/usr", "/opt", "/tmp"} or len(target.strip("/").split("/")) < 2:
            raise OperationError("conda 环境路径过于宽泛")
        yaml_text = payload.get("yaml")
        if not isinstance(yaml_text, str) or not yaml_text.strip() or len(yaml_text.encode("utf-8")) > 512 * 1024 or "\x00" in yaml_text:
            raise OperationError("环境 YAML 必须是非空 UTF-8 文本且不超过 512 KiB")
        stack = self.development_stack(host)
        conda_path = str(stack["tools"].get("conda", {}).get("path") or "")
        if not conda_path:
            raise OperationError("目标主机未安装 conda")
        encoded = base64.b64encode(yaml_text.encode("utf-8")).decode("ascii")
        lines = [
            "#!/usr/bin/env bash",
            "set -Eeuo pipefail",
            f"target={shlex.quote(target)}",
            "spec=$(mktemp)",
            "trap 'rm -f \"$spec\"' EXIT",
            f"printf %s {shlex.quote(encoded)} | base64 -d > \"$spec\"",
            f"{shlex.quote(conda_path)} env create -p \"$target\" -f \"$spec\"",
            "echo 'conda YAML 环境重建完成。'",
        ]
        return {"kind": "conda-yaml", "path": target, "script": "\n".join(lines) + "\n", "remote_execution": True}

    def execute_conda_yaml_plan(self, host: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        plan = self.conda_yaml_plan(host, payload)
        result = self.operations.run(
            host,
            _bash(plan["script"]),
            self.config.all()["install_timeout"],
            self.config.all()["schedule_output_limit"],
        )
        return {
            "ok": result.exit_code == 0,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": redact(result.stderr),
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "plan": plan,
        }

    def system_plan(self, host: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", ""))
        stack = self.development_stack(host)
        if stack["os"]["id"] not in {"ubuntu", "debian"}:
            raise OperationError("当前仅为 Debian/Ubuntu 生成 APT 软件栈方案")
        lines = ["#!/usr/bin/env bash", "set -Eeuo pipefail", "export DEBIAN_FRONTEND=noninteractive"]
        title = ""
        if kind == "gpu-driver":
            package = str(payload.get("package") or stack["gpu"].get("recommended_driver") or "")
            if not re.fullmatch(r"nvidia-driver-[0-9]+(?:-(?:server|open))?", package):
                raise OperationError("GPU 驱动包必须来自推荐项或受限版本格式")
            title = f"安装推荐 NVIDIA 驱动 {package}"
            lines.extend([
                "sudo apt-get update",
                f"sudo apt-get install -y -- {shlex.quote(package)}",
                "echo '驱动安装完成；通常需要人工重启后再做 GPU 自检。'",
            ])
        elif kind == "cuda":
            version = str(payload.get("version", ""))
            package = {"11.8": "cuda-toolkit-11-8", "12.1": "cuda-toolkit-12-1", "12.4": "cuda-toolkit-12-4"}.get(version)
            if not package:
                raise OperationError("CUDA 版本必须从受支持选项中选择")
            title = f"安装 CUDA Toolkit {version}"
            lines.extend(["sudo apt-get update", f"sudo apt-get install -y -- {package}", "echo '请确认 NVIDIA 官方仓库已配置；安装后重新检测 nvcc。'"])
        elif kind == "cudnn":
            version = str(payload.get("version", ""))
            package = {"8": "libcudnn8", "9-cuda12": "libcudnn9-cuda-12"}.get(version)
            if not package:
                raise OperationError("cuDNN 版本必须从受支持选项中选择")
            title = f"安装 cuDNN {version}"
            lines.extend(["sudo apt-get update", f"sudo apt-get install -y -- {package}", "echo '请确认 cuDNN 软件源已配置；安装后重新盘点。'"])
        elif kind == "uv-install":
            title = "安装 uv"
            lines.extend(["python3 -m pip install --user --upgrade uv", "echo '请重新登录 Shell 或将 ~/.local/bin 加入 PATH。'"])
        elif kind == "conda-install":
            title = "安装 Miniconda"
            lines.extend([
                "installer=$(mktemp)",
                "trap 'rm -f \"$installer\"' EXIT",
                "curl -fL --proto '=https' --tlsv1.2 -o \"$installer\" https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh",
                "bash \"$installer\" -b -p \"$HOME/miniconda3\"",
                "\"$HOME/miniconda3/bin/conda\" init bash",
            ])
        elif kind == "apt":
            action = str(payload.get("action", ""))
            if action == "update":
                command = "sudo apt-get update"
            elif action == "upgrade":
                command = "sudo apt-get upgrade -y"
            elif action == "autofix":
                command = "sudo dpkg --configure -a && sudo apt-get -f install -y"
            elif action in {"install", "remove", "purge"}:
                package = str(payload.get("package", ""))
                if not _APT_PACKAGE_PATTERN.fullmatch(package):
                    raise OperationError("APT 包名无效")
                command = f"sudo apt-get {action} -y -- {shlex.quote(package)}"
            else:
                raise OperationError("APT 操作无效")
            title = f"APT {action}"
            lines.extend([command, "echo 'APT 方案执行完成；请刷新包列表确认状态。'"])
        else:
            raise OperationError("系统方案类型无效")
        return {
            "kind": kind,
            "title": title,
            "script": "\n".join(lines) + "\n",
            "remote_execution": False,
            "stack": stack,
        }

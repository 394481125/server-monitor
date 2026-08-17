from __future__ import annotations

import base64
import json
import re
import shlex
from typing import Any

from .operations import OperationError
from .security import redact


RESULT_MARKER = "__SERVER_MONITOR_GPU_BENCHMARK__"
_PYTHON_COMMAND = re.compile(r"^(?:python3|python3\.(?:8|9|10|11|12|13))$")
_PYTHON_PATH = re.compile(r"^/[A-Za-z0-9._+/@-]+(?:/[A-Za-z0-9._+@-]+)*/python(?:3(?:\.(?:8|9|10|11|12|13))?)?$")


GPU_BENCHMARK_SCRIPT = r'''from __future__ import annotations

import argparse
import json
import math
import subprocess
import time

MARKER = "__SERVER_MONITOR_GPU_BENCHMARK__"


def rounded(value, digits=3):
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def telemetry():
    try:
        output = subprocess.check_output([
            "nvidia-smi",
            "--query-gpu=index,temperature.gpu,power.draw,clocks.sm,clocks.mem,utilization.gpu,memory.used",
            "--format=csv,noheader,nounits",
        ], text=True, timeout=5, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    rows = []
    for line in output.splitlines():
        values = [item.strip() for item in line.split(",")]
        if len(values) == 7:
            rows.append({
                "index": values[0], "temperature_c": values[1], "power_w": values[2],
                "sm_clock_mhz": values[3], "memory_clock_mhz": values[4],
                "utilization_percent": values[5], "memory_used_mib": values[6],
            })
    return rows


def synchronize(torch, devices):
    for device in devices:
        torch.cuda.synchronize(device)


def bf16_supported(torch, devices):
    checker = getattr(torch.cuda, "is_bf16_supported", None)
    if checker is not None:
        try:
            for device in devices:
                with torch.cuda.device(device):
                    if not checker():
                        return False
            return True
        except (AttributeError, RuntimeError, TypeError):
            pass
    return all(torch.cuda.get_device_capability(device)[0] >= 8 for device in devices)


def matrix_inputs(torch, size, device, dtype, integer=False):
    if integer:
        return (
            torch.randint(-127, 127, (size, size), device=device, dtype=torch.int8),
            torch.randint(-127, 127, (size, size), device=device, dtype=torch.int8),
        )
    if str(dtype).startswith("torch.float8"):
        return (
            torch.randn((size, size), device=device, dtype=torch.float16).to(dtype),
            torch.randn((size, size), device=device, dtype=torch.float16).to(dtype),
        )
    return (
        torch.randn((size, size), device=device, dtype=dtype),
        torch.randn((size, size), device=device, dtype=dtype),
    )


def gemm(torch, device, dtype, target_seconds, allow_tf32=False, integer=False):
    torch.backends.cuda.matmul.allow_tf32 = allow_tf32
    total_memory = torch.cuda.get_device_properties(device).total_memory
    size = 4096 if total_memory >= 6 * 1024 ** 3 else 2048
    operation = torch._int_mm if integer else torch.mm
    with torch.cuda.device(device):
        left, right = matrix_inputs(torch, size, device, dtype, integer)
        for _ in range(2):
            operation(left, right)
        torch.cuda.synchronize(device)
        probe_start = torch.cuda.Event(enable_timing=True)
        probe_end = torch.cuda.Event(enable_timing=True)
        probe_start.record()
        operation(left, right)
        probe_end.record()
        torch.cuda.synchronize(device)
        one_seconds = max(probe_start.elapsed_time(probe_end) / 1000, 0.0001)
        iterations = max(3, min(30, int(target_seconds / one_seconds)))
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        start.record()
        for _ in range(iterations):
            operation(left, right)
        end.record()
        torch.cuda.synchronize(device)
        elapsed = max(start.elapsed_time(end) / 1000, 0.000001)
        tflops = 2 * (size ** 3) * iterations / elapsed / 1e12
        del left, right
        return {"device": int(device), "matrix_size": size, "iterations": iterations, "tops": rounded(tflops, 2)}


def memory_bandwidth(torch, device):
    total_memory = torch.cuda.get_device_properties(device).total_memory
    size_bytes = int(min(256 * 1024 ** 2, max(32 * 1024 ** 2, total_memory // 32)))
    elements = size_bytes // 4
    with torch.cuda.device(device):
        source = torch.empty(elements, device=device, dtype=torch.float32)
        target = torch.empty_like(source)
        for _ in range(3):
            target.copy_(source)
        torch.cuda.synchronize(device)
        iterations = 20
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        start.record()
        for _ in range(iterations):
            target.copy_(source)
        end.record()
        torch.cuda.synchronize(device)
        elapsed = max(start.elapsed_time(end) / 1000, 0.000001)
        gbps = (size_bytes * 2 * iterations) / elapsed / 1e9
        del source, target
        return {"device": int(device), "bytes": size_bytes, "gbps": rounded(gbps, 2)}


def training_model(torch, model_name):
    if model_name == "vit_tiny_patch16_224":
        try:
            import timm
        except ImportError as exc:
            raise RuntimeError("vit_tiny_patch16_224 需要安装 timm") from exc
        return timm.create_model(model_name, pretrained=False, num_classes=10), 16
    try:
        from torchvision import models
    except ImportError as exc:
        raise RuntimeError("ResNet/MobileNet 训练测速需要安装 torchvision") from exc
    factories = {
        "resnet18": (models.resnet18, 32),
        "resnet34": (models.resnet34, 24),
        "resnet50": (models.resnet50, 16),
        "mobilenet_v3_small": (models.mobilenet_v3_small, 64),
    }
    factory, batch_per_gpu = factories[model_name]
    return factory(weights=None, num_classes=10), batch_per_gpu


def training_batches(torch, dataset_name, batch_size, iterations, primary, download_dataset):
    if dataset_name == "synthetic":
        inputs = torch.randn(batch_size, 3, 224, 224, device=primary)
        labels = torch.randint(0, 10, (batch_size,), device=primary)
        while True:
            yield inputs, labels
    try:
        from torchvision import datasets, transforms
    except ImportError as exc:
        raise RuntimeError("CIFAR 风格数据集需要安装 torchvision") from exc
    transform = transforms.Compose([transforms.Resize((224, 224)), transforms.ToTensor()])
    sample_count = max(batch_size * (iterations + 4), 512)
    if dataset_name == "fake_cifar10":
        dataset = datasets.FakeData(size=sample_count, image_size=(3, 32, 32), num_classes=10, transform=transform)
    else:
        try:
            dataset = datasets.CIFAR10(
                root="~/.cache/server-monitor/datasets", train=True,
                download=download_dataset, transform=transform,
            )
        except RuntimeError as exc:
            raise RuntimeError("CIFAR-10 未缓存；请勾选允许首次下载，或先在远端缓存数据集") from exc
    loader = torch.utils.data.DataLoader(
        dataset, batch_size=batch_size, shuffle=True, num_workers=2, pin_memory=True,
        persistent_workers=True, drop_last=True,
    )
    while True:
        for inputs, labels in loader:
            yield inputs.to(primary, non_blocking=True), labels.to(primary, non_blocking=True)


def cnn_training(torch, devices, duration, model_name, dataset_name, download_dataset):
    nn = torch.nn
    primary = devices[0]
    model, batch_per_gpu = training_model(torch, model_name)
    model = model.to(primary)
    if len(devices) > 1:
        model = nn.DataParallel(model, device_ids=devices)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
    criterion = nn.CrossEntropyLoss()
    batch_size = batch_per_gpu * len(devices)
    iterations = max(4, min(30, int(duration * 2)))
    batches = training_batches(torch, dataset_name, batch_size, iterations, primary, download_dataset)
    losses = []
    accuracies = []
    started = time.perf_counter()
    for step in range(2 + iterations):
        inputs, labels = next(batches)
        optimizer.zero_grad(set_to_none=True)
        outputs = model(inputs)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()
        if step == 1:
            synchronize(torch, devices)
            started = time.perf_counter()
        if step >= 2:
            losses.append(loss.detach())
            accuracies.append((outputs.argmax(dim=1) == labels).float().mean())
    synchronize(torch, devices)
    elapsed = max(time.perf_counter() - started, 0.000001)
    avg_loss = float(torch.stack(losses).mean().item())
    avg_accuracy = float(torch.stack(accuracies).mean().item())
    return {
        "available": True, "model": model_name, "dataset": dataset_name,
        "iterations": iterations, "batch_size": batch_size,
        "it_per_sec": rounded(iterations / elapsed, 2),
        "images_per_sec": rounded(iterations * batch_size / elapsed, 2),
        "avg_loss": rounded(avg_loss, 4),
        "avg_accuracy": rounded(avg_accuracy, 4),
        "accuracy_note": (
            "短时快速训练结果，仅用于横向评估训练链路；需要完整训练计划才能评价模型精度"
            if dataset_name == "cifar10" else
            "随机数据仅用于验证训练链路和数值稳定性，不代表模型精度"
        ),
    }


def nccl_all_reduce(torch, devices):
    if len(devices) < 2:
        return None
    try:
        tensors = [torch.ones(16 * 1024 * 1024, device=device, dtype=torch.float32) for device in devices]
        if not torch.cuda.nccl.is_available(tensors):
            return {"available": False, "reason": "当前 PyTorch/CUDA 未提供 NCCL"}
        for _ in range(3):
            torch.cuda.nccl.all_reduce(tensors)
        synchronize(torch, devices)
        iterations = 10
        started = time.perf_counter()
        for _ in range(iterations):
            torch.cuda.nccl.all_reduce(tensors)
        synchronize(torch, devices)
        elapsed = max(time.perf_counter() - started, 0.000001)
        payload_bytes = tensors[0].numel() * tensors[0].element_size()
        algorithm_gbps = payload_bytes * iterations / elapsed / 1e9
        bus_gbps = algorithm_gbps * 2 * (len(devices) - 1) / len(devices)
        return {
            "available": True, "payload_bytes": payload_bytes, "iterations": iterations,
            "algorithm_gbps": rounded(algorithm_gbps, 2), "bus_gbps": rounded(bus_gbps, 2),
        }
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:300]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("single", "multi"), required=True)
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--model", choices=("resnet18", "resnet34", "resnet50", "mobilenet_v3_small", "vit_tiny_patch16_224"), required=True)
    parser.add_argument("--dataset", choices=("synthetic", "fake_cifar10", "cifar10"), required=True)
    parser.add_argument("--download-dataset", choices=("0", "1"), required=True)
    args = parser.parse_args()
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("PyTorch 未检测到可用 CUDA GPU")
    available = torch.cuda.device_count()
    if args.mode == "multi" and available < 2:
        raise RuntimeError("多卡评估至少需要 2 张可用 CUDA GPU")
    devices = list(range(available if args.mode == "multi" else 1))[:8]
    before = telemetry()
    properties = []
    for device in devices:
        prop = torch.cuda.get_device_properties(device)
        properties.append({
            "index": device, "name": prop.name, "total_memory_bytes": prop.total_memory,
            "compute_capability": f"{prop.major}.{prop.minor}", "multi_processor_count": prop.multi_processor_count,
        })
    specs = [("fp32", torch.float32, False, False)]
    if all(torch.cuda.get_device_capability(device)[0] >= 8 for device in devices):
        specs.append(("tf32", torch.float32, True, False))
    specs.append(("fp16", torch.float16, False, False))
    if bf16_supported(torch, devices):
        specs.append(("bf16", torch.bfloat16, False, False))
    if hasattr(torch, "float8_e4m3fn"):
        specs.append(("fp8_e4m3", torch.float8_e4m3fn, False, False))
    if hasattr(torch, "float8_e5m2"):
        specs.append(("fp8_e5m2", torch.float8_e5m2, False, False))
    if hasattr(torch, "_int_mm"):
        specs.append(("int8", torch.int8, False, True))
    target_seconds = max(0.2, args.duration / max(4, len(specs) * len(devices) + 3))
    matrix = []
    warnings = []
    for label, dtype, allow_tf32, integer in specs:
        per_gpu = []
        for device in devices:
            try:
                per_gpu.append(gemm(torch, device, dtype, target_seconds, allow_tf32, integer))
            except Exception as exc:
                warnings.append(f"GPU {device} {label} GEMM 不可用: {str(exc)[:200]}")
        if per_gpu:
            matrix.append({
                "precision": label, "unit": "TOPS" if integer else "TFLOPS", "per_gpu": per_gpu,
                "aggregate": rounded(sum(item["tops"] for item in per_gpu), 2),
            })
    memory = []
    for device in devices:
        try:
            memory.append(memory_bandwidth(torch, device))
        except Exception as exc:
            warnings.append(f"GPU {device} 显存带宽测试失败: {str(exc)[:200]}")
    try:
        training = cnn_training(
            torch, devices, args.duration, args.model, args.dataset,
            args.download_dataset == "1",
        )
    except Exception as exc:
        training_error = str(exc)[:300]
        warnings.append(f"GPU 训练测速失败: {training_error}")
        training = {
            "available": False,
            "model": args.model,
            "dataset": args.dataset,
            "error": training_error,
            "accuracy_note": "训练测速未完成；矩阵、显存和通信结果仍可用于硬件评估",
        }
    result = {
        "ok": True, "schema_version": 1, "mode": args.mode, "requested_duration_seconds": args.duration,
        "gpu_count": len(devices), "devices": properties, "matrix": matrix, "memory_bandwidth": memory,
        "training": training,
        "collective": nccl_all_reduce(torch, devices),
        "parallelism": {"tp_degree": len(devices), "tp8_ready": len(devices) >= 8},
        "telemetry_before": before, "telemetry_after": telemetry(),
        "software": {"torch": torch.__version__, "cuda": torch.version.cuda, "cudnn": torch.backends.cudnn.version()},
        "warnings": warnings,
    }
    print(MARKER + json.dumps(result, ensure_ascii=False, separators=(",", ":")))


try:
    main()
except Exception as exc:
    print(MARKER + json.dumps({"ok": False, "error": str(exc)[:500]}, ensure_ascii=False, separators=(",", ":")))
'''


BENCHMARK_MODELS = frozenset({"resnet18", "resnet34", "resnet50", "mobilenet_v3_small", "vit_tiny_patch16_224"})
BENCHMARK_DATASETS = frozenset({"synthetic", "fake_cifar10", "cifar10"})


def validate_benchmark_request(payload: dict[str, Any]) -> tuple[str, int, str, str, str, bool]:
    mode = str(payload.get("mode", "single")).strip().lower()
    if mode not in {"single", "multi"}:
        raise OperationError("GPU 评估模式必须是 single 或 multi")
    try:
        duration = int(payload.get("duration_seconds", 5))
    except (TypeError, ValueError) as exc:
        raise OperationError("GPU 评估时长必须是整数") from exc
    if not 3 <= duration <= 30:
        raise OperationError("GPU 评估时长必须在 3 到 30 秒之间")
    python_command = str(payload.get("python", "python3")).strip()
    if not (_PYTHON_COMMAND.fullmatch(python_command) or _PYTHON_PATH.fullmatch(python_command)):
        raise OperationError("Python 命令无效，仅允许 python3 或绝对环境解释器路径")
    model = str(payload.get("model", "resnet18")).strip()
    if model not in BENCHMARK_MODELS:
        raise OperationError("GPU 训练测速模型无效")
    dataset = str(payload.get("dataset", "synthetic")).strip()
    if dataset not in BENCHMARK_DATASETS:
        raise OperationError("GPU 训练测速数据集无效")
    download_dataset = payload.get("download_dataset", False)
    if not isinstance(download_dataset, bool):
        raise OperationError("download_dataset 必须是布尔值")
    if download_dataset and dataset != "cifar10":
        raise OperationError("仅真实 CIFAR-10 支持首次下载")
    return mode, duration, python_command, model, dataset, download_dataset


def build_benchmark_command(
    mode: str,
    duration: int,
    python_command: str,
    model: str,
    dataset: str,
    download_dataset: bool,
) -> str:
    encoded = base64.b64encode(GPU_BENCHMARK_SCRIPT.encode("utf-8")).decode("ascii")
    return (
        "umask 077; benchmark_file=$(mktemp /tmp/server-monitor-gpu-benchmark.XXXXXX.py) || exit 1; "
        "trap 'rm -f \"$benchmark_file\"' EXIT HUP INT TERM; "
        f"printf %s {shlex.quote(encoded)} | base64 -d > \"$benchmark_file\"; "
        f"{shlex.quote(python_command)} \"$benchmark_file\" --mode {shlex.quote(mode)} --duration {duration} "
        f"--model {shlex.quote(model)} --dataset {shlex.quote(dataset)} --download-dataset {int(download_dataset)}"
    )


def parse_benchmark_output(output: str) -> dict[str, Any]:
    lines = [line[len(RESULT_MARKER):] for line in output.splitlines() if line.startswith(RESULT_MARKER)]
    if not lines:
        raise OperationError("远端 GPU 评估未返回结构化结果")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise OperationError("远端 GPU 评估结果格式无效") from exc
    if not isinstance(result, dict):
        raise OperationError("远端 GPU 评估结果格式无效")
    if result.get("ok") is not True:
        raise OperationError(str(result.get("error") or "GPU 评估失败"))
    required = {"mode", "gpu_count", "devices", "matrix", "memory_bandwidth", "training"}
    if not required.issubset(result):
        raise OperationError("远端 GPU 评估结果字段不完整")
    return result


def run_gpu_benchmark(operations: Any, config: Any, host: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
    mode, duration, python_command, model, dataset, download_dataset = validate_benchmark_request(payload)
    settings = config.all()
    result = operations.run(
        host,
        build_benchmark_command(mode, duration, python_command, model, dataset, download_dataset),
        int(settings.get("gpu_benchmark_timeout", 180)),
        min(int(settings.get("schedule_output_limit", 1024 * 1024)), 2 * 1024 * 1024),
    )
    if result.exit_code != 0 and RESULT_MARKER not in result.stdout:
        raise OperationError(redact(result.stderr) or f"GPU 评估命令退出码 {result.exit_code}")
    return parse_benchmark_output(result.stdout), python_command, duration

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.app import create_app
from monitor.gpu_benchmark import validate_benchmark_request


def simulated_benchmark(payload: dict) -> tuple[dict, str, int]:
    mode, duration, python_command, model, dataset, download_dataset = validate_benchmark_request(payload)
    expected = ("multi", 3, "python3", "resnet50", "cifar10", True)
    if (mode, duration, python_command, model, dataset, download_dataset) != expected:
        raise RuntimeError("浏览器提交的 GPU 评估参数与验收场景不一致")
    per_gpu = lambda value: [
        {"device": index, "matrix_size": 4096, "iterations": 10, "tops": value + index}
        for index in range(8)
    ]
    result = {
        "ok": True,
        "schema_version": 1,
        "mode": mode,
        "requested_duration_seconds": duration,
        "gpu_count": 8,
        "devices": [
            {
                "index": index,
                "name": "NVIDIA H100 PCIe",
                "total_memory_bytes": 80 * 1024**3,
                "compute_capability": "9.0",
                "multi_processor_count": 114,
            }
            for index in range(8)
        ],
        "matrix": [
            {"precision": "fp32", "unit": "TFLOPS", "aggregate": 528.0, "per_gpu": per_gpu(62.5)},
            {"precision": "tf32", "unit": "TFLOPS", "aggregate": 2080.0, "per_gpu": per_gpu(256.5)},
            {"precision": "fp16", "unit": "TFLOPS", "aggregate": 4160.0, "per_gpu": per_gpu(516.5)},
            {"precision": "bf16", "unit": "TFLOPS", "aggregate": 4080.0, "per_gpu": per_gpu(506.5)},
            {"precision": "fp8_e4m3", "unit": "TFLOPS", "aggregate": 7920.0, "per_gpu": per_gpu(986.5)},
            {"precision": "fp8_e5m2", "unit": "TFLOPS", "aggregate": 7760.0, "per_gpu": per_gpu(966.5)},
            {"precision": "int8", "unit": "TOPS", "aggregate": 8040.0, "per_gpu": per_gpu(1001.5)},
        ],
        "memory_bandwidth": [
            {"device": index, "bytes": 256 * 1024**2, "gbps": 1950.0 + index}
            for index in range(8)
        ],
        "training": {
            "model": model,
            "dataset": dataset,
            "iterations": 6,
            "batch_size": 128,
            "it_per_sec": 42.5,
            "images_per_sec": 5440.0,
            "avg_loss": 1.8421,
            "avg_accuracy": 0.4375,
            "accuracy_note": "短时快速训练结果，仅用于横向评估训练链路；需要完整训练计划才能评价模型精度",
        },
        "collective": {
            "available": True,
            "payload_bytes": 64 * 1024**2,
            "iterations": 10,
            "algorithm_gbps": 210.0,
            "bus_gbps": 367.5,
        },
        "parallelism": {"tp_degree": 8, "tp8_ready": True},
        "telemetry_before": [],
        "telemetry_after": [],
        "software": {"torch": "2.7.0", "cuda": "12.8", "cudnn": 91000},
        "warnings": [],
    }
    return result, python_command, duration


def main() -> int:
    initial_password = "BrowserPass123"
    changed_password = "BrowserPass456"
    with tempfile.TemporaryDirectory(prefix="server-monitor-e2e-", dir="/tmp") as temporary:
        data_dir = Path(temporary)
        app = create_app(
            {
                "TESTING": False,
                "DATA_DIR": str(data_dir),
                "DATABASE": str(data_dir / "server-monitor.sqlite3"),
                "MASTER_KEY": str(data_dir / "master.key"),
                "INITIAL_ADMIN_PASSWORD": initial_password,
                "START_BACKGROUND": False,
                "ACQUIRE_PROCESS_LOCK": False,
            }
        )
        host = app.extensions["hosts"].create(
            {
                "name": "e2e-gpu-node",
                "address": "192.0.2.80",
                "username": "monitor",
                "auth_type": "password",
                "auth_secret": "not-used-by-e2e",
                "allow_install": False,
                "allow_stress": True,
                "docker_enabled": False,
            },
            fingerprint="SHA256:e2e-gpu-node",
            machine_id="e2e-gpu-node",
        )
        submitted_payloads: list[dict] = []

        def benchmark(_host: dict, payload: dict) -> tuple[dict, str, int]:
            submitted_payloads.append(dict(payload))
            return simulated_benchmark(payload)

        app.extensions["development"].development_stack = lambda _host: {
            "os": {"id": "ubuntu", "version": "24.04"},
            "gpu": {"driver_version": "570.86", "recommended_driver": "nvidia-driver-570"},
            "cuda": {"nvcc_version": "12.8", "cudnn_packages": [], "cudnn_libraries": []},
            "python_versions": [{"command": "python3", "path": "/usr/bin/python3", "version": "3.12.3"}],
            "tools": {"conda": {"available": False}, "uv": {"available": True, "version": "0.7"}},
            "warnings": [],
        }
        app.extensions["development"].gpu_benchmark = benchmark
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        screenshot = data_dir / "gpu-benchmark.png"
        try:
            completed = subprocess.run(
                [
                    "node",
                    str(Path(__file__).with_name("browser.js")),
                    f"http://127.0.0.1:{server.server_port}/",
                    initial_password,
                    changed_password,
                    str(screenshot),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
            if completed.stdout:
                print(completed.stdout.strip())
            if completed.returncode != 0:
                print(completed.stderr.strip())
                return completed.returncode
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            if not screenshot.exists() or screenshot.stat().st_size < 1024:
                raise RuntimeError("浏览器未生成有效 GPU 评估截图")
            if set(result.values()) != {"passed"}:
                raise RuntimeError("浏览器验收流程未全部通过")
            if len(submitted_payloads) != 1:
                raise RuntimeError("浏览器没有且仅有一次提交 GPU 评估")
            stored = app.extensions["database"].query_one(
                "SELECT mode,gpu_count,result_json FROM gpu_benchmarks WHERE host_id=?",
                (host["id"],),
            )
            if not stored or stored["mode"] != "multi" or stored["gpu_count"] != 8:
                raise RuntimeError("GPU 评估结果未正确写入数据库")
            audit = app.extensions["database"].query_one(
                "SELECT action FROM audit_logs WHERE action='gpu_benchmark_completed' AND target_id=?",
                (str(host["id"]),),
            )
            if not audit:
                raise RuntimeError("GPU 评估没有写入审计日志")
            return 0
        finally:
            server.shutdown()
            thread.join(timeout=5)
            app.extensions["shutdown"]()


if __name__ == "__main__":
    raise SystemExit(main())

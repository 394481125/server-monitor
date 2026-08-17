from __future__ import annotations

import uuid

from flask import Response, g, jsonify, request

from ..utils import json_dump, json_load, utc_iso
from ..web import WebContext


def register_development_routes(context: WebContext) -> None:
    app = context.app
    audit_action = context.audit_action
    body = context.body
    database = context.database
    development = context.development
    hosts = context.hosts
    login_required = context.login_required
    operation_host = context.operation_host
    permission_service = context.permission_service

    @app.get("/api/development/hosts")
    @login_required(permission="page.environments")
    def development_hosts():
        items = hosts.list()
        return jsonify(items=[{
            "id": item["id"], "name": item["name"], "address": item["address"],
            "username": item["username"], "status": item.get("status"),
            "allow_install": item.get("allow_install", False),
            "allow_stress": item.get("allow_stress", False),
        } for item in items])

    @app.get("/api/hosts/<int:host_id>/development/stack")
    @login_required(permission="development.view")
    def development_stack(host_id: int):
        return jsonify(stack=development.development_stack(hosts.get(host_id, include_secrets=True)))

    @app.get("/api/hosts/<int:host_id>/development/gpu-diagnostics")
    @login_required(permission="diagnostics.view")
    def gpu_diagnostics(host_id: int):
        return jsonify(diagnostics=development.gpu_diagnostics(hosts.get(host_id, include_secrets=True)))

    @app.get("/api/hosts/<int:host_id>/development/gpu-benchmarks")
    @login_required(permission="diagnostics.view")
    def gpu_benchmark_history(host_id: int):
        hosts.get(host_id)
        try:
            limit = max(1, min(20, int(request.args.get("limit", 10))))
        except ValueError as exc:
            raise ValueError("limit 必须是整数") from exc
        rows = database.query_all(
            "SELECT id,mode,python_command,duration_seconds,gpu_count,result_json,created_at "
            "FROM gpu_benchmarks WHERE host_id=? ORDER BY created_at DESC LIMIT ?",
            (host_id, limit),
        )
        return jsonify(items=[{
            "id": row["id"], "mode": row["mode"], "python": row["python_command"],
            "duration_seconds": row["duration_seconds"], "gpu_count": row["gpu_count"],
            "created_at": row["created_at"], "result": json_load(row["result_json"], {}),
        } for row in rows])

    @app.post("/api/hosts/<int:host_id>/development/gpu-benchmarks")
    @login_required(permission="gpu.benchmark", write=True, elevated=True)
    def run_gpu_benchmark_route(host_id: int):
        host = operation_host(host_id, "allow_stress", "GPU 快速评估")
        result, python_command, duration_seconds = development.gpu_benchmark(host, body())
        benchmark_id = str(uuid.uuid4())
        created_at = utc_iso()
        database.execute(
            "INSERT INTO gpu_benchmarks(id,host_id,user_id,mode,python_command,duration_seconds,gpu_count,result_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                benchmark_id, host_id, g.user["id"], result["mode"], python_command,
                duration_seconds, int(result["gpu_count"]), json_dump(result), created_at,
            ),
        )
        audit_action(
            "gpu_benchmark_completed", target_type="host", target_id=host_id,
            summary=f"完成 {result['mode']} GPU 快速评估，覆盖 {result['gpu_count']} 张 GPU",
        )
        return jsonify(id=benchmark_id, created_at=created_at, result=result), 201

    @app.get("/api/hosts/<int:host_id>/development/environments")
    @login_required(permission="development.view")
    def environment_inventory(host_id: int):
        host = hosts.get(host_id, include_secrets=True)
        root = request.args.get("root") or f"/home/{host['username']}"
        return jsonify(development.environment_inventory(host, root))

    @app.post("/api/hosts/<int:host_id>/development/environment-plan")
    @login_required(permission="development.plan", write=True)
    def environment_plan(host_id: int):
        plan = development.environment_plan(operation_host(host_id, "allow_install", "开发环境管理"), body())
        audit_action("environment_plan_generated", target_type="host", target_id=host_id, summary=f"生成虚拟环境 {plan['backend']} {plan['action']} 方案")
        return jsonify(plan=plan)

    @app.post("/api/hosts/<int:host_id>/development/environment-backup-plan")
    @login_required(permission="development.plan", write=True)
    def environment_backup_plan(host_id: int):
        operation_host(host_id, "allow_install", "开发环境管理")
        plan = development.environment_backup_plan(body())
        audit_action("environment_backup_plan_generated", target_type="host", target_id=host_id, summary=f"生成虚拟环境备份脚本 {plan['path']}")
        return jsonify(plan=plan)

    @app.post("/api/hosts/<int:host_id>/development/environment-execute")
    @login_required(permission="development.execute", write=True, elevated=True)
    def execute_environment_plan(host_id: int):
        result = development.execute_environment_plan(operation_host(host_id, "allow_install", "开发环境管理"), body())
        audit_action(
            "environment_plan_executed", target_type="host", target_id=host_id, success=result["ok"],
            summary=f"网页执行虚拟环境 {result['plan']['backend']} {result['plan']['action']} 方案",
            error=None if result["ok"] else result["stderr"][:500],
        )
        return jsonify(result), 200 if result["ok"] else 409

    @app.get("/api/hosts/<int:host_id>/development/conda-export")
    @login_required(permission="development.view")
    def export_conda_environment(host_id: int):
        path = request.args.get("path", "")
        content = development.export_conda_environment(hosts.get(host_id, include_secrets=True), path)
        audit_action("conda_environment_exported", target_type="host", target_id=host_id, summary=f"导出 conda 环境 {path}")
        return Response(content, content_type="text/yaml; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''conda-environment-{host_id}.yml"})

    @app.post("/api/hosts/<int:host_id>/development/conda-yaml-plan")
    @login_required(permission="development.plan", write=True)
    def conda_yaml_plan(host_id: int):
        plan = development.conda_yaml_plan(operation_host(host_id, "allow_install", "开发环境管理"), body())
        audit_action("conda_yaml_plan_generated", target_type="host", target_id=host_id, summary=f"生成 conda YAML 重建方案 {plan['path']}")
        return jsonify(plan=plan)

    @app.post("/api/hosts/<int:host_id>/development/conda-yaml-execute")
    @login_required(permission="development.execute", write=True, elevated=True)
    def execute_conda_yaml_plan(host_id: int):
        result = development.execute_conda_yaml_plan(operation_host(host_id, "allow_install", "开发环境管理"), body())
        audit_action("conda_yaml_plan_executed", target_type="host", target_id=host_id, success=result["ok"], summary=f"网页执行 conda YAML 重建 {result['plan']['path']}")
        return jsonify(result), 200 if result["ok"] else 409

    @app.post("/api/hosts/<int:host_id>/development/system-plan")
    @login_required(permission="development.plan", write=True)
    def system_plan(host_id: int):
        payload = body()
        if payload.get("kind") == "apt" and not permission_service.allowed(g.user, "apt.plan"):
            return jsonify(error="当前账户未获得 APT 方案权限", permission="apt.plan"), 403
        plan = development.system_plan(operation_host(host_id, "allow_install", "开发环境管理"), payload)
        audit_action("system_plan_generated", target_type="host", target_id=host_id, summary=f"生成 {plan['title']} 脚本")
        return jsonify(plan=plan)

    @app.get("/api/hosts/<int:host_id>/development/apt-packages")
    @login_required(permission="development.view")
    def apt_packages(host_id: int):
        return jsonify(items=development.apt_packages(hosts.get(host_id, include_secrets=True), request.args.get("search", "")))

from __future__ import annotations

from flask import jsonify, request

from ..operations import OperationError
from ..services import HistoryService
from ..web import WebContext


def register_operation_routes(context: WebContext) -> None:
    app = context.app
    audit_action = context.audit_action
    backups = context.backups
    body = context.body
    config = context.config
    database = context.database
    hosts = context.hosts
    login_required = context.login_required
    operation_host = context.operation_host
    operations = context.operations

    @app.get("/api/hosts/<int:host_id>/tmux")
    @login_required(permission="tmux.view")
    def list_tmux(host_id: int):
        return jsonify(items=operations.tmux_sessions(operation_host(host_id, "allow_tmux", " Tmux 操作")))

    @app.get("/api/hosts/<int:host_id>/tmux/<path:name>/snapshot")
    @login_required(permission="tmux.view")
    def tmux_snapshot(host_id: int, name: str):
        return jsonify(snapshot=operations.tmux_snapshot(operation_host(host_id, "allow_tmux", " Tmux 操作"), name))

    @app.post("/api/hosts/<int:host_id>/tmux")
    @login_required(permission="tmux.manage", write=True)
    def create_tmux(host_id: int):
        name = str(body().get("name", ""))
        operations.tmux_create(operation_host(host_id, "allow_tmux", " Tmux 操作"), name)
        audit_action("tmux_created", target_type="host", target_id=host_id, summary=f"创建 Tmux 会话 {name}")
        return jsonify(ok=True), 201

    @app.patch("/api/hosts/<int:host_id>/tmux/<path:name>")
    @login_required(permission="tmux.manage", write=True)
    def rename_tmux(host_id: int, name: str):
        new_name = str(body().get("name", ""))
        operations.tmux_rename(operation_host(host_id, "allow_tmux", " Tmux 操作"), name, new_name)
        audit_action("tmux_renamed", target_type="host", target_id=host_id, summary=f"重命名 Tmux 会话 {name}")
        return jsonify(ok=True)

    @app.delete("/api/hosts/<int:host_id>/tmux/<path:name>")
    @login_required(permission="tmux.manage", write=True, elevated=True)
    def delete_tmux(host_id: int, name: str):
        operations.tmux_kill(operation_host(host_id, "allow_tmux", " Tmux 操作"), name)
        audit_action("tmux_deleted", target_type="host", target_id=host_id, summary=f"删除 Tmux 会话 {name}")
        return jsonify(ok=True)

    @app.get("/api/hosts/<int:host_id>/processes")
    @login_required(permission="process.view")
    def processes(host_id: int):
        return jsonify(items=operations.processes(operation_host(host_id, "allow_process", "进程操作"), request.args.get("hide_kernel", "1") != "0"))

    @app.post("/api/hosts/<int:host_id>/processes/<int:pid>/terminate")
    @login_required(permission="process.terminate", write=True, elevated=True)
    def terminate_process(host_id: int, pid: int):
        payload = body()
        operations.terminate_process(operation_host(host_id, "allow_process", "进程操作"), pid, str(payload.get("started", "")), str(payload.get("signal", "TERM")))
        audit_action("process_terminated", target_type="process", target_id=pid, summary=f"发送 SIG{payload.get('signal', 'TERM')} 到进程")
        return jsonify(ok=True)

    @app.get("/api/hosts/<int:host_id>/system-services")
    @login_required(permission="diagnostics.view")
    def system_services(host_id: int):
        return jsonify(items=operations.systemd_services(hosts.get(host_id, include_secrets=True)))

    @app.get("/api/hosts/<int:host_id>/system-services/logs")
    @login_required(permission="diagnostics.view")
    def system_service_logs(host_id: int):
        return jsonify(operations.service_logs(
            hosts.get(host_id, include_secrets=True),
            request.args.get("unit", ""),
            int(request.args.get("lines", 100)),
            request.args.get("keyword", ""),
        ))

    @app.get("/api/hosts/<int:host_id>/system-services/restart-plan")
    @login_required(permission="diagnostics.view")
    def system_service_restart_plan(host_id: int):
        unit = request.args.get("unit", "")
        plan = operations.service_restart_plan(unit)
        audit_action("system_service_restart_plan", target_type="host", target_id=host_id, summary=f"生成 systemd 服务脚本 {unit}")
        return jsonify(unit=unit, script=plan, remote_execution=False)

    @app.post("/api/hosts/<int:host_id>/network-diagnostic")
    @login_required(permission="diagnostics.view", write=True)
    def network_diagnostic(host_id: int):
        payload = body()
        result = operations.network_diagnostic(
            hosts.get(host_id, include_secrets=True),
            payload.get("target", ""),
            str(payload.get("mode", "ping")),
            payload.get("port"),
        )
        audit_action("network_diagnostic", target_type="host", target_id=host_id, success=result["success"], summary=f"网络诊断 {result['mode']} {result['target']}", error=None if result["success"] else result["output"][:500])
        return jsonify(result)

    @app.get("/api/hosts/<int:host_id>/docker/inventory")
    @login_required(permission="page.hosts")
    def docker_inventory(host_id: int):
        host = hosts.get(host_id, include_secrets=True)
        if not host.get("docker_enabled"):
            raise OperationError("该主机已关闭 Docker 采集")
        return jsonify(operations.docker_inventory(host))

    @app.get("/api/hosts/<int:host_id>/docker/logs")
    @login_required(permission="page.hosts")
    def docker_logs(host_id: int):
        host = hosts.get(host_id, include_secrets=True)
        if not host.get("docker_enabled"):
            raise OperationError("该主机已关闭 Docker 采集")
        return jsonify(operations.docker_logs(host, request.args.get("container", ""), int(request.args.get("lines", 100)), request.args.get("keyword", "")))

    @app.get("/api/hosts/<int:host_id>/tools")
    @login_required(permission="tools.view")
    def detect_tools(host_id: int):
        host = hosts.get(host_id, include_secrets=True)
        return jsonify(tools=operations.detect_tools(host), versions=operations.detect_tool_versions(host))

    @app.post("/api/hosts/<int:host_id>/health-inspection")
    @login_required(permission="diagnostics.view", write=True)
    def health_inspection(host_id: int):
        result = operations.health_inspection(hosts.get(host_id, include_secrets=True))
        audit_action("host_health_inspection", target_type="host", target_id=host_id, summary=f"完成主机巡检：通过 {result['passed']}，警告 {result['warnings']}，不可用 {result['unavailable']}")
        return jsonify(result)

    @app.post("/api/hosts/<int:host_id>/tools/<tool>/install")
    @login_required(permission="tools.install", write=True, elevated=True)
    def install_tool(host_id: int, tool: str):
        command = operations.install_tool(operation_host(host_id, "allow_install", "工具安装"), tool)
        audit_action("tool_installed", target_type="host", target_id=host_id, summary=f"安装工具 {tool}: {command}")
        return jsonify(ok=True)

    @app.get("/api/hosts/<int:host_id>/tools/<tool>/install-plan")
    @login_required(permission="tools.install")
    def install_tool_plan(host_id: int, tool: str):
        host = operation_host(host_id, "allow_install", "工具安装")
        return jsonify(command=operations.installation_command(host, tool), tool=tool, target_version=operations.tool_target_version(tool), sudo_password_configured=bool(host.get("sudo_password")))

    @app.post("/api/hosts/<int:host_id>/stress")
    @login_required(permission="stress.manage", write=True, elevated=True)
    def start_stress(host_id: int):
        payload = body()
        task_id = operations.start_stress(operation_host(host_id, "allow_stress", "压力测试"), int(payload.get("cpu_workers", 0)), int(payload.get("memory_workers", 0)), int(payload.get("memory_percent", 50)), int(payload.get("duration_minutes", 1)))
        audit_action("stress_started", target_type="host", target_id=host_id, summary=f"启动压力测试 {task_id}")
        return jsonify(task_id=task_id), 201

    @app.post("/api/hosts/<int:host_id>/stress/<task_id>/stop")
    @login_required(permission="stress.manage", write=True)
    def stop_stress(host_id: int, task_id: str):
        operations.stop_stress(operation_host(host_id, "allow_stress", "压力测试"), task_id)
        audit_action("stress_stopped", target_type="host", target_id=host_id, summary=f"停止压力测试 {task_id}")
        return jsonify(ok=True)

    @app.get("/api/hosts/<int:host_id>/stress/<task_id>")
    @login_required(permission="stress.view")
    def stress_status(host_id: int, task_id: str):
        return jsonify(task=operations.stress_status(operation_host(host_id, "allow_stress", "压力测试"), task_id))

    @app.post("/api/backups")
    @login_required(permission="backup.create", write=True)
    def create_backup():
        settings = config.all()
        path = backups.create(settings["backup_dir"], settings["backup_keep"])
        audit_action("backup_created", target_type="backup", target_id=str(path), summary="数据库备份成功")
        return jsonify(path=str(path)), 201

    @app.post("/api/maintenance/compact")
    @login_required(admin=True, write=True, elevated=True)
    def compact_database():
        settings = config.all()
        history_service = HistoryService(database)
        aggregate = history_service.aggregate(
            mid_seconds=settings["aggregation_mid_seconds"],
            long_seconds=settings["aggregation_long_seconds"],
            raw_retention_minutes=settings["metric_raw_retention_minutes"],
            mid_retention_hours=settings["metric_mid_retention_hours"],
        )
        cleanup = history_service.cleanup(
            metric_retention_days=settings["metric_retention_days"],
            log_retention_days=settings["log_retention_days"],
            collection_task_retention_minutes=settings["collection_task_retention_minutes"],
        )
        result = database.compact()
        audit_action(
            "database_compacted",
            target_type="database",
            summary=f"清理并压缩数据库，回收 {result['reclaimed_bytes']} 字节",
        )
        return jsonify(aggregate=aggregate, cleanup=cleanup, **result)

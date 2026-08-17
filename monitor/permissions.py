from __future__ import annotations

from typing import Any

from .utils import utc_iso


# The catalog is intentionally code-owned. A permission cannot be invented by a
# client and newly added permissions default to denied for ordinary users.
PERMISSION_CATALOG: tuple[dict[str, str], ...] = (
    {"key": "page.dashboard", "kind": "page", "group": "页面", "label": "集群概览", "description": "查看主机实时状态和资源卡片"},
    {"key": "page.hosts", "kind": "page", "group": "页面", "label": "主机管理", "description": "查看纳管主机和主机详情"},
    {"key": "page.files", "kind": "page", "group": "页面", "label": "文件管理", "description": "浏览服务器文件并执行授权的文件操作"},
    {"key": "page.jobs", "kind": "page", "group": "页面", "label": "调度记录", "description": "查看 GPU 调度记录"},
    {"key": "page.alerts", "kind": "page", "group": "页面", "label": "告警事件", "description": "查看告警和恢复事件"},
    {"key": "page.logs", "kind": "page", "group": "页面", "label": "审计日志", "description": "查看操作审计日志"},
    {"key": "page.settings", "kind": "page", "group": "页面", "label": "系统设置", "description": "查看系统配置和用户设置"},
    {"key": "page.environments", "kind": "page", "group": "页面", "label": "开发环境", "description": "查看主机 GPU 软件栈、虚拟环境和 APT 方案"},
    {"key": "host.manage", "kind": "action", "group": "主机", "label": "管理主机", "description": "添加、编辑、启停和删除主机"},
    {"key": "host.refresh", "kind": "action", "group": "主机", "label": "手动刷新采集", "description": "立即从远端刷新主机指标"},
    {"key": "terminal.open", "kind": "action", "group": "远程运维", "label": "打开 SSH 终端", "description": "打开交互式 SSH 终端"},
    {"key": "tmux.view", "kind": "action", "group": "远程运维", "label": "查看 Tmux", "description": "查看远端 Tmux 会话和快照"},
    {"key": "tmux.manage", "kind": "action", "group": "远程运维", "label": "管理 Tmux", "description": "创建、重命名和删除 Tmux 会话"},
    {"key": "process.view", "kind": "action", "group": "远程运维", "label": "查看进程", "description": "查看远程进程和当前工作目录"},
    {"key": "process.terminate", "kind": "action", "group": "远程运维", "label": "终止进程", "description": "向远程进程发送终止信号"},
    {"key": "tools.view", "kind": "action", "group": "远程运维", "label": "查看工具", "description": "检测远端工具安装状态"},
    {"key": "tools.install", "kind": "action", "group": "远程运维", "label": "安装工具", "description": "通过受控 sudo 安装远端工具"},
    {"key": "stress.view", "kind": "action", "group": "远程运维", "label": "查看压力任务", "description": "查看压力测试任务状态"},
    {"key": "stress.manage", "kind": "action", "group": "远程运维", "label": "管理压力测试", "description": "启动和停止远端压力测试"},
    {"key": "gpu.manage", "kind": "action", "group": "调度", "label": "管理 GPU 调度", "description": "修改 GPU 自动调度策略"},
    {"key": "gpu.benchmark", "kind": "action", "group": "诊断", "label": "运行 GPU 快速评估", "description": "在允许压力任务的主机上运行受控单卡或多卡基准"},
    {"key": "jobs.export", "kind": "action", "group": "调度", "label": "导出调度记录", "description": "导出 GPU 调度 CSV"},
    {"key": "logs.export", "kind": "action", "group": "审计", "label": "导出审计日志", "description": "导出操作日志 CSV"},
    {"key": "settings.manage", "kind": "action", "group": "系统", "label": "修改系统设置", "description": "保存采集、告警和安全设置"},
    {"key": "backup.create", "kind": "action", "group": "系统", "label": "创建数据库备份", "description": "立即创建 SQLite 备份"},
    {"key": "files.browse", "kind": "action", "group": "文件", "label": "浏览文件", "description": "列出远端目录和文件元数据"},
    {"key": "files.download", "kind": "action", "group": "文件", "label": "下载文件", "description": "下载文件或目录 ZIP"},
    {"key": "files.upload", "kind": "action", "group": "文件", "label": "上传文件", "description": "上传文件到远端目录"},
    {"key": "files.manage", "kind": "action", "group": "文件", "label": "管理文件", "description": "新建目录、重命名、移动和复制"},
    {"key": "files.delete", "kind": "action", "group": "文件", "label": "删除文件", "description": "删除远端文件或目录"},
    {"key": "development.view", "kind": "action", "group": "开发环境", "label": "查看开发环境", "description": "盘点 GPU 驱动、CUDA、cuDNN 和 Python 工具"},
    {"key": "development.plan", "kind": "action", "group": "开发环境", "label": "生成环境方案", "description": "生成受约束的虚拟环境、GPU 软件栈或 APT 脚本"},
    {"key": "development.execute", "kind": "action", "group": "开发环境", "label": "执行环境方案", "description": "经密码复核后通过 SSH 执行虚拟环境方案"},
    {"key": "diagnostics.view", "kind": "action", "group": "诊断", "label": "GPU 健康诊断", "description": "查看 GPU 温度、ECC、NVLink 和利用率诊断"},
    {"key": "storage.scan", "kind": "action", "group": "文件", "label": "磁盘扫描", "description": "查看目录容量并扫描受限范围内的大文件"},
    {"key": "alerts.manage", "kind": "action", "group": "告警", "label": "管理告警事件", "description": "确认告警或软清理告警记录"},
    {"key": "apt.plan", "kind": "action", "group": "开发环境", "label": "生成 APT 方案", "description": "生成 apt update、upgrade、修复和包操作脚本"},
)

PERMISSION_KEYS = frozenset(item["key"] for item in PERMISSION_CATALOG)
PAGE_KEYS = frozenset(item["key"] for item in PERMISSION_CATALOG if item["kind"] == "page")
DEFAULT_VIEWER_PERMISSIONS = frozenset(
    {
        "page.dashboard",
        "page.hosts",
        "page.jobs",
        "page.alerts",
        "page.logs",
        "page.environments",
        "tmux.view",
        "process.view",
        "tools.view",
        "stress.view",
        "jobs.export",
        "logs.export",
        "development.view",
        "diagnostics.view",
        "storage.scan",
    }
)


class PermissionError(ValueError):
    pass


class PermissionService:
    def __init__(self, database: Any):
        self.database = database

    @staticmethod
    def catalog() -> list[dict[str, str]]:
        return [dict(item) for item in PERMISSION_CATALOG]

    def ensure_defaults(self) -> None:
        now = utc_iso()
        users = self.database.query_all("SELECT id,role FROM users")
        with self.database.transaction() as connection:
            for user in users:
                count = connection.execute("SELECT COUNT(*) FROM user_permissions WHERE user_id=?", (user["id"],)).fetchone()[0]
                if count == 0 and user["role"] != "admin":
                    for key in PERMISSION_KEYS:
                        connection.execute(
                            "INSERT OR IGNORE INTO user_permissions(user_id,permission,granted,updated_at) VALUES(?,?,?,?)",
                            (user["id"], key, int(key in DEFAULT_VIEWER_PERMISSIONS), now),
                        )
                for key in PAGE_KEYS:
                    connection.execute(
                        "INSERT OR IGNORE INTO user_permission_preferences(user_id,permission,visible,updated_at) VALUES(?,?,1,?)",
                        (user["id"], key, now),
                    )

    def initialize_user(self, user_id: int, role: str) -> None:
        now = utc_iso()
        with self.database.transaction() as connection:
            if role != "admin":
                for key in PERMISSION_KEYS:
                    connection.execute(
                        "INSERT OR IGNORE INTO user_permissions(user_id,permission,granted,updated_at) VALUES(?,?,?,?)",
                        (user_id, key, int(key in DEFAULT_VIEWER_PERMISSIONS), now),
                    )
            for key in PAGE_KEYS:
                connection.execute(
                    "INSERT OR IGNORE INTO user_permission_preferences(user_id,permission,visible,updated_at) VALUES(?,?,1,?)",
                    (user_id, key, now),
                )

    def grants(self, user_id: int) -> set[str]:
        rows = self.database.query_all("SELECT permission FROM user_permissions WHERE user_id=? AND granted=1", (user_id,))
        return {row["permission"] for row in rows if row["permission"] in PERMISSION_KEYS}

    def visible_pages(self, user_id: int) -> set[str]:
        rows = self.database.query_all("SELECT permission FROM user_permission_preferences WHERE user_id=? AND visible=1", (user_id,))
        return {row["permission"] for row in rows if row["permission"] in PAGE_KEYS}

    def decorate(self, user: dict[str, Any] | None) -> dict[str, Any] | None:
        if not user:
            return None
        result = dict(user)
        grants = set(PERMISSION_KEYS) if user["role"] == "admin" else self.grants(user["id"])
        visible = set(PAGE_KEYS) if user["role"] == "admin" else self.visible_pages(user["id"])
        result["granted_permissions"] = sorted(grants)
        result["permissions"] = sorted(grants)
        result["visible_pages"] = sorted(visible & grants)
        return result

    def allowed(self, user: dict[str, Any] | None, permission: str) -> bool:
        if not user or permission not in PERMISSION_KEYS:
            return False
        return user["role"] == "admin" or permission in self.grants(user["id"])

    def user_permissions(self, user_id: int) -> dict[str, list[str]]:
        user = self.database.query_one("SELECT role FROM users WHERE id=?", (user_id,))
        grants = set(PERMISSION_KEYS) if user and user["role"] == "admin" else self.grants(user_id)
        visible = set(PAGE_KEYS) if user and user["role"] == "admin" else self.visible_pages(user_id)
        return {"granted": sorted(grants), "visible_pages": sorted(visible & grants)}

    def set_grants(self, user_id: int, permissions: list[Any]) -> list[str]:
        if not isinstance(permissions, list):
            raise PermissionError("permissions 必须是数组")
        cleaned = {str(item) for item in permissions}
        unknown = cleaned - PERMISSION_KEYS
        if unknown:
            raise PermissionError("包含未知权限")
        user = self.database.query_one("SELECT id,role FROM users WHERE id=?", (user_id,))
        if not user:
            raise PermissionError("用户不存在")
        if user["role"] == "admin":
            return sorted(PERMISSION_KEYS)
        now = utc_iso()
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM user_permissions WHERE user_id=?", (user_id,))
            # Keep explicit denials as well as grants.  An empty permission set
            # must remain empty after an application restart.
            for key in sorted(PERMISSION_KEYS):
                connection.execute(
                    "INSERT INTO user_permissions(user_id,permission,granted,updated_at) VALUES(?,?,?,?)",
                    (user_id, key, int(key in cleaned), now),
                )
        return sorted(cleaned)

    def set_visibility(self, user_id: int, visible_pages: list[Any]) -> list[str]:
        if not isinstance(visible_pages, list):
            raise PermissionError("visible_pages 必须是数组")
        cleaned = {str(item) for item in visible_pages}
        if cleaned - PAGE_KEYS:
            raise PermissionError("包含未知页面")
        grants = self.grants(user_id)
        cleaned &= grants
        now = utc_iso()
        with self.database.transaction() as connection:
            for key in PAGE_KEYS:
                connection.execute(
                    "INSERT INTO user_permission_preferences(user_id,permission,visible,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id,permission) DO UPDATE SET visible=excluded.visible,updated_at=excluded.updated_at",
                    (user_id, key, int(key in cleaned), now),
                )
        return sorted(cleaned)

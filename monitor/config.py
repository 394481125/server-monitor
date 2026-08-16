from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


DEFAULTS: dict[str, Any] = {
    "collection_interval": 10,
    "frontend_refresh_interval": 5,
    "ssh_concurrency": 10,
    "queue_limit": 50,
    "interactive_ssh_limit": 3,
    "ssh_connect_timeout": 5,
    "collection_timeout": 15,
    "collection_retries": 2,
    "retry_interval": 3,
    "install_timeout": 120,
    "scan_timeout_seconds": 60,
    "scan_max_depth": 8,
    "scan_result_limit": 100,
    "scan_minimum_mib": 1024,
    "environment_inventory_timeout": 60,
    "gpu_submit_timeout": 30,
    "gpu_direct_timeout": 300,
    "ssh_reuse": True,
    "ssh_idle_close": 60,
    "metric_retention_days": 7,
    "metric_raw_retention_minutes": 15,
    "metric_mid_retention_hours": 6,
    "collection_task_retention_minutes": 60,
    "log_retention_days": 30,
    "aggregation_mid_seconds": 60,
    "aggregation_long_seconds": 300,
    "green_threshold": 60,
    "yellow_threshold": 80,
    "cpu_temp_threshold": 80,
    "gpu_temp_threshold": 85,
    "gpu_power_threshold_percent": 95,
    "gpu_fan_min_percent": 5,
    "gpu_fan_alert_temperature": 60,
    "gpu_ecc_corrected_threshold": 100,
    "gpu_xid_alert_enabled": True,
    "gpu_pcie_alert_enabled": True,
    "gpu_throttle_alert_enabled": True,
    "gpu_residual_alert_enabled": True,
    "disk_temp_threshold": 55,
    "filesystem_usage_threshold": 85,
    "filesystem_inode_threshold": 85,
    "swap_usage_threshold": 50,
    "alert_samples": 3,
    "alert_hysteresis": 3,
    "alert_repeat_minutes": 30,
    "gpu_scheduler_enabled": False,
    "gpu_idle_mode": "both",
    "gpu_util_threshold": 10,
    "gpu_memory_threshold": 10,
    "gpu_idle_seconds": 900,
    "gpu_process_guard": True,
    "gpu_cooldown_seconds": 300,
    "gpu_max_attempts": 3,
    "gpu_retry_seconds": 60,
    "gpu_freeze_seconds": 3600,
    "login_fail_limit": 5,
    "login_window_minutes": 5,
    "login_lock_minutes": 5,
    "session_idle_minutes": 30,
    "terminal_idle_seconds": 300,
    "backup_time": "03:00",
    "backup_dir": "backups",
    "backup_keep": 10,
    "schedule_output_limit": 1024 * 1024,
    "toast_enabled": True,
    "serverchan_enabled": False,
    "serverchan_sendkey": "",
    "serverchan_events": [
        "host_offline",
        "temperature_high",
        "filesystem_usage_high",
        "filesystem_inode_high",
        "swap_usage_high",
        "gpu_schedule_success",
        "gpu_schedule_failed",
        "gpu_schedule_frozen",
        "gpu_power_high",
        "gpu_fan_low",
        "gpu_ecc_error",
        "gpu_xid_error",
        "gpu_pcie_degraded",
        "gpu_throttling",
        "gpu_residual_memory",
        "backup_failed",
    ],
    "timezone": "Asia/Shanghai",
}


@dataclass(frozen=True)
class NumberRule:
    minimum: float
    maximum: float
    integer: bool = True


NUMBER_RULES: dict[str, NumberRule] = {
    "collection_interval": NumberRule(5, 60),
    "frontend_refresh_interval": NumberRule(3, 30),
    "ssh_concurrency": NumberRule(1, 30),
    "queue_limit": NumberRule(10, 200),
    "interactive_ssh_limit": NumberRule(1, 10),
    "ssh_connect_timeout": NumberRule(3, 30),
    "collection_timeout": NumberRule(5, 60),
    "collection_retries": NumberRule(0, 3),
    "retry_interval": NumberRule(1, 30),
    "install_timeout": NumberRule(30, 600),
    "scan_timeout_seconds": NumberRule(10, 120),
    "scan_max_depth": NumberRule(1, 12),
    "scan_result_limit": NumberRule(1, 200),
    "scan_minimum_mib": NumberRule(1, 10 * 1024),
    "environment_inventory_timeout": NumberRule(10, 120),
    "gpu_submit_timeout": NumberRule(10, 120),
    "gpu_direct_timeout": NumberRule(30, 600),
    "ssh_idle_close": NumberRule(10, 600),
    "metric_retention_days": NumberRule(1, 30),
    "metric_raw_retention_minutes": NumberRule(5, 360),
    "metric_mid_retention_hours": NumberRule(1, 168),
    "collection_task_retention_minutes": NumberRule(15, 1440),
    "log_retention_days": NumberRule(7, 180),
    "aggregation_mid_seconds": NumberRule(30, 120),
    "aggregation_long_seconds": NumberRule(120, 600),
    "green_threshold": NumberRule(0, 99),
    "yellow_threshold": NumberRule(1, 100),
    "cpu_temp_threshold": NumberRule(0, 150),
    "gpu_temp_threshold": NumberRule(0, 150),
    "gpu_power_threshold_percent": NumberRule(50, 100),
    "gpu_fan_min_percent": NumberRule(0, 100),
    "gpu_fan_alert_temperature": NumberRule(0, 120),
    "gpu_ecc_corrected_threshold": NumberRule(1, 1000000),
    "disk_temp_threshold": NumberRule(0, 150),
    "filesystem_usage_threshold": NumberRule(1, 100),
    "filesystem_inode_threshold": NumberRule(1, 100),
    "swap_usage_threshold": NumberRule(1, 100),
    "alert_samples": NumberRule(1, 10),
    "alert_hysteresis": NumberRule(0, 20),
    "alert_repeat_minutes": NumberRule(0, 1440),
    "gpu_util_threshold": NumberRule(0, 100),
    "gpu_memory_threshold": NumberRule(0, 100),
    "gpu_idle_seconds": NumberRule(60, 86400),
    "gpu_cooldown_seconds": NumberRule(0, 3600),
    "gpu_max_attempts": NumberRule(1, 5),
    "gpu_retry_seconds": NumberRule(5, 3600),
    "gpu_freeze_seconds": NumberRule(60, 86400),
    "login_fail_limit": NumberRule(1, 20),
    "login_window_minutes": NumberRule(1, 60),
    "login_lock_minutes": NumberRule(1, 60),
    "session_idle_minutes": NumberRule(5, 240),
    "terminal_idle_seconds": NumberRule(30, 1800),
    "backup_keep": NumberRule(1, 10),
    "schedule_output_limit": NumberRule(64 * 1024, 5 * 1024 * 1024),
}

BOOLEAN_KEYS = {
    "ssh_reuse",
    "gpu_scheduler_enabled",
    "gpu_process_guard",
    "toast_enabled",
    "serverchan_enabled",
    "gpu_xid_alert_enabled",
    "gpu_pcie_alert_enabled",
    "gpu_throttle_alert_enabled",
    "gpu_residual_alert_enabled",
}

CHOICES = {
    "gpu_idle_mode": {"util", "memory", "both"},
    "timezone": {"Asia/Shanghai", "UTC"},
}


class ConfigError(ValueError):
    pass


def validate_settings(values: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"未知配置项: {', '.join(sorted(unknown))}")

    for key, value in values.items():
        if key in NUMBER_RULES:
            rule = NUMBER_RULES[key]
            if isinstance(value, bool):
                raise ConfigError(f"{key} 必须是数字")
            try:
                number = int(value) if rule.integer else float(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{key} 必须是数字") from exc
            if not rule.minimum <= number <= rule.maximum:
                raise ConfigError(f"{key} 必须在 {rule.minimum:g}～{rule.maximum:g} 之间")
            cleaned[key] = number
        elif key in BOOLEAN_KEYS:
            if not isinstance(value, bool):
                raise ConfigError(f"{key} 必须是布尔值")
            cleaned[key] = value
        elif key in CHOICES:
            if value not in CHOICES[key]:
                raise ConfigError(f"{key} 的值无效")
            cleaned[key] = value
        elif key == "backup_time":
            if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
                raise ConfigError("backup_time 必须使用 HH:MM 格式")
            try:
                hour, minute = (int(part) for part in value.split(":"))
            except ValueError as exc:
                raise ConfigError("backup_time 必须使用 HH:MM 格式") from exc
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ConfigError("backup_time 超出有效时间范围")
            cleaned[key] = value
        elif key in {"backup_dir", "serverchan_sendkey"}:
            if not isinstance(value, str):
                raise ConfigError(f"{key} 必须是字符串")
            value = value.strip()
            if key == "backup_dir" and not value:
                raise ConfigError("backup_dir 不能为空")
            if len(value) > 1024:
                raise ConfigError(f"{key} 过长")
            cleaned[key] = value
        elif key == "serverchan_events":
            allowed_events = {
                "host_offline", "temperature_high", "filesystem_usage_high", "filesystem_inode_high", "swap_usage_high",
                "gpu_schedule_success", "gpu_schedule_failed", "gpu_schedule_frozen", "gpu_power_high",
                "gpu_fan_low", "gpu_ecc_error", "gpu_xid_error", "gpu_pcie_degraded", "gpu_throttling", "gpu_residual_memory",
                "backup_failed",
            }
            if not isinstance(value, list) or any(item not in allowed_events for item in value):
                raise ConfigError("serverchan_events 包含无效事件类型")
            cleaned[key] = sorted(set(value))
        else:
            cleaned[key] = value

    persisted = {key: value for key, value in (current or {}).items() if key in DEFAULTS}
    merged = {**DEFAULTS, **persisted, **cleaned}
    if merged["green_threshold"] >= merged["yellow_threshold"]:
        raise ConfigError("绿色上限必须小于黄色上限")
    for threshold_key in ("cpu_temp_threshold", "gpu_temp_threshold", "disk_temp_threshold", "filesystem_usage_threshold", "filesystem_inode_threshold", "swap_usage_threshold"):
        if merged["alert_hysteresis"] > merged[threshold_key]:
            raise ConfigError("告警恢复回差不得大于告警阈值")
    if merged["ssh_connect_timeout"] > merged["collection_timeout"]:
        raise ConfigError("SSH 连接超时不得大于采集总超时")
    if merged["metric_raw_retention_minutes"] >= merged["metric_mid_retention_hours"] * 60:
        raise ConfigError("原始指标保留时间必须短于中期聚合保留时间")
    if merged["metric_mid_retention_hours"] >= merged["metric_retention_days"] * 24:
        raise ConfigError("中期聚合保留时间必须短于指标总保留时间")
    return cleaned


class ConfigStore:
    def __init__(self, database: Any):
        self.database = database

    def all(self) -> dict[str, Any]:
        values = dict(DEFAULTS)
        for row in self.database.query_all("SELECT key, value FROM settings"):
            values[row["key"]] = json.loads(row["value"])
        return values

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        cleaned = validate_settings(values, self.all())
        with self.database.transaction() as connection:
            for key, value in cleaned.items():
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
        return self.all()

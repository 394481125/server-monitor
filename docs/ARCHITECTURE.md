# 架构说明

## 请求与后台任务

`monitor/app.py` 创建 Flask 应用、数据库、权限、审计、SSH 连接池、采集器和文件服务。`monitor/background.py` 负责周期采集、GPU 调度、告警通知和清理。浏览器只调用 JSON、文件和 WebSocket 接口，不保存远端凭证。

`GET /api/dashboard` 聚合当前用户可见主机的最新采样，同时返回 `resource_summary`、`gpu_users` 和 `idle_gpus`。`GET /api/idle-gpus` 复用同一套 `idle_gpu_rows()` 规则，接受 `min_memory_mib`、`max_utilization`、`max_memory_percent`、`require_no_processes` 和 `host_status` 参数。空闲 GPU 必须有可解析的利用率、总显存和已用显存，且默认没有归属进程；未知采样不会降级为“可用”。

## 凭证与 SSH

主机旧字段 `auth_secret`、`private_key`、`private_key_passphrase`、sudo 密码保持兼容，均由 `SecretBox` 加密。迁移 9 新增 `ssh_keys` 密钥库和 `hosts.ssh_key_id` 引用。`CredentialService` 解析并限制 RSA/ed25519，列表接口只返回名称、类型、公钥和指纹。生成接口使用 cryptography 生成 OpenSSH 格式 RSA 3072/ed25519 私钥，再复用导入路径校验并加密保存；生成接口和导入接口均不返回私钥。浏览器的文件选择器只读取已有私钥文本后调用导入接口，服务器不保留上传临时文件。

启用跳板机后，`SSHClient` 先连接跳板机并校验 `jump_fingerprint`，再调用 `open_channel('direct-tcpip', target)`，把该 channel 作为目标 SSH 的 `sock`。目标指纹仍独立校验。跳板机密文只在服务端解密；连接池 key 包含跳板配置，凭证变更会淘汰旧连接。关闭目标连接时同时关闭 channel 和跳板连接。

公钥推送脚本使用 `umask 077`、`.ssh` 权限设置和 `grep -qxF` 幂等追加。远程执行通过现有 SSH 客户端的 stdin 发送脚本，审计记录执行结果。

## 数据模型

- `hosts`：连接、采集能力、指纹、密钥库引用和跳板机密文。
- `ssh_keys`：密钥密文、公钥、类型、指纹和元数据。
- `command_favorites`：按用户和主机隔离的快捷命令。
- `directory_favorites`：按用户和主机隔离的目录收藏。
- `host_notification_preferences`：按主机覆盖网页提醒、Apprise、总开关和事件列表；没有记录时继承全局设置。
- `host_runtime`、`latest_samples`、`metric_points`：运行状态和分层指标。
- `alerts`、`notifications`、`audit_logs`：告警、外部通知和审计。
- 空闲算力不单独建表：由 `latest_samples.data_json` 的 GPU 快照和 `host_runtime.status` 实时计算，避免缓存过期后继续展示可调度资源。

迁移是连续、前向的，当前版本为 10。数据库初始化先安装幂等基础表，再由 `monitor/migrations.py` 记录版本、名称和校验和。

## 文件边界

`SFTPFileService` 对路径做 POSIX 规范化和符号链接防护。目录列表返回权限模式及 uid/gid。预览仅允许常见文本后缀、普通文件、UTF-8 内容和默认 1 MiB 上限；超过上限或包含 NUL 字节时拒绝。下载、上传、复制继续使用既有 512 MiB 默认上限和断点上传。超限场景返回服务端生成的 `scp`/`rsync` 计划，不执行命令。

权限修改 API 只生成经校验和 shell 引号处理的脚本，并明确 `remote_execution: false`。删除接口需要 elevated 会话，前端也要求二次确认。

文件路由还提供批量下载脚本、批量删除、远端文件名搜索和小文本 diff。批量下载只拼出带 shell 引号的 `ssh ... tar -czf -` 脚本；批量删除在一个 SFTP 会话中复用 `_walk` 并拒绝符号链接；搜索使用固定 `find -maxdepth -type f -name` 命令并限制 500 项；diff 使用远端 `diff -u`，先检查两个文件是普通文本且不超过 1 MiB，再限制输出 256 KiB。接口只返回路径、脚本或受限 diff，不返回大文件内容。

终端工作台是浏览器端会话编排层，不新增凭证或 SSH 协议：页面维护多个会话记录，每个会话仍连接 `/ws/terminal/<host_id>`，权限和并发控制集中在 `routes/sockets.py`。X11 转发未实现，因为 WebSocket/xterm 只承载字符流，不能向浏览器提供 X Server；后端强行开启 X11 channel 也会把图形输出留在服务端桌面。

工具检测保留兼容的 `tools: {name: available|missing}` 响应，并通过 `versions` 返回当前版本、软件源候选版本和安装能力。RustDesk/RustDesktop/ToDesk 被列为人工部署能力，不进入 `installation_command` 的受控系统包白名单。

## 权限与审计

路由通过 `WebContext.login_required` 检查登录、CSRF、权限和二次验证。密钥库、跳板机和远程推送要求 `host.manage` 加 elevated；快捷命令要求 `terminal.open`；文件浏览、预览、脚本计划遵循对应 `files.*` 权限。敏感字段不会进入普通主机响应、导出、日志摘要或浏览器状态。

告警通知在服务端分两路过滤：Apprise 发送由 `NotificationService.notify` 检查主机策略，网页轮询由 `/api/alerts` 返回 `notification_allowed` 后前端决定是否弹出。策略关闭只停止通知，不删除或隐藏告警历史。

“空闲算力”复用 `page.dashboard` 权限，只读，不会触发远端 SSH 或调度；卡片中的“打开终端”仍额外检查 `terminal.open` 和主机 `allow_terminal`。点击主机详情继续遵循 `page.hosts` 的页面权限。

## 参考项目视觉取舍

RackTop 的桌面 UI 使用浅色页面、统计摘要、资源卡片、状态徽标和空状态引导。本项目在 `monitor/static/style.css` 中采用相同的信息层级，但保留密集表格用于告警、审计、文件和权限页面；不引入外部字体、远程图片或桌面壳，不改变服务端安全边界。

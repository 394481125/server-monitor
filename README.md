# Server Monitor

Server Monitor 是一个面向实验室 GPU/Linux 集群的服务端监控与受控运维平台。应用使用 Flask、SQLite、Paramiko 和浏览器端 WebSocket，所有 SSH、采集、终端和文件操作都由后端完成。

## 当前能力

- 主机纳管、SSH 指纹 TOFU、定时采集、GPU/CPU/磁盘/网络/文件系统指标和告警。
- 网页告警与 Apprise 外部通知分别配置事件勾选，并可在“通知”设置中按服务器单独启用/停用、分别控制网页和 Apprise，以及覆盖事件范围；事件包含主机离线、温度、容量、inode、Swap、备份失败及 GPU 健康/调度事件。
- 全局加密 SSH 私钥库：支持网页生成 RSA（3072 位）/ed25519 密钥和 passphrase，也可选择已有私钥文件或粘贴文本导入。主机可引用密钥，接口永不返回私钥。
- 公钥推送向导 API：生成幂等 shell 脚本，或在服务端通过 SSH 执行；不在浏览器运行 SSH。
- 主机专属快捷命令和目录收藏。终端选中文本只进入可编辑收藏，不会自动执行。
- SFTP 浏览、目录收藏、权限/属主展示、受限文本预览、重命名、新建目录、二次确认删除、限额上传下载。
- 文件批量选择：批量删除要求二次确认和 elevated 验证；批量下载只生成本地执行的 `tar.gz`/SSH 脚本，不把大批量数据传入网页。
- 文件名搜索和小文本 diff：搜索在远端执行 `find`，diff 在远端执行 `diff -u`，两者都有路径、结果和输出上限，禁止大文件在线读取。
- 大文件和权限变更只生成 `scp`/`rsync`/`chmod`/`chown` 脚本，不把 GB 级文件读入网页内存，也不直接执行权限脚本。
- 一级“终端工作台”：从统一页面选择主机并打开多个独立 SSH WebSocket 会话；每个会话可单独断开，仍受现有终端权限、elevated、并发和空闲超时控制。
- RackTop 风格的资源发现：集群概览提供主机/GPU/空闲 GPU/活动告警统计；“空闲算力”一级页面按最低可用显存、GPU 利用率、显存占用、主机状态和进程占用筛选，并可直接打开终端或主机详情。
- 资源判定只使用服务端最新可信采样。利用率、显存或主机状态未知时不会把 GPU 标记为可用，默认也会排除存在归属进程的 GPU。
- 工具页显示当前版本和可解析的软件源候选版本；无法锁定时明确标记“未锁定”。RustDesk（含 RustDesktop 别名）/ToDesk 不提供网页一键安装，只标记为人工部署，避免未经审核的第三方远控代理进入服务器。
- 服务端跳板机：目标主机连接通过 Paramiko `direct-tcpip` 中转，采集、终端、SFTP 和运维操作共用该通道。
- 用户、细粒度权限、二次验证、审计日志、数据库备份和数据保留策略。

X11 转发不在浏览器终端能力范围内。`ssh -X` 需要 SSH X11 channel 和用户侧 X Server；浏览器 xterm 只接收字符流，后端开启 X11 也无法把 GUI 窗口渲染到浏览器，因此平台不伪装提供该按钮。需要图形调试时请使用受控跳板机和本地 `ssh -X`，并按组织安全策略授权。

## 与 RackTop 参考项目的取舍

`References-for-other-projects/RackTop-main` 是 React/Tauri/Rust 桌面 GPU 工作台，优势在于浅色卡片式总览、空闲算力筛选、任务工作流和本地桌面体验。本项目吸收了其中适合 Web 监控平台的视觉层级和资源发现模型：浅色背景、统计带、主机/GPU 卡片、状态徽标、空状态提示和独立空闲算力页。

没有迁移 Tauri 桌面壳、本地钥匙串、SSH Agent、本地 SSH Config、项目/数据集同步和桌面通知。这些能力依赖用户本机文件系统或桌面环境，与本项目“凭证、跳板机、采集和远端执行全部在服务端完成”的安全边界冲突。复杂任务编排也继续使用现有受控调度 API，而不是引入另一套任务模型。

## 启动

```bash
cd /home/qq394481125/app/server_monitor
SERVER_MONITOR_INITIAL_PASSWORD='请设置至少 8 位的一次性密码' \
  .venv/bin/python -m flask --app monitor.wsgi run --host 127.0.0.1 --port 8000
```

已有数据目录默认是 `data/`，其中的 `server-monitor.sqlite3` 和 `master.key` 会被原地复用。不要设置临时 `SERVER_MONITOR_DATA_DIR`，也不要删除或重置数据库。生产环境应通过反向代理提供 HTTPS，并限制管理端口访问。

首次登录后必须修改一次性管理员密码。应用会自动执行连续的数据库迁移；启动前会检查数据目录 0700 和主密钥 0600 权限。

## 测试

```bash
.venv/bin/python -m pytest -q
```

测试覆盖迁移、加密凭证、跳板通道、文件安全边界、API 权限以及模拟采集/运维流程。浏览器验收脚本位于 `tests/acceptance/`。

空闲算力的后端规则、接口筛选和总览汇总由 `tests/test_idle_gpu_discovery.py` 覆盖；浏览器验收会登录真实页面、打开“空闲算力”导航、检查筛选控件和桌面宽度不溢出。

更多架构、运维和参考项目取舍见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、[docs/OPERATIONS.md](docs/OPERATIONS.md) 与 [docs/REFERENCE_COMPARISON.md](docs/REFERENCE_COMPARISON.md)。

# Server Monitor

Server Monitor 是一个面向实验室 GPU/Linux 集群的服务端监控与受控运维平台。应用使用 Flask、SQLite、Paramiko 和浏览器端 WebSocket，所有 SSH、采集、终端和文件操作都由后端完成。

## 当前能力

- 主机纳管、SSH 指纹 TOFU、定时采集、GPU/CPU/磁盘/网络/文件系统指标和告警。
- 网页告警与 Apprise 外部通知分别配置事件勾选，并可在“通知”设置中按服务器单独启用/停用、分别控制网页和 Apprise，以及覆盖事件范围；事件包含主机离线、温度、容量、inode、Swap、备份失败及 GPU 健康/调度事件。
- 全局加密 SSH 私钥库：支持网页生成 RSA（3072 位）/ed25519 密钥和 passphrase，也可选择已有私钥文件或粘贴文本导入。主机可引用密钥，接口永不返回私钥。
- 公钥推送向导 API：生成幂等 shell 脚本，或在服务端通过 SSH 执行；不在浏览器运行 SSH。
- 主机专属快捷命令和目录收藏。终端选中文本只进入可编辑收藏，不会自动执行。
- SFTP 浏览、目录收藏、权限/属主展示、受限文本预览、重命名、新建目录、二次确认删除、限额上传下载。
- 大文件和权限变更只生成 `scp`/`rsync`/`chmod`/`chown` 脚本，不把 GB 级文件读入网页内存，也不直接执行权限脚本。
- 服务端跳板机：目标主机连接通过 Paramiko `direct-tcpip` 中转，采集、终端、SFTP 和运维操作共用该通道。
- 用户、细粒度权限、二次验证、审计日志、数据库备份和数据保留策略。

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

更多架构和运维说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

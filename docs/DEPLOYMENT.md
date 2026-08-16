# 部署与运维说明

本文对应当前 Server Monitor 版本。生产原则只有一条：同一数据目录只运行一组 Gunicorn；任何升级、目录迁移或重建虚拟环境都必须先停止旧实例。

## 运行前提

- Ubuntu 22.04/24.04 或其他 Python 3.11+ Linux。
- 数据目录所在文件系统支持 `0700/0600`、`flock` 和 SQLite WAL。
- 服务用户能够读取代码和 `.venv`，只能写入独立数据目录。
- 被管主机启用 SSH；建议使用低权限运维账号和专用密钥。

## 首次安装

```bash
sudo apt update
sudo apt install -y python3 python3-venv openssh-client
cd /opt/server-monitor
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
sudo install -d -m 0700 -o server-monitor -g server-monitor /var/lib/server-monitor
```

配置 systemd 前先复制 `deploy/server-monitor.service.example`，核对 `WorkingDirectory`、`ExecStart`、`User` 和 `ReadWritePaths`。环境文件建议放在 `/etc/server-monitor/environment`，权限 `0600`，至少设置：

```text
SERVER_MONITOR_DATA_DIR=/var/lib/server-monitor
SERVER_MONITOR_BIND=127.0.0.1:8000
SERVER_MONITOR_INITIAL_PASSWORD=首次部署专用长密码
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor
curl http://127.0.0.1:8000/health
sudo systemctl status server-monitor --no-pager
```

首次改密完成后，删除环境文件中的 `SERVER_MONITOR_INITIAL_PASSWORD` 并重启服务。它不会覆盖已有管理员密码。

## 目录迁移与虚拟环境重建

`.venv` 内的脚本记录了创建时 Python 的绝对路径，不能从旧目录复制到 Ubuntu 新目录。迁移时保留数据目录和主密钥，删除的只应是旧虚拟环境：

```bash
sudo systemctl stop server-monitor
pgrep -af 'gunicorn.*monitor.wsgi' || true
cp -a /var/lib/server-monitor /var/lib/server-monitor.backup-$(date +%Y%m%d%H%M%S)
cd /opt/server-monitor
rm -rf .venv
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
node --check monitor/static/app.js
sudo systemctl start server-monitor
curl http://127.0.0.1:8000/health
```

确认旧路径没有残留进程：

```bash
pgrep -af 'gunicorn.*monitor.wsgi' || true
ss -lntp | grep ':8000' || true
```

正式部署应只看到当前目录的一组 master/worker。不要在旧服务仍占用 8000 时启动第二组实例，也不要把旧 `.venv` 临时改名后继续使用。

## 升级流程

1. 通知维护窗口，停止 systemd 服务。
2. 备份 SQLite、`master.key` 和最近备份文件；主密钥必须单独保管。
3. 更新代码和 `requirements.txt`；只有依赖变化时才重新生成 `requirements.lock`。
4. 重建 `.venv`，安装锁定依赖，运行 pytest、compileall、pip check 和前端语法检查。
5. 启动唯一新服务，检查 `/health`、监听端口、日志、SQLite `PRAGMA integrity_check` 和 `PRAGMA user_version`（当前为 `5`）。
6. 登录控制台检查主机列表、最新样本、开发环境页面、告警、权限和数据库容量。

数据库 schema 使用幂等 `CREATE TABLE IF NOT EXISTS` 和迁移版本记录；升级不会删除已有主机、凭据或指标。不要从代码目录复制数据库覆盖正式数据。

## 配置与安全

`SERVER_MONITOR_DATA_DIR` 保存数据库、主密钥、进程锁和默认备份；`SERVER_MONITOR_DATABASE`、`SERVER_MONITOR_MASTER_KEY` 可分别覆盖路径。`SERVER_MONITOR_MAX_UPLOAD_BYTES` 控制 HTTP 请求上限，主机导入接口还限制单文件 2 MiB/100 台；`SERVER_MONITOR_FILE_TRANSFER_LIMIT` 控制 SFTP 单次传输。

默认监听 `127.0.0.1:8000`。通过反向代理提供 HTTPS 时设置 `SERVER_MONITOR_HTTPS=1`，并限制管理网来源。不要把 master key、私钥、sudo 密码、Cookie 或导出文件提交到仓库。

远端工具安装先执行精确 `sudo -n`；只有主机单独保存了加密 sudo 密码且远端明确要求密码时才使用 `sudo -S` 标准输入。`deploy/sudoers.example` 只是最小权限示例，禁止无范围 `NOPASSWD: ALL`。

## 开发环境和系统软件操作

开发环境页面会通过 SSH 读取 GPU 软件栈、Python、conda、uv、APT 包、目录容量和大文件。对所有受管主机启用前，应在权限页分别授权 `page.environments`、`development.view`、`development.plan`、`development.execute`、`diagnostics.view`、`storage.scan` 和（如需 APT 方案）`apt.plan`；主机本身还必须开启“允许安装”。

网页 SSH 执行仅覆盖 venv/conda/uv 的创建、依赖安装和 conda YAML 重建，执行前要求平台密码复核。它是同步操作：输出在命令结束或超时后返回，不是流式终端，也没有网页中止按钮。环境删除仅可生成脚本。

NVIDIA 驱动、CUDA、cuDNN、uv/Miniconda 引导和 APT 方案均可由页面生成，但系统级方案不提供网页执行入口。下载脚本后在目标机上审阅、选择维护窗口并执行；驱动安装通常还需要手动重启。CUDA/cuDNN 包名依赖目标机已配置的 NVIDIA 软件源，平台不会擅自添加第三方源。

开发环境页面只应因 SSH 连接/认证/指纹错误整体失败。工具发现不依赖登录 shell 是否激活环境，会检查常见绝对路径，包括 `/opt/anaconda3`、用户目录 Conda、`/usr/local/cuda*/bin/nvcc` 和用户目录 `uv`。`ubuntu-drivers`、`nvidia-smi`、nvcc、dpkg/cuDNN、ECC、拓扑或 NVLink 探针超时时，接口会保留其他盘点结果；其中 ubuntu-drivers 超过 4 秒只显示“自动推荐值暂不可用”说明，不升级为黄色告警。环境清单通过 `conda-meta`/`pyvenv.cfg` 元数据读取，不执行 `conda env list`、`conda list` 或每个环境的 Python，避免环境很多时超时。若需要人工确认推荐驱动，再在目标机执行 `ubuntu-drivers devices`，不应通过增大全局采集超时掩盖命令卡死。

开发环境页的扫描根目录按主机保存，默认是 `/home/<username>`。切换主机后必须确认页面顶部的“当前目标”和扫描根目录同时更新；如果仍显示上一台主机的路径，应先刷新前端资源并检查是否存在旧 Gunicorn 进程。

## 主机导入与采集

主机管理页提供 CSV/JSON 模板。导入文件字段包含地址、端口、用户、认证方式、标签和功能开关；密码/私钥只在导入文件中临时提供，平台端逐行连接测试后加密保存。导出永远不包含任何凭据，JSON 明确标记 `credentials_included: false`。

采集命令通过 SSH 一次返回 CPU、内存、文件系统容量/inode、磁盘 IO、网络、TCP 汇总、监听端口、GPU、Docker、SMART 和可选温度。`ss`、SMART、sensors 等缺失只产生可选能力错误，不丢弃 CPU/内存等核心指标。原始指标按 15 分钟、中期 6 小时、长期 7 天分层，采集任务只写紧凑摘要。

## 备份、压缩和恢复

设置页显示主库、WAL、总数据库和磁盘可用空间。管理员再认证后执行“清理并压缩”，操作会按保留策略聚合/清理后再 `VACUUM`，期间短暂阻塞写入；同一磁盘应预留接近当前主库大小的临时空间。

同盘备份不能防止磁盘损坏，生产环境应异步复制到其他存储：

```bash
curl http://127.0.0.1:8000/health
sudo systemctl stop server-monitor
cp -a /var/lib/server-monitor/server-monitor.sqlite3 /backup/server-monitor-$(date +%Y%m%d).sqlite3
cp -a /var/lib/server-monitor/master.key /backup/server-monitor-master.key
sudo systemctl start server-monitor
```

恢复前停止服务、保留现场副本、恢复数据库与同一主密钥，再执行 `PRAGMA integrity_check` 和登录测试。没有原 master key 时，数据库中的加密凭据无法恢复，只能重新录入主机凭据。

## 日常检查

```bash
sudo systemctl status server-monitor --no-pager
sudo journalctl -u server-monitor -n 100 --no-pager
curl http://127.0.0.1:8000/health
du -sh /var/lib/server-monitor/*
```

看到数据库或 WAL 增长时，先检查设置页保留策略和磁盘空间，再执行管理员压缩；不要手工删除 `.sqlite3-wal` 或 `.sqlite3-shm`。服务异常时优先读取 request ID、systemd 日志和最近审计记录。

## 完整验收

```bash
.venv/bin/python -m pytest -q
node --check monitor/static/app.js
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
```

Chrome/Playwright 浏览器验收必须使用临时数据目录和临时端口，验收结束后停止临时服务。真实 SSH/WebSocket 验收见 `scripts/ssh_acceptance.py` 和 `scripts/websocket_acceptance.py`。

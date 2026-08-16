# Server Monitor 部署与运维

本文对应 `v1.3.6` 和数据库 schema `6`。生产环境只运行一组 Gunicorn，并把 SQLite、主密钥、日志和 PID 文件放在独立数据目录。

## 前提

- Ubuntu 22.04/24.04 或 Python 3.11+ 的 Linux。
- 平台服务器安装 Python、SQLite、OpenSSH client；被管主机启用 SSH。
- 被管主机使用低权限专用账号。按需授予 Docker、进程、文件、Tmux、安装或压力测试能力。
- 默认监听 `127.0.0.1:8000`。跨机器访问放在 HTTPS 反向代理、VPN 或管理网之后。

## Ubuntu 首次安装

```bash
sudo apt update
sudo apt install -y python3 python3-venv openssh-client
cd /opt/server-monitor
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
SERVER_MONITOR_INITIAL_PASSWORD='至少 10 位的初始密码' bash scripts/start_ubuntu.sh start
```

首次登录使用 `admin`，登录后必须改密码。初始密码只在数据库没有管理员时读取。

## 二次启动和异常恢复

```bash
cd /opt/server-monitor
bash scripts/start_ubuntu.sh start
bash scripts/start_ubuntu.sh status
bash scripts/start_ubuntu.sh restart
bash scripts/start_ubuntu.sh stop
tail -f data/logs/server-monitor.log
```

脚本会校验 PID 对应的 Gunicorn 命令和工作目录，服务健康时拒绝重复启动；异常退出留下过期 PID 文件时只清理自己的文件，不会强杀未知进程。前台排错：

```bash
bash scripts/start_ubuntu.sh foreground
```

## 8000 端口占用

```bash
bash scripts/start_ubuntu.sh status
curl http://127.0.0.1:8000/health
ss -lntp | grep ':8000'
ps -ef | grep '[g]unicorn.*monitor.wsgi'
```

健康服务不要再次 `start`；修改代码用 `restart`。systemd、Docker、脚本只能选择一种管理方式。临时端口：

```bash
SERVER_MONITOR_BIND=127.0.0.1:18000 bash scripts/start_ubuntu.sh restart
```

## Docker 部署

```bash
cp .env.example .env
chmod 600 .env
# 编辑 .env，设置 SERVER_MONITOR_INITIAL_PASSWORD
docker compose up -d --build
docker compose ps
docker compose logs -f server-monitor
```

升级：

```bash
git pull --ff-only
docker compose up -d --build
```

不要执行 `docker compose down -v`，否则会删除保存数据库和主密钥的数据卷。

## systemd 长期运行

```bash
sudo cp deploy/server-monitor.service.example /etc/systemd/system/server-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor
sudo systemctl status server-monitor --no-pager
sudo journalctl -u server-monitor -n 100 --no-pager
```

使用 systemd 后，停止、重启和日志查看都使用 `systemctl/journalctl`，不要并行运行 `start_ubuntu.sh`。

## 远端工具

核心采集不依赖所有可选工具。平台会将缺失工具显示为“未安装”，不会使整台主机采集失败。建议按需安装：`sysstat`、`smartmontools`、`ethtool`、`iproute2`、`lsof`、`jq`；交互工具 `tmux`、`htop`、`ncdu`、`nvtop`、`iotop`、`btop` 等由工具页提供检查和受限安装方案。SMART、iostat、网络和 lsof 读取失败时页面显示降级原因。

### SMART 和内核日志权限

巡检遇到 `Smartctl open device ... Permission denied` 会显示“不可用/无权限”，不会误报磁盘损坏；dmesg 无权限时会尝试读取最近的 `journalctl -k`。需要完整只读结果时，在被管机按实际路径配置最小权限：

```bash
sudo apt-get install -y smartmontools
sudo usermod -aG adm,systemd-journal <ssh-user>
sudo visudo -f /etc/sudoers.d/server-monitor-smart
```

sudoers 文件内容示例（先用 `command -v smartctl` 确认路径，再按发行版调整）：

```text
<ssh-user> ALL=(root) NOPASSWD: /usr/sbin/smartctl -H *, /usr/sbin/smartctl -H -A *
```

保存后重新登录 SSH 会话。没有上述权限时平台仍会继续采集其他指标，只把 SMART 或内核日志标记为降级，不会阻塞整台主机。

## 配置和安全

`SERVER_MONITOR_DATA_DIR` 默认是项目下的 `data/`，保存 SQLite、WAL、主密钥、备份和日志。建议：

- 数据目录 `0700`，数据库、主密钥、`.env` 和日志 `0600`。
- 数据库和同版本 `master.key` 必须一起备份；没有主密钥无法解密远端凭据。
- 不提交 `.env`、`data/`、`.venv/`、私钥、真实密码、Cookie 或导出文件。
- 在设置页配置采集间隔、SSH 并发、连接/采集超时、扫描深度、扫描超时、结果上限、告警阈值和指标保留时间。
- 目录扫描和远端命令都有路径校验、超时、输出上限和部分结果返回；不会运行在线 `badblocks`。

## 升级

升级前停止唯一实例并备份数据库、WAL 和主密钥：

```bash
sudo systemctl stop server-monitor  # 若使用 systemd
cp -a data data.backup-$(date +%Y%m%d%H%M%S)
git pull --ff-only
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
node --check monitor/static/app.js
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
sudo systemctl start server-monitor  # 若使用 systemd
```

启动后检查 `/health` 和 schema：

```bash
curl http://127.0.0.1:8000/health
.venv/bin/python -c "import sqlite3; print(sqlite3.connect('data/server-monitor.sqlite3').execute('PRAGMA user_version').fetchone()[0])"
```

输出应为 `6`，监听端口只能对应一组服务。迁移目录时不要复制旧 `.venv`；保留数据目录和主密钥，重新创建虚拟环境。

## 备份与恢复

```bash
sudo systemctl stop server-monitor
cp -a data/server-monitor.sqlite3 /backup/server-monitor-$(date +%Y%m%d).sqlite3
cp -a data/master.key /backup/server-monitor-master.key
sudo systemctl start server-monitor
```

恢复前停止服务并保留现场副本；恢复数据库和同一主密钥后执行 `PRAGMA integrity_check`。不要手工删除 `.sqlite3-wal` 或 `.sqlite3-shm`。

## 验收

```bash
.venv/bin/python -m pytest -q
node --check monitor/static/app.js
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
git diff --check
```

浏览器验收建议使用临时数据目录和临时端口，结束后停止临时服务。功能取舍见 [FEATURE_ASSESSMENT.md](FEATURE_ASSESSMENT.md)。

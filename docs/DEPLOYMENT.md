# 部署与运维

本文对应当前源码和数据库 schema `7`。

## 运行前提

- Linux、Python 3.11+、OpenSSH client。
- 被管主机开启 SSH，推荐使用权限受限的专用账号。
- Docker 部署需要 Docker Engine 和 Compose v2。
- 浏览器 E2E 仅开发/CI 需要 Node.js 22+ 和 Google Chrome，生产运行不需要 Node。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SERVER_MONITOR_DATA_DIR` | `./data` | SQLite、主密钥、备份、日志和锁文件目录 |
| `SERVER_MONITOR_DATABASE` | 数据目录内数据库 | 可指定 SQLite 文件 |
| `SERVER_MONITOR_MASTER_KEY` | 数据目录内 `master.key` | 可指定主密钥路径 |
| `SERVER_MONITOR_INITIAL_PASSWORD` | 无 | 首次创建管理员时必填，之后不会覆盖密码 |
| `SERVER_MONITOR_BIND` | `127.0.0.1:8000` | Gunicorn 监听地址 |
| `SERVER_MONITOR_HTTPS` | `0` | 反向代理已终止 HTTPS 时设为 `1` |
| `SERVER_MONITOR_MAX_UPLOAD_BYTES` | 512 MiB | 单次 HTTP 请求上限 |
| `SERVER_MONITOR_FILE_TRANSFER_LIMIT` | 512 MiB | 单次文件操作总量上限 |
| `LOG_LEVEL` | `INFO` | 应用和 Gunicorn 日志级别 |
| `SERVER_MONITOR_LOG_LEVEL` | 未设置 | 优先于 `LOG_LEVEL` |

日志继续输出到 stdout/stderr，这是 Docker 和 systemd 的标准采集方式。`start_ubuntu.sh` 会把 Gunicorn 日志写到数据目录。

## Docker

```bash
cp .env.example .env
chmod 600 .env
# 编辑一次性初始密码和 LOG_LEVEL
docker compose up -d --build
docker compose ps
docker compose logs -f server-monitor
```

升级：

```bash
git pull --ff-only
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

不要使用 `docker compose down -v`。

## Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv openssh-client
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
SERVER_MONITOR_INITIAL_PASSWORD='一次性长密码' LOG_LEVEL=INFO bash scripts/start_ubuntu.sh start
```

```bash
bash scripts/start_ubuntu.sh status
bash scripts/start_ubuntu.sh restart
bash scripts/start_ubuntu.sh stop
tail -f data/logs/server-monitor.log
```

systemd：

```bash
sudo cp deploy/server-monitor.service.example /etc/systemd/system/server-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor
sudo systemctl status server-monitor --no-pager
sudo journalctl -u server-monitor -n 100 --no-pager
```

## 数据库迁移

启动时先执行幂等基础建表，再由 `monitor/migrations.py` 按连续版本执行迁移。迁移记录包含版本、名称、应用时间和校验和。

所有待执行迁移在一个 SQLite 写事务中运行：任一步失败都会回滚本批全部变更并拒绝启动。程序也会拒绝打开高于当前支持版本的数据库，防止无意降级。

查看版本和记录：

```bash
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/server-monitor.sqlite3'); print(c.execute('PRAGMA user_version').fetchone()[0]); print(c.execute('select version,name,applied_at from schema_migrations order by version').fetchall())"
```

当前应为 `7`。生产升级前仍应备份，因为事务回滚不能替代人为误操作、磁盘损坏或程序降级时的数据恢复。

## 备份与恢复

平台设置页支持 SQLite 在线备份。主机级备份必须同时保存数据库和主密钥：

```bash
sudo systemctl stop server-monitor
cp -a data/server-monitor.sqlite3 /backup/server-monitor.sqlite3
cp -a data/master.key /backup/master.key
sudo systemctl start server-monitor
```

恢复时停止服务、保留现场副本、恢复同一组数据库和主密钥，再运行：

```bash
.venv/bin/python -c "import sqlite3; print(sqlite3.connect('data/server-monitor.sqlite3').execute('PRAGMA integrity_check').fetchone()[0])"
```

不要手工删除正在使用的 `-wal` 或 `-shm` 文件。

## Worker 与扩容

生产保持一个 Gunicorn Worker。应用内包含唯一后台采集/调度循环和进程锁，SQLite 也不是横向 Web 实例的共享写库。不要通过环境变量或命令行强行增加 Worker。

扩容顺序应是：

1. 将采集、调度、备份和维护任务移到独立 Worker 服务。
2. 使用数据库锁或消息队列保证任务唯一性和幂等性。
3. 将 SQLite 迁移到 PostgreSQL 等多实例数据库。
4. 再增加无状态 Web Worker 和多节点负载均衡。

## SSH 与文件传输

SSH 连接复用由设置页的 `ssh_reuse` 和 `ssh_idle_close` 控制。连接池只保存空闲健康连接，凭据或指纹变化会淘汰旧连接。

断点续传仅覆盖“平台到远端 SFTP”阶段。浏览器重新选择同一文件后，服务会发现匹配的隐藏临时文件并继续写；完成前目标正式文件不存在。修改了文件内容、大小或目标路径会生成新的续传标识。

## GPU 评估远端依赖

```bash
python3 -c "import torch; print(torch.__version__, torch.cuda.is_available())"
python3 -c "import torchvision; print(torchvision.__version__)"
python3 -c "import timm; print(timm.__version__)"  # 仅 ViT-Tiny 需要
nvidia-smi
```

FP8 和 INT8 是否可测取决于 GPU 架构、CUDA 和 PyTorch 构建。真实 CIFAR-10 首次下载由页面显式确认，缓存目录为远端用户的 `~/.cache/server-monitor/datasets`。

## 升级验收

```bash
.venv/bin/python -m pytest -q
node --test tests_js/*.test.js
.venv/bin/python scripts/e2e_acceptance.py
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
curl http://127.0.0.1:8000/health
```

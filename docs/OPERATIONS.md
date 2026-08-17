# 运维手册

## 运行前提

- Linux、Python 3.11+ 和 OpenSSH client。
- 被管主机开启 SSH，推荐使用权限受限的专用账号。
- Docker 部署需要 Docker Engine 和 Compose v2。
- Node.js 与 Chrome 仅用于开发和 CI，生产不需要。

配置项见根目录 [README](../README.md#关键配置)。首次启动必须通过环境变量提供一次性管理员密码；已有数据库不会被该变量覆盖。

## Docker 部署

```bash
cp .env.example .env
chmod 600 .env
# 编辑 SERVER_MONITOR_INITIAL_PASSWORD 和日志级别
docker compose up -d --build
docker compose ps
docker compose logs -f server-monitor
curl http://127.0.0.1:8000/health
```

升级：

```bash
git pull --ff-only
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

不要使用 `docker compose down -v`，除非明确要删除数据卷。

## Ubuntu 部署

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
bash scripts/start_ubuntu.sh foreground
bash scripts/start_ubuntu.sh restart
bash scripts/start_ubuntu.sh stop
tail -f data/logs/server-monitor.log
```

systemd 部署：

```bash
sudo cp deploy/server-monitor.service.example /etc/systemd/system/server-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor
sudo systemctl status server-monitor --no-pager
sudo journalctl -u server-monitor -n 100 --no-pager
```

启动脚本、systemd 和 Docker 只能选择一种进程管理方式。生产始终保持一个 Gunicorn Worker。

## 数据与备份

启动时会自动执行连续数据库迁移。查看当前版本与迁移记录：

```bash
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/server-monitor.sqlite3'); print(c.execute('PRAGMA user_version').fetchone()[0]); print(c.execute('select version,name,applied_at from schema_migrations order by version').fetchall())"
```

设置页可以创建 SQLite 在线备份。主机级备份必须同时保存数据库与主密钥：

```bash
sudo systemctl stop server-monitor
cp -a data/server-monitor.sqlite3 /backup/server-monitor.sqlite3
cp -a data/master.key /backup/master.key
sudo systemctl start server-monitor
```

恢复时停止服务、保留现场副本、恢复同一组文件，再检查：

```bash
.venv/bin/python -c "import sqlite3; print(sqlite3.connect('data/server-monitor.sqlite3').execute('PRAGMA integrity_check').fetchone()[0])"
```

不要手工删除正在使用的 `-wal` 或 `-shm` 文件。

## SSH 与 GPU 验收

SSH 连接池只复用空闲且健康的连接，修改凭据或指纹后旧连接会失效。真实环境验收应使用专用主机：

```bash
.venv/bin/python tests/acceptance/ssh.py
.venv/bin/python tests/acceptance/websocket.py --help
```

GPU 快速评估的远端 Python 环境需要 CUDA 版 PyTorch；ResNet、MobileNet 和 CIFAR-10 需要 `torchvision`，ViT-Tiny 需要 `timm`。

```bash
python3 -c "import torch; print(torch.__version__, torch.cuda.is_available())"
python3 -c "import torchvision; print(torchvision.__version__)"
python3 -c "import timm; print(timm.__version__)"
nvidia-smi
```

FP8、INT8 和 NCCL 能力取决于 GPU、驱动、CUDA 与 PyTorch 构建。短时 loss/accuracy 和 GEMM 峰值只用于链路及横向比较，不是综合性能或模型精度结论。

## 发布与升级验收

日常提交并推送前，脚本会同步远端并执行可用测试：

```bash
bash scripts/update_github.sh "说明本次修改"
```

首次发布到新仓库使用 `scripts/publish_github.sh`。构建源码包和部署包：

```bash
bash scripts/build_release.sh 1.4.0
cd dist
sha256sum -c SHA256SUMS
```

升级前至少执行：

```bash
.venv/bin/python -m pytest -q
node --test tests/frontend/*.test.js
.venv/bin/python tests/acceptance/e2e.py
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
git diff --check
```

升级后检查 `/health`、后台采集线程、数据库版本、最近告警、SSH 指纹状态和一台专用测试主机的采集结果。

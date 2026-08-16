# Server Monitor 命令速查

默认源码目录：

```bash
cd /home/qq394481125/app/server_monitor
```

## Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv openssh-client
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
SERVER_MONITOR_INITIAL_PASSWORD='至少 10 位的初始密码' bash scripts/start_ubuntu.sh start
```

二次启动、异常恢复和查看日志：

```bash
bash scripts/start_ubuntu.sh start
bash scripts/start_ubuntu.sh status
bash scripts/start_ubuntu.sh restart
bash scripts/start_ubuntu.sh stop
bash scripts/start_ubuntu.sh foreground
tail -f data/logs/server-monitor.log
```

## 端口 8000

```bash
curl http://127.0.0.1:8000/health
ss -lntp | grep ':8000'
ps -ef | grep '[g]unicorn.*monitor.wsgi'
SERVER_MONITOR_BIND=127.0.0.1:18000 bash scripts/start_ubuntu.sh restart
```

健康服务已经运行时不要再次 `start`。systemd 和 Docker 管理的服务分别使用 `systemctl` 或 `docker compose`，不要混用。

## Docker

```bash
bash scripts/quick_start.sh
docker compose up -d
docker compose up -d --build
docker compose ps
docker compose logs -f server-monitor
docker compose down
```

不要使用 `docker compose down -v`。

## systemd

```bash
sudo systemctl start server-monitor
sudo systemctl stop server-monitor
sudo systemctl restart server-monitor
sudo systemctl status server-monitor --no-pager
sudo journalctl -u server-monitor -n 100 -f
```

## GitHub

首次上传：

```bash
cd dist/server-monitor-github-v1.0.0
bash scripts/publish_github.sh git@github.com:394481125/server-monitor.git "发布 v1.3.5"
```

日常更新：

```bash
bash scripts/update_github.sh "说明本次修改"
```

构建发布包：

```bash
cd /home/qq394481125/app/server_monitor
bash scripts/build_release.sh v1.3.5
(cd dist && sha256sum -c SHA256SUMS)
```

## 运维检查

```bash
.venv/bin/python scripts/reset_admin_password.py
.venv/bin/python -m pytest -q
node --check monitor/static/app.js
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
git diff --check
```

远端工具安装、Docker 只读信息、systemd/journal、网络诊断、进程资源、环境备份和目录扫描都从页面进入；扫描超时、并发和数据保留在“系统设置”配置。

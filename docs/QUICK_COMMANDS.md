# 常用命令

## Ubuntu 服务

```bash
bash scripts/start_ubuntu.sh start
bash scripts/start_ubuntu.sh status
bash scripts/start_ubuntu.sh restart
bash scripts/start_ubuntu.sh stop
bash scripts/start_ubuntu.sh foreground
tail -f data/logs/server-monitor.log
```

## Docker

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f server-monitor
docker compose restart server-monitor
docker compose down
```

不要执行 `docker compose down -v`。

## 健康与端口

```bash
curl http://127.0.0.1:8000/health
ss -lntp | rg ':8000'
ps -eo pid,ppid,user,args | rg '[g]unicorn.*monitor\.wsgi'
```

## 日志级别

```bash
LOG_LEVEL=DEBUG bash scripts/start_ubuntu.sh foreground
SERVER_MONITOR_LOG_LEVEL=WARNING docker compose up -d --build
```

## 数据库

```bash
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/server-monitor.sqlite3'); print(c.execute('PRAGMA user_version').fetchone()[0])"
.venv/bin/python scripts/reset_admin_password.py --username admin
```

## 测试

```bash
.venv/bin/python -m pytest -q
node --check monitor/static/app_logic.js
node --check monitor/static/app.js
node --test tests_js/*.test.js
.venv/bin/python scripts/e2e_acceptance.py
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
git diff --check
```

## SSH 接受测试

```bash
.venv/bin/python scripts/ssh_acceptance.py --help
.venv/bin/python scripts/websocket_acceptance.py --help
```

## Git 与发布

```bash
bash scripts/update_github.sh "提交说明"
bash scripts/build_release.sh vX.Y.Z
(cd dist && sha256sum -c SHA256SUMS)
```

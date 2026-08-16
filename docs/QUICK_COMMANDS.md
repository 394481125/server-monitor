# Server Monitor 常用命令速查

本文集中记录本项目最常用的启动、排错、更新和打包命令。当前机器可先进入项目目录：

```bash
cd ~/app/server_monitor
```

## Ubuntu 启动与停止

第一次安装依赖：

```bash
sudo apt update
sudo apt install -y python3 python3-venv openssh-client
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
```

第一次启动需要提供一次性管理员初始密码：

```bash
SERVER_MONITOR_INITIAL_PASSWORD='替换为至少10位的初始密码' bash scripts/start_ubuntu.sh
```

以后启动不需要再提供初始密码：

```bash
bash scripts/start_ubuntu.sh
```

不带参数等价于 `start`。服务已经运行时会显示现有 PID 和访问地址，不会重复启动。
脚本还会核对 PID 对应的命令和项目目录；PID 文件损坏或指向其他程序时不会误停止其他进程。

```bash
# 启动
bash scripts/start_ubuntu.sh start

# 查看状态
bash scripts/start_ubuntu.sh status

# 修改代码后重启加载新版本
bash scripts/start_ubuntu.sh restart

# 停止
bash scripts/start_ubuntu.sh stop

# 临时以前台方式运行
bash scripts/start_ubuntu.sh foreground
```

默认访问地址：`http://127.0.0.1:8000`

## 查看日志

```bash
# 持续查看
tail -f data/logs/server-monitor.log

# 查看最近 100 行
tail -n 100 data/logs/server-monitor.log
```

## 8000 端口已被占用

出现 `Connection in use: ('127.0.0.1', 8000)`，通常表示本项目已经启动过，或 systemd、Docker、手工 Gunicorn 正在使用同一端口。

先检查本项目是否已经运行：

```bash
bash scripts/start_ubuntu.sh status
```

如果显示 `healthy`，说明服务已经启动，不需要再次运行 `start`。修改代码后需要加载新版时执行：

```bash
bash scripts/start_ubuntu.sh restart
```

如果脚本提示端口上存在“不受当前 PID 文件管理”的服务，不要直接强制结束未知进程。先检查：

```bash
curl http://127.0.0.1:8000/health
ss -lntp | grep ':8000'
ps -ef | grep '[g]unicorn.*monitor.wsgi'
```

确认进程确实属于本项目后，再使用对应服务的停止方式；systemd 部署应使用 `sudo systemctl stop server-monitor`。

## 修改监听地址

只允许本机访问，这是默认值：

```bash
SERVER_MONITOR_BIND=127.0.0.1:8000 bash scripts/start_ubuntu.sh restart
```

允许局域网访问：

```bash
SERVER_MONITOR_BIND=0.0.0.0:8000 bash scripts/start_ubuntu.sh restart
```

不要把 8000 端口直接暴露到公网。公网部署应通过 Caddy、Nginx 或 Traefik 提供 HTTPS，并限制在可信管理网或 VPN 内。

## systemd 长期运行

```bash
sudo systemctl start server-monitor
sudo systemctl stop server-monitor
sudo systemctl restart server-monitor
sudo systemctl status server-monitor --no-pager
sudo journalctl -u server-monitor -n 100 -f
```

使用 systemd 后，不要同时运行 `scripts/start_ubuntu.sh start`，否则两个服务会争用同一端口和数据目录。

## Docker 启动与更新

```bash
# 第一次启动或重新构建
docker compose up -d --build

# 查看状态
docker compose ps

# 查看日志
docker compose logs -f

# 停止
docker compose down
```

## 更新 GitHub 仓库

当前已经发布到 GitHub 的本地仓库目录：

```bash
cd ~/app/server_monitor/dist/server-monitor-github-v1.0.0
```

日常修改后，只需执行：

```bash
bash scripts/update_github.sh "说明本次修改内容"
```

例如：

```bash
bash scripts/update_github.sh "完善启动管理和扫描进度"
```

该脚本会自动测试、执行 `git add`、提交并推送当前分支。第一次创建全新 GitHub 仓库时才使用 `scripts/publish_github.sh`。

## 生成新的发布包

版本号不能与 `dist/` 中已有版本重复：

```bash
cd ~/app/server_monitor
bash scripts/build_release.sh v1.1.1
```

生成内容：

- `dist/server-monitor-github-v1.1.1/`：完整 GitHub 源码版，包含测试和 CI。
- `dist/server-monitor-deploy-v1.1.1/`：不含测试的快速部署版。
- 两个对应的 `.tar.gz` 压缩包。
- `dist/SHA256SUMS`：压缩包校验值。

## 管理员密码重置

先停止服务，再运行密码重置脚本：

```bash
bash scripts/start_ubuntu.sh stop
.venv/bin/python scripts/reset_admin_password.py
bash scripts/start_ubuntu.sh start
```

更完整的 GitHub 说明见 [GITHUB_GUIDE.md](GITHUB_GUIDE.md)，生产部署说明见 [DEPLOYMENT.md](DEPLOYMENT.md)。

# Server Monitor

Server Monitor 是面向可信内网的 Linux 多机 GPU、主机和受控运维平台。平台服务器通过 SSH 采集，不要求被管主机安装常驻 Agent；数据保存在 SQLite，页面由 Flask/Gunicorn 提供。

当前交付版本：`v1.3.5`，数据库 schema：`6`。

## 快速启动

### Docker

需要 Docker Engine 和 Compose v2：

```bash
git clone git@github.com:394481125/server-monitor.git
cd server-monitor
bash scripts/quick_start.sh
```

脚本会创建权限为 `0600` 的 `.env`、随机初始密码、数据卷并等待健康检查。打开 `http://服务器地址:8000`，账号为 `admin`；首次登录必须修改密码。

已有 `.env` 时的二次启动、异常恢复和升级：

```bash
docker compose up -d
docker compose ps
docker compose logs -f server-monitor
git pull --ff-only
docker compose up -d --build
```

不要使用 `docker compose down -v`，它会删除数据库和主密钥所在的数据卷。

### Ubuntu / Python

```bash
sudo apt update
sudo apt install -y python3 python3-venv openssh-client
cd /opt/server-monitor
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
SERVER_MONITOR_INITIAL_PASSWORD='至少 10 位的初始密码' bash scripts/start_ubuntu.sh start
```

首次启动访问 `http://127.0.0.1:8000`。服务器重启、SSH 会话断开或异常中断后，仍在项目目录执行：

```bash
bash scripts/start_ubuntu.sh start       # 二次启动，已运行时会安全返回
bash scripts/start_ubuntu.sh status      # PID、健康状态、端口和日志
bash scripts/start_ubuntu.sh restart     # 更新代码后重启
bash scripts/start_ubuntu.sh stop
bash scripts/start_ubuntu.sh foreground  # 前台排错
tail -f data/logs/server-monitor.log
```

`SERVER_MONITOR_INITIAL_PASSWORD` 只在首次创建管理员时读取，不会覆盖已有密码。长期运行可使用 `deploy/server-monitor.service.example`，但 systemd 和脚本只能选择一种管理方式。

## 端口占用

看到 `Connection in use: ('127.0.0.1', 8000)` 时不要重复启动：

```bash
bash scripts/start_ubuntu.sh status
curl http://127.0.0.1:8000/health
ss -lntp | grep ':8000'
ps -ef | grep '[g]unicorn.*monitor.wsgi'
```

健康检查通过说明服务已经在运行，代码更新使用 `restart`。若由 systemd 或 Docker 管理，请分别使用 `sudo systemctl restart server-monitor` 或 `docker compose restart server-monitor`。确认归属前不要强杀未知进程。临时端口示例：

```bash
SERVER_MONITOR_BIND=127.0.0.1:18000 bash scripts/start_ubuntu.sh restart
```

## 当前功能

- CPU、iowait、load 1/5/15、内存、Swap、磁盘容量/inode、磁盘 IO、网卡、TCP 和监听端口。
- GPU 利用率、显存、功耗、P-State、当前/应用/默认时钟、风扇、ECC、XID、PCIe 和节流原因；每个 GPU 展示占用进程的 PID、用户、显存、工作目录和命令，概览卡片支持悬停查看。
- 疑似 GPU 残留显存检测、Swap 使用率告警、按 Linux 用户聚合 GPU 卡数/显存/进程数。
- 进程 RSS、Swap、累计读写 IO、父子层级、僵尸状态和受保护的终止操作。
- 主机连通性分级、SSH 指纹确认、硬件资产、只读健康巡检、SMART 权限降级提示和底部平台状态条。
- 告警按当前筛选条件一键忽略提示或软清理，单次最多 1000 条，操作保留审计记录。
- systemd 关键服务只读状态、journal 日志筛选、重启脚本生成；单目标 ping/TCP 端口诊断。
- Docker 容器、镜像、Volume、Compose、Docker info 和容器日志只读查看。
- 开发环境盘点、环境备份脚本、conda YAML、依赖安装后的 `pip check` 冲突提示。
- 目录扫描进度、软超时部分结果、可配置扫描限制、工具检测/安装向导、当前集群 JSON 快照。

Docker 和 systemd 的高危写操作、GPU 残留显存一键清理、任意批量 Shell、网页端口转发和真正断点续传暂不开放，原因与替代方案见 [功能评估](docs/FEATURE_ASSESSMENT.md)。

## GitHub 上传和更新

首次上传新仓库：

```bash
cd /home/qq394481125/app/server_monitor/dist/server-monitor-github-v1.0.0
bash scripts/publish_github.sh git@github.com:394481125/server-monitor.git
```

日常修改在已经有 `.git` 和 `origin` 的源码目录执行：

```bash
bash scripts/update_github.sh "说明本次修改"
```

脚本会运行测试、检查差异、提交并推送当前分支。完整流程、冲突处理和发布标签见 [GitHub 教程](docs/GITHUB_GUIDE.md)。

## 构建轻量发布包

```bash
cd /home/qq394481125/app/server_monitor
bash scripts/build_release.sh v1.3.5
(cd dist && sha256sum -c SHA256SUMS)
```

输出：

- `dist/server-monitor-github-v1.3.5/`：源码、测试、CI 和文档。
- `dist/server-monitor-deploy-v1.3.5/`：不含测试的轻量部署包。
- 两个 `.tar.gz` 和 `dist/SHA256SUMS`。

脚本不会覆盖已有版本目录；请选择新版本号。`.env`、`data/`、`.venv/`、数据库、主密钥和真实凭据不会进入发布包。

## 安全边界

默认绑定 `127.0.0.1:8000`，跨机器访问应放在 HTTPS 反向代理、VPN 或隔离管理网后。远端密码、私钥和 sudo 密码由数据目录主密钥加密保存；数据库和主密钥必须一起备份，不能提交到 GitHub。

所有远程命令都有权限、CSRF、超时和输出上限。高危操作还需要平台密码再认证；前端隐藏按钮不是安全边界。GPU 残留显存只标记疑似异常，不提供自动误杀进程的按钮。

## 验证

```bash
.venv/bin/python -m pytest -q
node --check monitor/static/app.js
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
git diff --check
```

当前本地测试基线为 `110 passed`。第三方 `monitor/static/vendor/xterm/README.md` 是上游说明，不属于项目功能文档。

# Server Monitor

Server Monitor 是面向可信内网的 Linux 与 NVIDIA GPU 主机监控、巡检和受控运维平台。平台通过 SSH 工作，被管主机无需安装常驻 Agent；配置、历史、告警和审计保存在本机 SQLite。

默认监听 `127.0.0.1:8000`，适合单实例部署。跨主机访问应放在 HTTPS 反向代理、VPN 或隔离管理网之后。

## 快速启动

### Docker

```bash
git clone git@github.com:394481125/server-monitor.git
cd server-monitor
bash scripts/quick_start.sh
```

脚本会创建权限为 `0600` 的 `.env` 并生成一次性管理员密码。打开 `http://127.0.0.1:8000`，使用 `admin` 登录并立即修改密码。

```bash
docker compose ps
docker compose logs -f server-monitor
docker compose restart server-monitor
```

不要执行 `docker compose down -v`，它会删除数据库和主密钥所在的数据卷。

### Ubuntu / Python

```bash
sudo apt update
sudo apt install -y python3 python3-venv openssh-client
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
SERVER_MONITOR_INITIAL_PASSWORD='至少 10 位的一次性密码' bash scripts/start_ubuntu.sh start
```

```bash
bash scripts/start_ubuntu.sh status
bash scripts/start_ubuntu.sh restart
bash scripts/start_ubuntu.sh stop
tail -f data/logs/server-monitor.log
```

生产环境可改用 [systemd 示例](deploy/server-monitor.service.example)。启动脚本、systemd 和 Docker 应只选择一种进程管理方式。

## 核心能力

- 采集 CPU、负载、内存、Swap、文件系统、inode、磁盘 IO、网络、TCP、端口及系统限制。
- 采集 GPU 利用率、显存、温度、功耗、时钟、ECC、XID、PCIe、节流状态及计算进程归属。
- 提供主机状态分级、SSH 指纹确认、硬件资产、SMART、内核和 systemd 只读巡检。
- 提供受权限控制的进程、Tmux、终端、工具安装方案、压力任务和 GPU 自动调度。
- 提供 Docker 容器、镜像、Volume、Compose、资源限制和日志的只读视图。
- 提供文件浏览、下载、复制、移动、删除和 SFTP 原子断点续传上传。
- 提供 venv、conda、uv、CUDA、cuDNN、APT 方案、环境备份及 GPU 快速评估。
- 提供告警确认、批量处置、网页/桌面/Server 酱通知、在线备份和历史聚合。

高影响能力默认有意受限：平台不提供任意批量 Shell、Docker 写管理、网页端口转发或 GPU 残留显存一键清理。

## 项目结构

```text
monitor/
  app.py                 应用装配、通用页面和核心 API
  web.py                 请求鉴权、JSON、审计等共享 Web 上下文
  routes/                运维、开发环境、文件和 WebSocket 路由
  services.py            主机、历史、告警、备份领域服务
  collector.py           远端采集命令与结果解析
  operations.py          受限远端运维操作
  development.py         开发环境检查与方案生成
  gpu_benchmark.py       GPU 快速评估脚本与结果解析
  files.py               SFTP 文件管理和断点续传
  ssh_client.py          SSH 客户端、指纹和连接池
  db.py / migrations.py  SQLite 存储与连续迁移
  static/ / templates/   原生前端资源
tests/
  test_*.py              后端、服务和 API 自动测试
  frontend/              Node 前端逻辑测试
  acceptance/            浏览器、SSH 和 WebSocket 接受测试
scripts/                  启动、管理、发布脚本
deploy/                   systemd 和 sudoers 示例
docs/                     架构与运维文档
```

从一次 HTTP 请求入手时，建议按 `monitor/app.py` 或 `monitor/routes/` -> 领域服务 -> `db.py` / `ssh_client.py` 的顺序阅读。详细边界见 [架构说明](docs/ARCHITECTURE.md)。

## 本地开发

运行环境为 Linux、Python 3.11+。前端逻辑测试和浏览器验收需要 Node.js 22+，浏览器验收还需要 `google-chrome`。

```bash
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
SERVER_MONITOR_DATA_DIR=/tmp/server-monitor-dev \
SERVER_MONITOR_INITIAL_PASSWORD='DevelopmentPass123' \
.venv/bin/python -m flask --app monitor.wsgi run --host 127.0.0.1 --port 8000
```

完整自动验证：

```bash
.venv/bin/python -m pytest -q
node --check monitor/static/app_logic.js
node --check monitor/static/app.js
node --check tests/acceptance/browser.js
node --test tests/frontend/*.test.js
.venv/bin/python tests/acceptance/e2e.py
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
git diff --check
```

`tests/acceptance/e2e.py` 使用 `/tmp` 中的隔离数据库和模拟 GPU，不连接生产 SSH 主机。真实 SSH 与 WebSocket 验收必须指向专用测试主机：

```bash
.venv/bin/python tests/acceptance/ssh.py
.venv/bin/python tests/acceptance/websocket.py --help
```

## 关键配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SERVER_MONITOR_DATA_DIR` | `./data` | 数据库、主密钥、备份、日志和锁文件目录 |
| `SERVER_MONITOR_DATABASE` | 数据目录内数据库 | 指定 SQLite 文件 |
| `SERVER_MONITOR_MASTER_KEY` | 数据目录内 `master.key` | 指定主密钥路径 |
| `SERVER_MONITOR_INITIAL_PASSWORD` | 无 | 仅首次创建管理员时必填 |
| `SERVER_MONITOR_BIND` | `127.0.0.1:8000` | Gunicorn 监听地址 |
| `SERVER_MONITOR_HTTPS` | `0` | HTTPS 已在反向代理终止时设为 `1` |
| `SERVER_MONITOR_MAX_UPLOAD_BYTES` | 512 MiB | HTTP 请求体上限 |
| `SERVER_MONITOR_FILE_TRANSFER_LIMIT` | 512 MiB | 单次文件操作总量上限 |
| `SERVER_MONITOR_LOG_LEVEL` | 未设置 | 应用日志级别，优先于 `LOG_LEVEL` |
| `LOG_LEVEL` | `INFO` | 通用日志级别 |

完整部署、备份、恢复、升级和发布步骤见 [运维手册](docs/OPERATIONS.md)。

## 架构约束

`gunicorn.conf.py` 有意保持 `workers = 1`。后台采集、GPU 调度、备份、进程锁和 SQLite 当前属于同一实例；直接增加 Worker 会产生锁冲突或重复后台任务。需要横向扩展时，应先拆出后台任务、引入跨进程协调和共享数据库，再增加无状态 Web Worker。

数据目录必须为 `0700`，数据库、主密钥和 `.env` 必须为 `0600`。数据库中的远端凭据依赖同一个 `master.key` 解密，因此两者必须成组备份。

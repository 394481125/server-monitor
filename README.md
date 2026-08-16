# Server Monitor

Server Monitor 是一个面向可信内网的 Linux 多机监控与运维控制台。平台通过 SSH 采集主机指标，不要求被管主机安装常驻 Agent；服务端使用 Flask、SQLite、Paramiko 和 Gunicorn，前端使用原生 HTML/CSS/JavaScript，内置 xterm.js。

完整功能范围和安全边界见 [需求规格](docs/REQUIREMENTS.md)，生产部署、备份和恢复见 [部署文档](docs/DEPLOYMENT.md)。

当前控制台包含：

- 腾讯云服务器控制台风格的集群概览、主机卡片和主机详情；卡片展示 CPU 核数、CPU/内存、GPU 使用/空闲、物理磁盘和挂载点容量。
- 主机安全纳管、SSH 指纹与 machine-id 校验、JSON/CSV 批量导入、非敏感配置导出、逐台批量 SSH 重测。
- CPU 利用率与 iowait、内存、文件系统容量与 inode、磁盘 IO、网卡速率、TCP 总数/ESTABLISHED/TIME_WAIT、监听端口、GPU、Docker 和 SMART 指标。
- 全局文件系统容量/inode 告警阈值，以及按主机挂载点覆盖的容量和 inode 阈值；告警中心支持服务端筛选、CSV 导出和故障主机聚合。
- GPU 调度、Tmux/Web 终端、当前工作目录进程管理、受控工具安装、压力测试和 SFTP 文件管理。SFTP 支持文件夹上传与目录 ZIP 下载。
- 开发环境页面与主机详情同构标签页：盘点 NVIDIA 驱动、CUDA、cuDNN、Python、conda、uv 和 APT 包；管理 venv/conda/uv 环境、导出/导入 conda YAML、扫描目录容量与大文件。
- 管理员逐项授权页面和操作；普通用户只能隐藏自己已经获授的页面。危险操作需要 CSRF、权限和再认证，并写入审计日志。

## 运行边界

- Python 3.11+ Linux，推荐 Ubuntu 22.04/24.04。
- 按 5～30 台主机、单应用实例、单 Gunicorn worker 设计。
- 数据目录必须支持 Unix 权限、`flock` 和 SQLite WAL，且建议放在本地磁盘。
- Web 终端、进程终止、安装和压力测试拥有远端操作风险，只授权给可信人员。
- HTTP 默认只绑定本机；跨机器访问应放在 HTTPS 反向代理或隔离管理网后。

## Docker Compose 快速部署

这是公开仓库用户最省事的启动方式。需要已安装 Docker Engine、Docker Compose v2 和 `openssl`：

```bash
git clone https://github.com/<你的用户名>/<仓库名>.git
cd <仓库名>
bash scripts/quick_start.sh
```

脚本会自动生成权限为 `0600` 的 `.env` 和随机初始密码，构建容器，等待健康检查并打印登录信息。也可以手动复制 `.env.example`、填写密码后执行下面的 Compose 命令：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f server-monitor
```

健康状态变为 `healthy` 后访问 `http://服务器地址:8000`，用户名是 `admin`。首次登录会要求修改密码；改密完成后应删除 `.env` 中的 `SERVER_MONITOR_INITIAL_PASSWORD`，再执行 `docker compose up -d`。数据库和 `master.key` 保存在 Docker 命名卷 `server-monitor-data` 中，删除容器不会丢失，禁止使用 `docker compose down -v`，除非明确要删除全部业务数据。

升级代码：

```bash
git pull --ff-only
docker compose up -d --build
```

正式公网访问不要直接暴露 8000 端口，应使用 Caddy、Nginx 或 Traefik 提供 HTTPS，并把平台限制在可信管理网或 VPN 内。详细 systemd、备份和恢复流程见 [部署文档](docs/DEPLOYMENT.md)。

## Ubuntu 快速启动

在项目根目录执行：

```bash
sudo apt update
sudo apt install -y python3 python3-venv openssh-client
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check

export SERVER_MONITOR_DATA_DIR="$PWD/data"
export SERVER_MONITOR_INITIAL_PASSWORD='Replace-With-A-Long-Initial-Password'
export SERVER_MONITOR_BIND=127.0.0.1:8000
.venv/bin/python -m gunicorn -c gunicorn.conf.py monitor.wsgi:app
```

另开终端确认：

```bash
curl http://127.0.0.1:8000/health
```

返回 `{"background":true,"status":"ok"}` 后访问 `http://127.0.0.1:8000`。首次登录用户是 `admin`，首次登录必须改密码。`SERVER_MONITOR_INITIAL_PASSWORD` 只在第一次创建管理员时读取，不会覆盖已有密码。

忘记已改过的管理员密码时，停止服务后运行：

```bash
.venv/bin/python scripts/reset_admin_password.py
```

## 构建、迁移和停止

项目没有 npm 或前端打包步骤，`monitor/static` 和 `monitor/templates` 由 Flask 直接提供。生产构建就是重建项目专用虚拟环境、安装锁定依赖、准备独立数据目录并启动一个 Gunicorn 实例。

代码目录迁移到 Ubuntu 后必须先停服务，再删除并重建旧 `.venv`；虚拟环境中的入口脚本包含旧解释器绝对路径，不能跨目录复制。数据库、`master.key` 和备份目录属于业务数据，不随 `.venv` 删除。

```bash
sudo systemctl stop server-monitor
rm -rf .venv
python3 -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

systemd 示例见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) 和 `deploy/server-monitor.service.example`。升级时始终遵循“停止旧进程 -> 备份数据库和主密钥 -> 修改代码/环境 -> 测试 -> 只启动一组新进程”的顺序。

## requirements.txt 与 requirements.lock

`.txt` 和 `.lock` 都是 pip 可读取的纯文本文件，扩展名不改变 pip 行为。项目约定如下：

| 文件                  | 作用                                                                |
| --------------------- | ------------------------------------------------------------------- |
| `requirements.txt`  | 直接依赖声明，开发者在新增或升级依赖时编辑。                        |
| `requirements.lock` | 直接依赖及传递依赖的确定版本，用于部署、CI 和验收，保证环境可复现。 |

没有依赖变更时不要为了改名而替换锁文件。变更依赖时先在临时环境安装 `requirements.txt`，再用 `pip freeze` 生成新的 `requirements.lock`，重建 `.venv` 后运行 `pip check` 和完整测试。

## 主机批量管理

“主机管理”支持 CSV 模板、JSON 模板、CSV/JSON 导入和 CSV/JSON 配置导出。导入单次最多 100 台、文件最多 2 MiB；每一行都单独执行 SSH 连接测试、读取服务端指纹和 machine-id，再决定是否纳管，返回逐行成功/失败结果。

导出只包含可重新编辑的非敏感配置。密码、私钥、私钥口令、sudo 密码不会写入导出内容；JSON 带有 `credentials_included: false`。导入凭据只在 HTTPS 或可信内网传输，平台仍会加密存储。

选中主机后可执行“批量 SSH 重测”。结果会区分正常、指纹不一致、物理身份变化、重复节点和连接失败。批量重测绝不自动接受新指纹；指纹变更必须逐台重新认证并确认，无法确认时应删除旧记录后重新纳管。

## 指标与告警

远端采集命令使用 `/proc`、`df -P`、`df -Pi` 和可选 `ss`。首个 CPU 样本只建立计数器，第二个样本开始计算利用率和 iowait；`ss` 不存在时核心采集仍成功，但主机显示可选能力降级。数据库只保存 TCP 汇总和最多 256 个监听端口展示项，不保存完整连接表。

系统设置中的文件系统容量/inode 阈值是全局默认值。主机详情“存储与网络”页可为 `/`、`/data` 等挂载点配置独立覆盖；规则为空时继承全局值。告警使用现有连续样本和恢复回差，避免单次抖动触发。告警中心支持主机、事件类型、状态、级别、时间范围和摘要搜索，CSV 导出使用同一组服务端过滤条件；故障主机聚合显示离线、指纹异常、采集降级、繁忙和活动资源告警。

系统日志 `dmesg/journalctl`、任意用户自定义脚本、长期进程告警和 GPU 泄漏预测本版本不做周期落库：它们会扩大远程代码执行面、权限边界或数据库容量。需要临时查看时使用受控 Web 终端，后续若实现必须先设计权限、限流和独立保留策略。

## 文件与远程运维

文件管理通过保存的 SSH 凭据打开 SFTP，不自动使用 sudo。支持绝对路径浏览、文件/文件夹上传、文件/目录下载（目录自动 ZIP）、新建目录、复制、移动、重命名和递归删除。符号链接下载/复制和目录自复制会被拒绝，删除要求再认证。

Tmux/Web 终端必须由 xterm.js 解释 ANSI/VT 控制序列；SSH 字节流按 UTF-8 增量解码，Tmux 附着时优先设置远端 UTF-8 locale。进程表包含当前工作目录；无法读取时显示不可用而不是伪造路径。

## 开发环境与系统软件

“开发环境”页面以及主机详情中的同名标签页使用 SSH 按需盘点目标机，不安装常驻 Agent。它显示 Debian/Ubuntu 发行版、NVIDIA 驱动及 `ubuntu-drivers` 推荐包、`nvcc`、已安装 cuDNN 包、可用 Python 3、conda、uv 和已安装 APT 包。

工具发现先查远端 `PATH`，再查常见绝对路径：包括 `$HOME/miniconda3`、`$HOME/anaconda3`、`/opt/anaconda3`、`/opt/conda`、`/usr/local/cuda*/bin/nvcc` 和用户目录中的 `uv`。因此即使登录 shell 没有激活 conda，页面仍会显示实际安装位置，并且创建/导出操作使用该绝对路径。环境清单不执行容易卡住的 `conda env list` 或 `conda list`，而是读取已知 Conda 根目录、`~/.conda/environments.txt`、`conda-meta` 和 `pyvenv.cfg`；这也避免在环境很多的主机上因逐个启动 Python 而超时。

驱动推荐、`nvidia-smi`、nvcc、cuDNN、ECC、拓扑和 NVLink 都是可选探针，并在远端分别设置硬超时。某个工具卡住时只跳过该项并保留其他字段；`ubuntu-drivers` 超过 4 秒时显示“自动推荐值暂不可用”说明，不作为整页告警。`nvidia-smi` 返回错误时不会把错误文本伪装成驱动版本。SSH 连接本身失败时仍返回真实连接错误。

虚拟环境支持 `venv`、conda、uv。目标主机缺少对应 Python、conda 或 uv 时，创建选项会禁用；可以先生成 uv 或 Miniconda 的固定方案。创建环境、安装受限格式的依赖和使用 YAML 重建 conda 环境可在当前平台密码复核后通过 SSH 同步执行，远端输出在执行结束后返回。删除环境始终只生成带路径校验的脚本，避免网页误删。

GPU 驱动、CUDA、cuDNN、Miniconda 和 APT 的 `update`、`upgrade`、`autofix`、`install`、`remove`、`purge` 只生成固定模板脚本，不从网页直接执行系统级改动。驱动、内核、软件源和全局包失败时可能破坏主机可用性，必须由管理员在目标机审阅并运行脚本；本版本没有伪装成实时流输出或网页停止按钮。

GPU 健康自检读取 `nvidia-smi` 的 GPU 枚举、温度、显存、利用率、ECC、拓扑和 NVLink 状态。显存碎片只有 PyTorch 等运行进程中的 allocator 才能准确提供，`nvidia-smi` 不具备该指标；该说明只在点击 GPU 自检后返回，不以猜测数据告警。高利用率但低显存仅给出排查提示，不代表训练必然异常。

开发环境页的扫描根目录按主机保存，默认分别为 `/home/<username>`。切换目标主机后，当前目标、扫描目录、虚拟环境默认路径和磁盘扫描路径都会同步切换，不能继续沿用上一台主机的路径。

## 数据库容量

默认保留策略：原始指标 15 分钟、中期聚合 6 小时、长期指标 7 天、采集任务摘要 60 分钟、审计/通知/完成记录 30 天。后台每 5 分钟聚合和清理；主机最新完整采样只保留一份。管理员可在设置页再认证后执行“清理并压缩”，不要手工删除 SQLite WAL/SHM 文件。

备份目录默认在数据目录下，同盘备份不能防止磁盘故障；生产环境应把备份复制到另一块磁盘或其他受控存储。数据库和主密钥必须分别备份，丢失主密钥后加密凭据无法恢复。

## 测试与验收

```bash
.venv/bin/python -m pytest -q
node --check monitor/static/app.js
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
```

真实 SSH 和 WebSocket 脚本分别见 `scripts/ssh_acceptance.py`、`scripts/websocket_acceptance.py`。浏览器验收使用本机 Chrome + Playwright，必须在隔离数据目录和临时端口运行，完成后停止隔离服务。

## 发布到 GitHub

当前目录需要先初始化为真正的 Git 仓库，然后推送到你在 GitHub 创建的空仓库：

```bash
git init
git add .
git commit -m "Initial release"
git branch -M main
git remote add origin git@github.com:<你的用户名>/<仓库名>.git
git push -u origin main
```

公开前必须完成以下事项：选择并添加开源许可证（常见选择是 MIT、Apache-2.0 或 GPL-3.0）；确认 `git status` 中没有 `.env`、数据库、`master.key`、SSH 私钥或真实服务器信息；在 GitHub 仓库设置中启用 Issues，并把 GHCR 包设为 public。仓库已包含 CI，推送和 Pull Request 会自动运行测试并构建容器；创建 `v1.0.0` 这类 tag 后会自动发布 `ghcr.io/<你的用户名>/<仓库名>:v1.0.0` 和 `latest` 镜像。

```bash
git tag v1.0.0
git push origin v1.0.0
```

许可证代表你允许别人如何使用、修改和再发布代码，必须由项目所有者明确选择，因此仓库不会自动替你写入某一种许可证。

源码仓库应保留 `tests/`、`.github/workflows/` 和开发文档，它们是项目质量与可维护性的组成部分。一次命令可以同时生成 GitHub 源码上传版和不含测试的部署版：

```bash
bash scripts/build_release.sh v1.0.0
```

输出位于 `dist/server-monitor-github-v1.0.0/`（完整源码，可上传 GitHub）和 `dist/server-monitor-deploy-v1.0.0/`（快速部署），两者都有 `.tar.gz` 压缩包，并生成 `dist/SHA256SUMS`。重复生成时执行 `bash scripts/build_release.sh v1.0.0 --force`；`dist/` 已被 Git 忽略。推送 `v1.0.0` tag 时，GitHub Actions 会自动创建 GitHub Release并发布 GHCR 容器镜像。

在 GitHub 创建空仓库后，也可以从源码上传目录一条命令完成初始化和推送（需要先配置 Git 用户信息及 GitHub SSH/HTTPS 凭据）：

```bash
cd dist/server-monitor-github-v1.0.0
bash scripts/publish_github.sh git@github.com:<你的用户名>/<仓库名>.git
```

## 文件清理边界

不要删除 `monitor/`、`tests/`、`scripts/`、`deploy/`、`docs/`、`requirements.txt`、`requirements.lock`、`data/` 或 `master.key`。`.venv`、`__pycache__`、`.pytest_cache` 和 `*.pyc` 可以重建或清理；第三方 `monitor/static/vendor/xterm/README.md` 是许可证/供应商说明，除非升级 xterm，否则保持原文。

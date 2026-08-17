# Server Monitor

Server Monitor 是面向可信内网的 Linux 主机、NVIDIA GPU 和受控远程运维平台。平台通过 SSH 采集和执行受限操作，被管主机无需安装常驻 Agent；状态、历史、审计和配置保存在 SQLite。

当前源码数据库 schema 为 `7`。版本号由发布时的 Git 标签和 `scripts/build_release.sh <version>` 参数确定，文档不再绑定历史发布包版本。

## 快速启动

### Docker

```bash
git clone git@github.com:394481125/server-monitor.git
cd server-monitor
bash scripts/quick_start.sh
```

脚本会生成权限为 `0600` 的 `.env` 和一次性管理员密码。打开 `http://127.0.0.1:8000`，使用 `admin` 登录并立即修改密码。

常用命令：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f server-monitor
docker compose restart server-monitor
```

不要执行 `docker compose down -v`，该命令会删除数据库和主密钥所在的数据卷。

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
bash scripts/start_ubuntu.sh foreground
tail -f data/logs/server-monitor.log
```

长期运行建议使用 [systemd 示例](deploy/server-monitor.service.example)。脚本、systemd 和 Docker 只能选择一种进程管理方式。

## 核心能力

- CPU、iowait、load、内存、Swap、文件系统容量和 inode、磁盘 IO、网卡、TCP、监听端口和系统限制。
- GPU 利用率、显存、温度、功耗、风扇、P-State、时钟、ECC、XID、PCIe、节流原因和计算进程归属。
- 主机状态分级、SSH 指纹确认、硬件资产、SMART、内核和 systemd 只读巡检。
- 进程、Tmux、受限终端、工具安装方案、压力任务、GPU 自动调度和审计。
- Docker 容器、镜像、Volume、Compose、资源限制和日志只读查看。
- 文件浏览、下载、复制、移动、删除和 SFTP 原子断点续传上传。
- venv、conda、uv、CUDA、cuDNN、APT 方案、环境备份和 CIFAR-10 GPU 快速评估。
- 告警确认、软清理、批量处置、网页/桌面/Server 酱通知、SQLite 在线备份和历史聚合。

## GPU 快速评估

入口位于“开发环境 / GPU 快速评估”。运行需要 `gpu.benchmark` 权限、主机启用“允许压力任务”，并通过平台密码二次验证。

矩阵与系统指标：

- 严格 FP32、TF32、FP16、BF16。
- 硬件和 PyTorch 支持时测试 FP8 E4M3、FP8 E5M2 和 INT8；不支持的精度会记录警告，不伪造结果。
- 单卡/逐卡 GEMM 吞吐、显存拷贝带宽、温度、功耗和时钟快照。
- 多卡 DataParallel 训练吞吐、NCCL All-Reduce 算法带宽和总线带宽。
- 多卡结果显示 TP degree；`TP=8 ready` 只表示卡数条件满足，不代表某个模型框架的 Tensor Parallel 已完成优化。

训练模型：`ResNet-18`、`ResNet-34`、`ResNet-50`、`MobileNetV3-Small` 和 `timm` 的 `vit_tiny_patch16_224`。

数据源：

- `synthetic`：数据常驻 GPU，适合比较纯训练吞吐。
- `fake_cifar10`：使用 torchvision FakeData，覆盖轻量 DataLoader 到训练链路。
- `cifar10`：使用真实 CIFAR-10。默认只读远端缓存；首次下载必须在页面显式允许。

远端 Python 环境必须安装 CUDA 版 PyTorch；ResNet/MobileNet/CIFAR 需要 `torchvision`，ViT-Tiny 需要 `timm`。短时 `loss/acc` 用于执行链路和横向比较，不能替代完整收敛训练。

## 可靠性改进

- SQLite 使用连续版本迁移、名称和校验和记录；所有待执行迁移在一个 `BEGIN IMMEDIATE` 事务内完成，失败会整体回滚。
- SSH 连接池按主机和凭据隔离，只复用空闲健康连接；凭据变化、超时、连接异常和应用退出都会淘汰连接。
- SFTP 上传写入同目录隐藏临时文件，重试时按目标路径、文件大小和首块指纹续传，完成后原子重命名。
- `LOG_LEVEL` 或 `SERVER_MONITOR_LOG_LEVEL` 支持 `DEBUG/INFO/WARNING/ERROR/CRITICAL`，默认 `INFO`。
- 前端纯逻辑由 Node 内置测试覆盖；真实 Chrome E2E 覆盖认证、模拟 8 卡 ResNet-50/CIFAR-10 评估、历史渲染、设置页和退出。

## 单 Worker 说明

`gunicorn.conf.py` 有意保持 `workers = 1`。当前后台采集器、调度器、备份任务、进程锁和 SQLite 都属于单实例架构；直接增加 Worker 会造成启动锁冲突或重复后台任务，不是有效扩容。

Web 请求和 SSH 工作主要是 IO 密集型，当前使用 gevent 和受控线程池。需要多 Worker 或横向扩展时，必须先把后台任务拆成独立进程、引入跨进程任务协调，并把数据库迁移到支持多实例写入的服务。详见 [架构说明](docs/ARCHITECTURE.md)。

## 安全边界

- 默认监听 `127.0.0.1:8000`；跨主机访问应置于 HTTPS 反向代理、VPN 或隔离管理网后。
- 数据目录必须为 `0700`；数据库、主密钥和 `.env` 必须为 `0600`。数据库和同一 `master.key` 必须一起备份。
- 远端操作受权限、CSRF、超时、输出上限和审计保护；高影响操作需要二次验证。
- Docker 写操作、任意批量 Shell、GPU 残留显存一键清理和网页端口转发仍不开放。

## 验证

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

E2E 需要 Node.js 22+ 和 `google-chrome`。完整测试说明见 [TESTING.md](docs/TESTING.md)，部署、功能取舍和需求基线分别见 [DEPLOYMENT.md](docs/DEPLOYMENT.md)、[FEATURE_ASSESSMENT.md](docs/FEATURE_ASSESSMENT.md) 和 [REQUIREMENTS.md](docs/REQUIREMENTS.md)。

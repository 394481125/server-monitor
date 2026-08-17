# 当前需求与验收基线

## 产品定位

平台面向可信内网的 Linux/GPU 服务器监控和受控运维。平台通过 SSH 工作，不在远端安装常驻 Agent，不提供通用远程代码执行器、容器编排或公网暴露方案。

## 功能需求

| 模块 | 当前要求 |
| --- | --- |
| 主机采集 | CPU、负载、内存、Swap、温度、文件系统、inode、磁盘 IO、网卡、TCP、监听端口和系统限制 |
| GPU 监控 | 利用率、显存、功耗、温度、风扇、P-State、时钟、ECC、XID、PCIe、节流、compute mode 和进程归属 |
| GPU 评估 | 单/多卡；FP32/TF32/FP16/BF16/FP8/INT8；显存带宽；ResNet/MobileNet/ViT；合成/FakeData/真实 CIFAR-10；NCCL 和 TP=8 条件 |
| 主机运维 | SSH 指纹、Tmux、终端、进程、工具、压力任务、GPU 调度、健康巡检和审计 |
| 文件 | 浏览、下载、上传、复制、移动、删除；上传支持远端 SFTP 原子断点续传 |
| Docker | 容器、镜像、Volume、Compose、资源限制和日志只读查看 |
| 开发环境 | Python、venv、conda、uv、CUDA、cuDNN、APT 方案、备份和依赖冲突检查 |
| 告警 | 连续样本、恢复回差、通知间隔、确认、软清理、批量处置、网页/桌面/Server 酱 |
| 数据 | SQLite WAL、raw/mid/long 指标、在线备份、压缩、连续版本迁移和升级失败回滚 |
| 权限 | 页面和动作权限、CSRF、二次验证、主机能力开关、输出/超时限制和 before/after 审计 |

## 非功能需求

- 数据目录 `0700`，数据库和主密钥 `0600`。
- 默认绑定回环地址；远程访问通过 HTTPS/VPN/管理网。
- SSH 连接池不得并发共享同一租约；凭据变化和异常连接必须失效。
- SFTP 正式目标只能在完整传输后出现，续传临时文件不得显示在文件列表。
- 数据库迁移必须连续、有记录、有校验和并在失败时整体回滚。
- GPU 评估必须受专门权限、主机压力能力和二次验证保护，并持久化审计与历史。
- 日志级别必须可配置；生产日志仍输出到运行环境标准流。
- 前端逻辑必须可由 Node 测试，关键登录流程必须由真实浏览器 E2E 覆盖。

## 明确不实现

- 在现有单实例架构中直接增加 Gunicorn Worker。
- Docker 启停、删除、exec、镜像构建和 Volume 写管理。
- 任意批量 Shell、网页端口转发和 GPU 残留显存自动清理。
- 将短时 CIFAR-10 accuracy 或 GEMM 峰值包装成单一“综合 GPU 分数”。

## 验收命令

```bash
.venv/bin/python -m pytest -q
node --check monitor/static/app_logic.js
node --check monitor/static/app.js
node --check scripts/browser_acceptance.js
node --test tests_js/*.test.js
.venv/bin/python scripts/e2e_acceptance.py
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
git diff --check
```

真实 SSH 环境另运行 `scripts/ssh_acceptance.py` 和 `scripts/websocket_acceptance.py`。无真实 GPU 时，GPU 服务层、权限、命令生成、解析、持久化和页面逻辑使用模拟结果验收；硬件性能数值必须在目标 GPU 主机上执行。

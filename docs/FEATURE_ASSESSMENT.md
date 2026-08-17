# 架构与功能需求评估

更新日期：2026-08-17。本文记录本轮需求的必要性、实现结论和保留边界。

| 需求 | 必要性 | 结论 |
| --- | --- | --- |
| Gunicorn 多 Worker | 当前不必要且不能直接启用 | 保持单 Worker。后台任务、进程锁和 SQLite 尚未拆分，多 Worker 会冲突或重复执行。 |
| 正式数据库迁移 | 必要 | 已实现连续版本、名称、校验和、单事务顺序执行、失败整体回滚和高版本拒绝降级。 |
| 前端单元测试 | 必要 | 已抽出 `app_logic.js`，使用 Node 内置测试覆盖转义、格式化、筛选和 GPU 结果归一化。 |
| 浏览器 E2E | 必要 | 已使用真实 Headless Chrome 覆盖认证、模拟 8 卡 GPU 评估、历史渲染、设置页和退出。 |
| 日志级别配置 | 必要 | 已支持 `LOG_LEVEL` 和 `SERVER_MONITOR_LOG_LEVEL`；stdout/stderr 保持由运行环境收集。 |
| SSH 连接池 | 必要 | 已实现线程安全的空闲连接复用、健康检查、空闲淘汰、容量上限和凭据变化失效。 |
| SFTP 断点续传 | 必要 | 已实现隐藏临时文件、内容标识、续写、大小校验和完成后原子重命名。 |
| Docker 写操作 | 当前不必要 | 保持只读。Docker socket 通常等价 root，启停/删除/exec 会明显扩大平台破坏范围。 |
| `app.py` 蓝图拆分 | 有价值但非本轮阻塞项 | 当前约 1700 行，已低于原评估的 2500 行；先为新增域保持独立服务模块，待路由契约稳定后渐进拆分。 |
| SSH 层单元测试 | 必要 | 已使用模拟客户端覆盖复用、凭据变化、超时淘汰；真实 SSH 继续由接受测试覆盖。 |
| GPU 单卡/多卡快速评估 | 必要 | 已实现精度、显存、训练、NCCL、遥测、历史和权限控制。 |

## 为什么不直接增加 Worker

单 Worker 不是简单的性能配置遗漏。当前 `create_app()` 会持有数据目录进程锁并启动后台采集、GPU 调度、维护和备份线程。多个 Gunicorn Worker 会争用同一个锁；移除锁又会让每个 Worker 重复调度任务。

此外 SQLite 虽支持 WAL 和多读者，但不适合作为多节点后台调度系统的协调数据库。因此本轮正确处理是保持一个 Web Worker，并复用 IO 连接和控制并发。真正扩容方案见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 为什么 Docker 保持只读

启动、停止、重启、删除、exec、镜像构建和 Volume 修改都可能中断训练或删除数据。若未来实现，至少需要：

- 独立的 `allow_docker_manage` 主机能力和细分权限。
- 二次验证、对象白名单、状态前置条件和完整审计。
- 对停止、删除和 Volume 操作增加审批或双人复核。
- 远端最小 sudoers，而不是把用户直接加入等价 root 的 Docker 组。

在这些条件出现前，本地 Docker CLI 或受控 SSH/Tmux 是更清晰的边界。

## GPU 评估指标

已实现：

- FP32、TF32、FP16、BF16、FP8 E4M3/E5M2 和 INT8 GEMM。
- 显存设备到设备拷贝带宽。
- ResNet-18/34/50、MobileNetV3-Small、ViT-Tiny 训练 `it/s`、`images/s`、平均 loss 和平均 accuracy。
- 合成数据、FakeData-CIFAR10 和真实 CIFAR-10。
- 多卡 DataParallel、NCCL All-Reduce 算法/总线带宽、TP degree 和 TP=8 卡数条件。
- 测试前后温度、功耗、SM/显存时钟、利用率和显存占用。

不应伪造为通用分数的项目：

- `TP=8 ready` 只表示有 8 张可用 GPU，不代表 Megatron-LM、DeepSpeed、FSDP 或具体模型 TP 已调优。
- 短时 CIFAR-10 loss/accuracy 不代表完整模型精度。
- GEMM 峰值不等于端到端训练速度；数据加载、通信、算子覆盖和模型结构都会改变结果。
- FP8/INT8 失败可能来自硬件、PyTorch 或 CUDA 不支持，应作为能力缺失显示。
- 训练依赖或数据集不可用时保留已完成的矩阵、显存和 NCCL 结果，并明确标记训练测速未完成。

未来只有在存在明确使用场景时再增加：Transformer 模型 tokens/s、H2D/D2H PCIe 带宽、P2P 拓扑矩阵、长时间热稳定性、功耗效率（性能/瓦）和特定框架的 TP/FSDP 基准。

## 暂缓项

- 任意批量 Shell 和命令模板。
- GPU 残留显存一键清理。
- 浏览器本地端口转发。
- 在线破坏性磁盘测试。
- 任意 systemd 重启和 Docker 写管理。

这些功能不是无法开发，而是需要额外授权模型、审批、回滚和故障恢复，当前收益不足以覆盖风险。

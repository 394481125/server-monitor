# Server Monitor 需求规格与实现边界

版本：`v1.3.6`
更新日期：2026-08-17
数据库 schema：`6`
状态：与当前源码、自动化测试和浏览器验收一致

## 1. 产品范围

平台面向可信内网的 Linux 多机 GPU 监控和受控运维。平台服务器通过 SSH 执行只读采集与受限命令，不在被管主机部署常驻 Agent，不开放远程 Docker API，不做 k8s、Swarm 或跨主机容器编排。

所有远端操作都受权限、CSRF、超时、输出上限和审计保护。高危操作需要平台密码再认证；只读需求优先通过脚本输出完成。

## 2. 已实现需求

| 模块 | 交付内容 |
| --- | --- |
| 基础资源 | CPU 使用率/iowait、load 1/5/15、内存、Swap 使用率、上下文切换/中断速率、网卡、TCP、监听端口、磁盘 IO、inode |
| 进程 | RSS、Swap、累计读写 IO、父子深度、僵尸状态、工作目录、PID 启动时间校验；GPU 进程记录所属卡、PID、用户、显存、工作目录、完整命令和 PID 是否存在，概览卡片点击单块 GPU 展开/收起 |
| GPU | 利用率、显存、温度、功耗/功耗上限、P-State、当前/应用/默认时钟、风扇、ECC、XID、PCIe Gen/宽度、节流原因、compute mode |
| GPU 告警 | 功耗、风扇、ECC、XID、PCIe、节流、Swap 使用率和疑似残留显存，支持恢复回差和通知事件开关 |
| 告警处置 | 单条忽略/软清理；按当前筛选结果批量忽略提示或软清理，单次最多 1000 条；可关闭红点、网页/桌面弹窗和 Server 酱发送但继续保留历史，权限、CSRF 和审计完整保留 |
| 主机状态 | SSH 网络不通、认证失败、采集超时、命令失败、GPU 采集失败、指纹异常等细分状态和简短失败原因 |
| 诊断 | SMART、只读健康巡检、nvidia-persistenced、NVIDIA 模块、nouveau、Secure Boot、内核、NFS、NTP、systemd 服务和 journal |
| 网络 | 单个 IP/主机名 ping 和单端口 TCP 连通性测试；禁止网段扫描和任意目标脚本 |
| Docker | `docker ps -a` 容器、GPU 映射、资源限制、镜像、Volume、Compose 项目、Docker info 和容器日志只读查看 |
| 开发环境 | venv/conda/uv 盘点、依赖安装后 `pip check`、依赖冲突标记、conda YAML 方案、环境目录备份脚本和 SHA256 校验脚本 |
| 存储 | 目录容量和大文件扫描进度、软超时部分结果；超时、深度、最小大小和结果数可在设置中配置 |
| 资产与协作 | 主机硬件档案、位置/负责人/保修、CSV、标签多选、快捷视图、审计 before/after 脱敏、PWA、底部平台状态条、当前快照 JSON |
| 工具 | 远端检测和受限安装方案：sysstat、smartmontools、ethtool、iproute2、lsof、jq、tmux、htop、ncdu、nvtop、iotop、git、rsync 等 |

## 3. Docker 边界

Docker 入口位于主机详情的“Docker”标签页。平台通过 SSH 调用远端 Docker CLI，不保存 Docker Hub/Harbor 密码，不上传或下载 GB 级镜像 tar，不部署 Docker API 或远端 Agent。

当前只读功能：容器（含停止容器）列表、GPU 设备映射、资源限制、镜像列表、Volume 列表、Compose 项目、Docker info 和容器日志。启动/停止/重启/删除、exec 终端、镜像拉取/删除/构建、Volume 删除和 daemon.json 修改暂缓；避免把高权限 Docker socket 暴露给浏览器。

## 4. 安全与降级

- GPU 残留显存只做疑似标记，不提供“一键清理”，因为无法安全判断显存归属，误杀训练进程的代价高。
- systemd 只允许固定关键服务白名单；日志只读，重启仅生成脚本，不直接远程重启。
- 网络诊断只允许单个 IP/主机名和 1-65535 端口，不接受 CIDR、换行或 Shell 字符。
- 可选工具缺失只导致该项显示“未安装”，核心 CPU/内存采集继续运行。
- SMART 权限错误必须显示“不可用/无权限”，不能因 `failed: Permission denied` 误报磁盘损坏；dmesg 无权限时尝试 journalctl 内核日志，仍无权才显示不可用。
- 目录扫描有路径规范化、深度、结果数量和软超时；超时返回进度和已发现结果。

## 5. 配置项

设置页支持采集间隔、SSH 并发、连接/采集超时、重试、扫描超时/深度/最小文件大小/结果数、Swap/CPU/GPU/磁盘阈值、告警样本数/回差/重复通知、告警总提醒，以及 raw/mid/long 指标保留时间。默认推荐阈值为文件系统/inode 90%、Swap 80%、CPU/GPU 90C、磁盘 60C、GPU 功耗上限 98%，连续 5 个样本才告警；升级时仅迁移仍等于旧默认值的配置。告警页也提供同一开关；关闭总提醒后红点、网页/桌面弹窗和 Server 酱发送一并停用。

## 6. 暂缓需求

1. 浏览器本地端口转发：普通网页不能安全监听用户电脑端口，后续需独立客户端或受限代理网关。
2. 任意命令模板、多机批量 Shell、批量驱动更新/清缓存：需要审批、逐机停止、并发/回滚和更细的主机范围授权。
3. GPU 残留显存一键清理、任意 systemd 重启和 Docker 写管理：均可能造成不可逆服务中断。
4. SFTP 真正断点续传和 SSH 长连接池：当前使用上传进度、超时、并发和 rsync/SFTP 外部工具替代；达到规模后再以数据评估。
5. 定时快照调度、标签级告警抑制、彩色状态模板、cron 修改、Base64 小工具：对监控核心价值有限，优先级低于稳定性和安全。
6. 在线 `badblocks`、dmesg/OOM 长期落库和实时按进程 IO 速率：权限、噪声、容量或 IO 开销风险较高；当前保留只读摘要/累计计数。

## 7. 验收标准

自动化测试覆盖采集解析、GPU/Swap 告警、GPU 进程详情、SMART/dmesg 权限降级、告警批量筛选与权限审计、告警提醒开关参数/权限/CSRF、旧阈值迁移、进程资源字段、安全边界、systemd/Docker/网络 API、健康巡检、环境备份、依赖冲突、快照脱敏、权限和 schema 迁移。浏览器验收覆盖 GPU 点击详情、GPU 用户汇总收起、告警提醒关闭/开启、批量处置红点同步和 390px 手机横向溢出。

```bash
.venv/bin/python -m pytest -q
node --check monitor/static/app.js
.venv/bin/python -m compileall -q monitor tests scripts gunicorn.conf.py
.venv/bin/python -m pip check
git diff --check
```

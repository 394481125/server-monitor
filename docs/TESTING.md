# 测试说明

## 后端测试

```bash
.venv/bin/python -m pytest -q
```

覆盖 API 权限、认证、主机服务、采集解析、告警、GPU 调度、数据库迁移、SSH 连接池、SFTP 续传、GPU 评估命令/解析/持久化和安全边界。

关键新增案例：

- 迁移按顺序记录版本、名称和校验和。
- 旧占位迁移记录补齐元数据，高版本数据库拒绝降级启动。
- 后续迁移失败时，本批先前 DDL 一并回滚。
- 旧 schema 缺列时升级修复。
- 历史维护删除过期会话和终态记录，同时保留有效会话及运行中任务。
- SSH 连接仅在空闲、健康且凭据一致时复用。
- SFTP 从隐藏部分文件续传并原子完成，失败时保留部分文件。
- GPU 请求拒绝命令注入和未知模型/数据集。
- BF16 探测兼容无参数 PyTorch 接口，训练失败时保留其他硬件指标。
- GPU 历史接口要求二次验证并写入审计。

## 前端逻辑测试

```bash
node --check monitor/static/app_logic.js
node --check monitor/static/app.js
node --test tests_js/*.test.js
```

`app_logic.js` 同时供浏览器和 CommonJS 使用。测试覆盖 HTML 转义、指标格式化、仪表盘组合筛选，以及 FP8/INT8/训练/TP=8 结果归一化。

## 浏览器 E2E

```bash
.venv/bin/python scripts/e2e_acceptance.py
```

前提：Node.js 22+、`google-chrome`。脚本会：

1. 在 `/tmp` 创建隔离数据目录和 SQLite。
2. 启动只监听 `127.0.0.1` 随机端口的临时服务。
3. 启动 Headless Chrome 并通过 DevTools Protocol 操作页面。
4. 完成首次登录、强制改密和重新登录。
5. 在模拟 8 卡 H100 主机上提交多卡 ResNet-50 + CIFAR-10 快速评估。
6. 验证二次认证、FP8/INT8、loss/acc、NCCL、TP=8、历史持久化、审计记录和页面横向溢出。
7. 打开设置页、退出登录，并检查未捕获浏览器异常和截图。
8. 停止服务并清理临时文件。

E2E 不连接真实 SSH/GPU 主机，不会修改项目生产数据。远端执行由确定性模拟结果替代，权限、API、持久化和浏览器渲染仍走正式代码路径。

## SSH 接受测试

```bash
.venv/bin/python scripts/ssh_acceptance.py --help
.venv/bin/python scripts/websocket_acceptance.py --help
```

这些脚本用于具有 SSH 测试目标的环境，覆盖真实认证、指纹、命令、SFTP、终端和 Tmux WebSocket。运行参数必须指向专用测试主机。

## GPU 硬件验收

无 GPU 的 CI 只能验证请求、权限、固定脚本生成、结构化解析、入库和页面显示。发布前在目标 GPU 主机检查：

- 各精度结果是否与硬件能力一致。
- FP8/INT8 不支持时是否返回警告而非错误数值。
- ResNet/MobileNet/ViT 模型依赖提示是否准确。
- synthetic、FakeData 和 CIFAR-10 三种数据源是否可区分。
- 多卡 NCCL 带宽、TP degree、遥测和历史是否完整。

不同驱动、CUDA、PyTorch 和功耗状态会改变数值，测试只判断结构、范围和可解释性，不固定某个吞吐常数。

## 发布前完整检查

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

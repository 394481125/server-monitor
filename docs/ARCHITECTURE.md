# 架构说明

## 运行结构

```text
Browser
  -> Flask / Gunicorn (单 Worker, gevent)
       -> Web 安全上下文 -> 领域路由 -> 领域服务
       -> SSH 连接池 -> 被管 Linux 主机
       -> 后台采集 / GPU 调度 / 维护
       -> SQLite WAL + 加密主密钥
```

Web API、后台采集、调度和维护当前位于同一应用进程。`ProcessLock` 保证一个数据目录只被一个实例打开；SQLite 保存配置、会话、任务、指标、告警和审计。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `monitor/app.py` | 创建服务、注册通用中间件及核心 API，并装配领域路由 |
| `monitor/web.py` | 统一登录、权限、CSRF、二次认证、JSON 请求体和审计调用 |
| `monitor/routes/` | 运维、开发环境、文件与交互终端的 HTTP/WebSocket 适配层 |
| `monitor/services.py` | 主机、历史、告警、备份和导出等领域逻辑 |
| `monitor/collector.py` | 远端只读采集脚本、解析及扁平指标 |
| `monitor/operations.py` | 进程、Tmux、诊断、Docker 只读视图和受限任务 |
| `monitor/development.py` | Python/CUDA 环境探测、可审计方案及 GPU 评估入口 |
| `monitor/files.py` | SFTP 浏览、传输、校验和原子断点续传 |
| `monitor/ssh_client.py` | 主机指纹、命令超时、连接租约和空闲连接池 |
| `monitor/db.py` / `migrations.py` | SQLite 原语、基础 schema 和 forward-only 迁移 |

路由只负责输入、权限和响应适配。可独立测试的规则应进入对应服务，跨领域通用的 HTTP 规则进入 `WebContext`，不得在多个路由模块复制鉴权或审计实现。

## 请求路径

1. `before_request` 生成请求 ID、认证会话并装饰用户权限。
2. `WebContext.login_required` 检查登录、功能权限、CSRF、强制改密和二次认证。
3. 路由校验 HTTP 输入，调用领域服务，不直接拼接任意远端 Shell。
4. 服务通过数据库或 SSH 客户端完成操作，写操作记录 before/after 或结构化摘要。
5. `after_request` 设置 CSP、安全响应头和缓存策略。

现有 URL 和响应结构是前端契约。拆分路由时保持端点函数名、权限键、审计动作和状态码不变，并运行完整 API 与浏览器测试。

## 后台与单实例约束

`BackgroundService` 负责定时采集、维护和告警；`GPUScheduler` 依赖连续样本推进状态。两者与 Flask 应用共享数据库和内存状态，因此当前必须使用一个 Gunicorn Worker。

横向扩展顺序：

1. 将采集、调度、备份和维护移到独立 Worker 服务。
2. 为任务建立跨进程租约、幂等键及共享会话状态。
3. 将 SQLite 迁移到支持多实例写入的数据库。
4. 最后增加无状态 Web Worker 和节点。

## 数据与迁移

基础 schema 保证新库可启动，有序迁移负责旧库升级。迁移版本必须连续，已发布迁移不得修改；名称与校验和用于检测代码漂移。

一次启动中的所有待执行迁移在同一个 `BEGIN IMMEDIATE` 事务内提交，任一步失败都会整体回滚并拒绝启动。迁移采用 forward-only 策略，生产回退依赖升级前成组备份数据库和主密钥。

## SSH 与文件传输

连接池按主机、地址、端口、用户、凭据和指纹生成连接键。租约从空闲池移除，因此同一个 Paramiko Client 不会被线程并发使用；凭据变化、连接异常、空闲超时和应用退出都会关闭连接。

交互终端由 WebSocket 独占，不进入连接池，并受全局及用户/主机并发限制。SFTP 上传先写同目录隐藏临时文件，按目标、大小和首块指纹续传，通过大小校验后原子重命名。

## 安全边界

- 默认只绑定回环地址，不信任转发头；代理部署应显式配置可信 `ProxyFix`。
- 密码、CSRF、功能权限、主机能力和二次认证是相互独立的检查层。
- 远端命令使用固定模板、枚举参数、超时和输出上限，平台不接受任意脚本。
- Docker 保持只读，因为 Docker socket 通常等价 root。
- GPU 快速评估只接收受限模式、模型、数据集、时长和 Python 路径，结果结构化校验后入库。

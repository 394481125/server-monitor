# 架构说明

## 运行结构

```text
Browser
  -> Flask/Gunicorn (1 worker, gevent)
       -> Auth / Permission / Audit
       -> Host, File, Operation, Development services
       -> SSH connection pool -> managed Linux hosts
       -> Background collector / scheduler / maintenance
       -> SQLite WAL + encrypted secret key
```

Web API、后台采集、GPU 调度、备份和维护当前运行在同一应用进程。`ProcessLock` 保证同一数据目录只有一个实例。

维护任务按配置聚合和清理指标、审计、已结束任务、通知、恢复告警及过期会话；数据库和备份文件不依赖手工删除控制增长。

## 模块边界

- `monitor/app.py`：应用装配、HTTP/WebSocket 路由和请求级安全。
- `monitor/services.py`：主机、历史、告警、备份等领域服务。
- `monitor/collector.py`：远端采集命令和解析。
- `monitor/operations.py`：受限远端运维操作。
- `monitor/development.py`、`monitor/gpu_benchmark.py`：开发环境和 GPU 评估。
- `monitor/files.py`：SFTP 文件管理和断点续传。
- `monitor/ssh_client.py`：SSH 客户端、指纹校验和连接池。
- `monitor/migrations.py`、`monitor/db.py`：SQLite schema、迁移和存储原语。
- `monitor/static/app_logic.js`：可测试前端纯逻辑。
- `monitor/static/app.js`：页面状态、渲染和交互。

## 单 Worker 约束

`workers = 1` 是当前一致性要求。进程锁、后台循环和 SQLite 都以单实例为前提。gevent 负责 WebSocket 和 HTTP IO 并发，后台线程池负责受控 SSH 并发，SSH 池减少重复握手。

多 Worker 演进需要先完成：

1. 将后台服务从 Flask 应用工厂移到独立进程。
2. 为采集、调度和维护建立跨进程租约及幂等任务键。
3. 将会话、交互 SSH 限制和任务状态移到共享存储。
4. 使用 PostgreSQL 等多实例数据库。
5. 最后把 Web 层改为无状态多 Worker。

## 路由拆分策略

`app.py` 仍较大，但本轮不进行全量蓝图迁移，因为装饰器依赖请求用户、权限、审计和多个服务闭包，机械拆分会扩大回归面。

后续按域渐进拆分：

- 先把共用 `login_required`、`audit_action` 和服务访问放入明确的应用扩展接口。
- 再按 `auth/hosts/alerts/development/files/operations` 建立 Blueprint。
- 每迁移一个域，保持 API 路径和响应契约不变，并运行全量 API/E2E。

## 数据迁移

基础 schema 使用 `CREATE IF NOT EXISTS` 保证新库可启动；有序迁移负责旧库升级和新字段。迁移版本必须从 1 连续递增，不允许修改已发布迁移的实现；校验和用于发现代码漂移。

本批所有待执行迁移在单个写事务中提交。当前采用 forward-only 策略：失败自动回滚，已成功上线后的业务回退依赖升级前备份，而不是运行破坏性的 down migration。

## SSH 复用

连接池按主机 ID、地址、端口、用户、认证材料和指纹生成连接键。租约从空闲列表移除，因此同一个 Paramiko Client 不会被两个线程同时使用。归还时只保留活动 Transport；连接异常、空闲超时、容量超限和配置变化都会关闭连接。

交互终端不进入连接池，因为其生命周期由 WebSocket 独占。

## GPU 评估边界

平台生成固定版本的远端 Python 脚本并写入随机临时文件，不接受用户提供的 Python 源码。请求只允许枚举模式、模型、数据集、时长和受限 Python 可执行路径。

评估结果以带标记 JSON 返回，后端校验必需字段后入库。真实硬件能力缺失以警告或明确错误返回，不以估算值补齐。

# ADR-0001：模块化单体与独立 Worker

> 状态：Accepted
> 日期：2026-08-31
> 决策者：量化平台架构
> 关联文档：[`../TARGET_ARCHITECTURE_V3.md`](../TARGET_ARCHITECTURE_V3.md)

## 背景

Quart 当前是单机 Gradio 应用，研究、任务编排、手动交易和文件制品位于同一代码仓库。它满足当前日频研究与人工 T+1 交易，但任务队列、刷新总线和 PaperBroker 依赖进程内状态。未来接入券商 API 后，HTTP 请求、多进程部署、调度器和券商回报可能并发修改订单与账户状态。

同时，项目已经拥有可复用的日频回测、T+1 撮合、ArtifactStore、SQLite 账本和 BrokerAdapter。整体迁移到 vn.py、Qlib、WonderTrader、NautilusTrader 或 QuantDinger 会引入大量非当前目标能力，并增加 A 股规则与已有研究口径迁移风险。

## 决策驱动因素

- 保留已测试的回测与信号执行语义；
- 在自动实盘前解决任务、订单和账户的持久化与恢复；
- 支持 Windows 本地券商 SDK；
- 控制单人/小团队的部署与运维复杂度；
- 允许未来独立前端、多用户和 AI Agent，而不提前微服务化；
- 让研究计算与真实资金副作用隔离。

## 备选方案

### 方案 A：维持单进程 Gradio

优点是最简单；缺点是重启丢任务、无法保证唯一交易所有者、多进程状态不一致，不满足券商 API 安全要求。

### 方案 B：整体采用第三方量化平台

优点是可快速获得部分交易或研究能力；缺点是迁移成本、许可、A 股适配、数据口径和现有测试重写风险高。不同平台也分别偏向研究、期货/实时交易、Crypto 或高频，不能同时替代当前全部领域。

### 方案 C：立即拆分微服务

优点是进程隔离明确；缺点是需要服务发现、网络合同、分布式事务、部署和监控，远超当前规模。

### 方案 D：模块化单体 + 独立 Worker

共享一个代码库和领域模型，将外部副作用按进程角色隔离，使用持久化命令和状态库协调。

## 决策

选择方案 D：

1. 保留一个仓库和模块化领域代码；
2. Gradio/Control API 负责交互、认证、校验和命令提交；
3. Research Worker 执行有限、可重试的研究任务；
4. Trading Worker 独占券商连接、订单执行和回报同步；
5. Scheduler 只提交持久化命令；
6. 任务、订单、成交、账户和审计写入持久化状态库；
7. Parquet/DuckDB 继续承担分析数据，不承担交易事务；
8. Redis 为可选缓存/队列，不作为权威状态源。

## 结果

### 正面影响

- 不重写已验证研究内核；
- 交易副作用具有唯一进程所有者；
- 支持重启恢复、幂等和审计；
- 可以渐进增加 API、SPA、PostgreSQL 和 Agent Gateway；
- Broker Agent 可原生部署于 Windows。

### 负面影响与成本

- 需要新增 migration、持久化 Job 和 OMS；
- 兼容期内存在旧 `TaskQueue`/reports 双写与新合同并存；
- 进程拆分后必须建设健康检查、结构化日志和部署脚本；
- SQLite 只适合作为过渡，券商阶段需要 PostgreSQL 或严格单写者架构。

## 实施约束

- 任何进程不得绕过 Application Service 直接修改订单或账户；
- Trading Worker 是唯一可以调用真实 BrokerAdapter 写接口的进程；
- 正式订单必须经过 SecurityMaster、RuleBook 和 Risk Engine；
- 所有 mutation 支持幂等；
- 先 PaperBroker 和故障恢复，再真实券商；
- 不因该决策立即拆分部署，按目标架构 Phase A-D 渐进实施。

## 验证与退出条件

该决策在以下情况重新评审：

- 单一代码库无法在可接受时间内发布；
- Research Worker 与 Trading Worker 需要独立技术栈或独立扩缩容；
- 多账户/多地域要求使单一数据库成为明确瓶颈；
- 合规要求必须进行更强的网络和组织隔离。

重新评审前必须已有生产指标证明瓶颈，不能仅以“商业平台通常使用微服务”为理由。

## 参考

- [QuantDinger Architecture](https://github.com/OpenByteInc/QuantDinger/blob/main/docs/architecture/ARCHITECTURE.md)
- [QuantDinger Concurrency Model](https://github.com/OpenByteInc/QuantDinger/blob/main/docs/architecture/CONCURRENCY_MODEL.md)
- [vn.py](https://github.com/vnpy/vnpy)
- [Microsoft Qlib](https://github.com/microsoft/qlib)
- [NautilusTrader Execution](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/concepts/execution.md)

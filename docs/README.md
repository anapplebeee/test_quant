# Quart 文档索引

本目录保存需要长期维护的架构、开发流程、研究结论和架构决策。根目录中的规划文件负责说明阶段目标，本目录负责说明稳定契约与执行规则。

## 架构与开发

| 文档 | 用途 | 维护触发条件 |
|---|---|---|
| [`TARGET_ARCHITECTURE_V3.md`](TARGET_ARCHITECTURE_V3.md) | 目标架构、领域边界、运行拓扑、迁移路径 | 新增进程、状态源、交易链路或跨域依赖 |
| [`DEVELOPMENT_COORDINATION.md`](DEVELOPMENT_COORDINATION.md) | 多工作流协调、任务拆分、PR/验收/发布流程 | 开发流程、质量门禁或模块所有权变化 |
| [`adr/README.md`](adr/README.md) | 架构决策记录索引与模板 | 出现长期、跨模块、难回滚的设计决策 |

## 根目录配套文档

| 文档 | 定位 |
|---|---|
| [`../ARCHITECTURE_OVERVIEW.md`](../ARCHITECTURE_OVERVIEW.md) | 当前工作树的 As-Is 前后端和 T+1 流程总览 |
| [`../QUANT_PLATFORM_REFACTOR_ROADMAP.md`](../QUANT_PLATFORM_REFACTOR_ROADMAP.md) | 商业级能力差距和长期改造路线 |
| [`../MANUAL_TRADING_T1_SYNC_PLAN.md`](../MANUAL_TRADING_T1_SYNC_PLAN.md) | 手动 T+1 交易闭环设计 |

## 文档维护规则

1. 当前实现写入 `ARCHITECTURE_OVERVIEW.md`，目标契约写入 `TARGET_ARCHITECTURE_V3.md`，避免把规划误写成已完成能力。
2. API、数据库、订单状态机、风控不变量或进程所有权发生变化时，代码与文档必须在同一提交更新。
3. 临时排查记录不进入长期架构文档；可复用的结论应整理成 ADR、测试或正式研究报告。
4. 策略收益数字必须标注数据快照、研究模式、样本内/样本外、成本和容量口径。
5. 文档中的完成状态以测试和可复现命令为依据，不以页面存在或代码占位为依据。

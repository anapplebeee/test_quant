# ADR-0001：SQLite Migration 与持久化 Job

- 状态：Accepted
- 日期：2026-08-31
- 对应工作项：DB-001（SQLite migration、WAL、busy timeout）、JOB-001（持久化 Job/Command）
- 主工作流：F 平台与质量（后端平台工程）
- 批次：1

## 背景

当前任务系统是**进程内**的（`api/task_api.py` 的 `TaskQueue` 用内存 dict + 后台线程）。
问题：
1. **进程重启即丢失**——Gradio 重启后所有任务状态、排队、结果全部消失；
2. 无跨进程的 job 认领（claim）、租约（lease）、心跳与恢复机制；
3. 无法支撑多 Worker、控制面 API（API-001）与 OMS（OMS-001）等后续工作流。

此外，现有 SQLite 用法（`quart/manual_trading/repository.py`）是非 versioned 的
`CREATE TABLE IF NOT EXISTS`，schema 变更无法安全演进——协调文档明确要求
"数据 schema 变化必须先定义兼容或迁移方案，再修改消费者"。

## 决策

1. **引入 SQLite 作为基础设施数据库**（`state/quart.db`），用 **versioned migration** 管理 schema：
   - 每张表/每次变更 = 一个递增版本；
   - migration 用 `PRAGMA user_version` 记录当前版本；
   - 只前向应用，支持回滚方案；
   - migration 与代码、测试同一提交（协调文档第 6 节）。

2. **数据库连接开启 WAL 模式 + busy timeout**：
   - `PRAGMA journal_mode=WAL`：读写并发不互相阻塞；
   - `PRAGMA busy_timeout`：并发写等待而非立即失败；
   - `PRAGMA foreign_keys=ON`：保证引用完整性。

3. **Job 持久化**，核心状态机：

   ```
   CREATED → QUEUED → CLAIMED → RUNNING → SUCCEEDED
                              ↘ FAILED
                              ↘ CANCELLED
   ```

   - `claim`：Worker 原子认领一个 QUEUED job（`UPDATE ... WHERE status='QUEUED'` 的
     CAS 语义，SQLite 单写保证原子）；
   - `lease`：job 有租约到期时间，Worker 周期性心跳续约；
   - `recovery`：进程重启后，将 lease 过期的 RUNNING/CLAIMED job 重置为 QUEUED（重试）
     或标记为 FAILED（超限）；
   - `idempotency`：job 有幂等键（`idempotency_key` 唯一约束），重复提交同一 job 返回原 job。

4. **只搭建基础骨架，不含 ARCH-001 的未冻结领域字段**：
   - Job 表只含任务类型、参数、状态、租约、结果等**通用平台字段**；
   - 不包含订单/成交/风控等 ARCH-001 待冻结的领域列；
   - 后续 OMS/订单领域接入时，通过新的 migration 版本扩展。

## 非目标

- 不替代现有的 `manual_trading` 账本（其有自己的 schema）；
- 不实现 OMS、Risk Engine、Broker Adapter（ARCH-001/RISK-001/OMS-001 的范畴）；
- 不做多进程 Worker 调度器（DB-001 完成后，API-001 才接入）；
- 不冻结订单领域 schema。

## 影响的不变量

- 任务系统从"进程内内存"变为"SQLite 持久化"，重启可恢复；
- 所有 job 状态转换必须通过 repository，不允许直接改数据库绕过；
- 引入新的基础设施（数据库），需更新部署/监控文档。

## API / schema / 配置变化

- 新增 `state/quart.db`（SQLite）；
- 新增 migration 表（用 `PRAGMA user_version`）；
- Job 表 schema（版本 1）。

## 依赖项

- 无（不依赖 ARCH-001，因只搭通用平台骨架）；
- 为 API-001（Control API）提供基础。

## 验收标准

1. migration 可前向升级、可回滚演练；
2. Job 支持 create/claim/lease/cancel/recovery；
3. 进程崩溃/重启后，未完成任务可恢复（lease 过期重置为 QUEUED）；
4. 幂等键唯一，重复提交不产生重复 job。

## 测试计划

- `test_migration.py`：升级/回滚/并发；
- `test_job_repository.py`：状态机、claim/lease/cancel、幂等；
- `test_job_recovery.py`：模拟崩溃/重启恢复（恢复测试夹具）。

## 文档更新

- 本 ADR；
- README 补充 Job/migration 说明；
- 协调文档同步勾选 DB-001/JOB-001。

## 回滚或兼容方案

- 数据库是新增的 `state/quart.db`，与现有文件状态并存；
- 回滚 = 删除该库（任务系统回退到内存 TaskQueue），不影响现有功能；
- migration 用 `user_version`，可安全回退到低版本（若 migration 定义了向下迁移）。

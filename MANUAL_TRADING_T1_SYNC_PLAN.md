# Quart 手动交易 T+1 同步规划

> 阶段定位：平台负责生成信号、风险检查和 T+1 交易计划；用户在券商客户端手动下单；平台通过人工成交回填或券商成交文件导入，维护下一交易日可用的账户状态。
>
> 当前阶段不自动向券商报单。未来接入券商 API 时，复用本文定义的计划、订单、成交、持仓、现金和对账模型，仅替换执行输入输出方式。

## 0. 当前落地状态（2026-08-30）

第一版最小闭环已经落地：

- SQLite 账户、快照、交易计划、计划订单、成交、持仓批次和对账表；
- 从 `holdings.json` 初始化或每日信号首次运行自动迁移；
- 每日信号自动创建 `DRAFT` 计划并在报告中显示 `plan_id`；
- CLI 查询、审批、取消计划；
- 单笔成交录入和通用 CSV 导入；
- 计划审批、部分成交、完成状态和重复成交编号保护；
- 当日买入不可卖、下一交易日转可卖的持仓批次；
- 券商账户 JSON 快照差异预览和显式确认对账；
- 关键流程自动化测试。

仍待落地：

- Gradio 手动交易操作页；
- 计划订单调减的前端/CLI 操作；
- 人工下单 CSV 导出；
- 各券商 CSV/XLSX 列映射；
- 权威 A 股交易日历；
- 未对账账户对正式计划的强制阻断；
- 券商 API、自动报单和订单回报。

## 1. 建设目标

本阶段解决以下问题：

1. T 日收盘后，根据已对账账户生成 T+1 交易计划；
2. 用户在 T+1 手动下单后，将真实成交回填平台；
3. 平台根据真实成交更新现金、费用、持仓批次和可卖数量；
4. T+1 收盘后，将平台账本与券商账户快照对账；
5. 使用对账后的账户状态生成 T+2 交易计划；
6. 保留每次信号、计划、人工调整、成交和对账证据；
7. 为未来券商 API 接入建立稳定领域接口。

本阶段不包含：

- 自动向券商发送订单；
- 自动撤单或追单；
- 盘中高频信号；
- 多券商实时行情；
- 无人值守自动实盘。

## 2. 当前流程问题

当前 `state/holdings.json` 只保存：

```json
{
  "cash": 50000,
  "positions": {
    "600519": 200,
    "601318": 800
  }
}
```

它无法可靠表达：

- 哪些股票是当日买入、尚不可卖；
- 计划数量与实际成交数量的差异；
- 部分成交、未成交、撤单和人工跳过；
- 实际成交价格、佣金、印花税和其他费用；
- 可交易现金与可转出现金；
- 平台持仓与券商持仓的差异；
- 某个持仓状态由哪些成交记录推导得到；
- 用户是否确认并执行了某一份交易计划。

因此应将 `holdings.json` 从“唯一真实状态”降级为兼容输入，逐步迁移到本地交易账本。

## 3. T+1 日常工作流

### 3.1 T 日收盘后：生成计划

建议运行时间：收盘数据完整并通过质量检查后。

```text
更新 T 日行情
-> 校验数据完整性
-> 读取最近一次已对账账户快照
-> 生成 T 日目标组合
-> 执行组合与风险约束
-> 生成 intended_trade_date = T+1 的交易计划
-> 用户查看并确认计划
```

输出内容：

- 信号日期；
- 计划交易日期；
- 目标权重；
- 当前持仓和目标持仓；
- 建议买卖数量；
- 参考价格和价格偏离提示；
- 预计费用；
- T+1 可卖数量；
- 风险、流动性、涨跌停和停牌提示；
- 计划指纹和版本。

### 3.2 T+1 开盘前：人工确认

计划初始状态为 `DRAFT`。用户可以：

- 确认全部计划；
- 修改限价或执行备注；
- 减少数量；
- 跳过某只股票；
- 取消整份计划。

任何人工调整都不得覆盖策略原始建议，应同时保存：

- `strategy_quantity`：策略建议数量；
- `approved_quantity`：用户确认数量；
- `adjustment_reason`：调整原因；
- `approved_at`：确认时间。

确认后计划状态变为 `APPROVED`。

### 3.3 T+1 交易时段：手动下单

用户继续在券商客户端手动下单。平台不自动报单。

成交同步支持两种方式：

1. 人工录入：适合少量订单；
2. 导入券商当日成交 CSV/XLSX：适合订单较多时。

每笔成交至少记录：

- 成交日期和时间；
- 股票代码；
- 买卖方向；
- 成交数量；
- 成交价格；
- 佣金、印花税和其他费用；
- 券商成交编号；
- 对应计划订单；
- 数据来源和录入时间。

成交可以多笔对应一条计划订单，以支持部分成交和分批成交。

### 3.4 T+1 收盘后：对账

用户录入或导入券商账户快照：

- 总资产；
- 可用资金；
- 可取资金；
- 股票代码；
- 总持仓；
- 可用持仓；
- 成本价；
- 市值。

平台自动比较：

```text
平台账本推导状态
vs
券商账户快照
```

差异分类：

- 成交遗漏；
- 费用差异；
- 数量差异；
- 现金差异；
- T+1 可卖数量差异；
- 手工订单未关联计划；
- 公司行为或券商调整。

无差异或用户确认差异后，生成 `RECONCILED` 账户快照。下一次信号只能使用最近已对账快照。

## 4. T+1 账户语义

### 4.1 持仓数量

每只股票至少区分：

- `total_quantity`：总持仓；
- `sellable_quantity`：当前可卖数量；
- `frozen_quantity`：已挂卖单或其他冻结数量；
- `today_buy_quantity`：当日买入、当日不可卖数量。

采用持仓批次记录：

```text
symbol
buy_trade_date
settle_date
quantity
remaining_quantity
unit_cost
source_fill_id
```

T+1 日买入的 A 股不得在 T+1 日内卖出；进入下一交易日后才转为可卖。具体可卖数量以券商快照为最终校验来源。

### 4.2 现金数量

至少区分：

- `cash_available_to_trade`：可用于证券交易的资金；
- `cash_withdrawable`：可转出资金；
- `cash_frozen`：委托或结算冻结资金；
- `cash_total`：现金总额。

手动日频阶段生成次日计划时，主要使用 `cash_available_to_trade`。不同券商结算展示差异由账户快照和未来 Broker Adapter 适配。

### 4.3 计划卖出约束

生成计划时：

```text
planned_sell_quantity <= sellable_quantity
```

若目标要求卖出数量超过可卖数量：

- 仅生成当前可卖部分；
- 将剩余目标记录为 `deferred_quantity`；
- 下一交易日重新计算，不机械沿用旧价格和旧数量；
- 报告中明确提示 T+1 限制。

## 5. 数据模型

建议第一阶段使用 Python 标准库 `sqlite3`，数据库文件为：

```text
state/trading.db
```

SQLite 足以支撑单机、单用户、日频手动交易，并提供事务和可追溯性。未来接券商 API 后仍可保留领域接口，再根据并发需求迁移数据库。

### 5.1 `accounts`

```text
account_id
account_name
broker_name
base_currency
status
created_at
```

### 5.2 `trade_plans`

```text
plan_id
account_id
strategy_name
signal_date
intended_trade_date
status
source_run_id
config_fingerprint
created_at
approved_at
completed_at
notes
```

状态：

```text
DRAFT -> APPROVED -> IN_PROGRESS -> COMPLETED
   |          |            |
CANCELED   CANCELED      PARTIAL / EXPIRED
```

### 5.3 `planned_orders`

```text
planned_order_id
plan_id
symbol
side
strategy_quantity
approved_quantity
reference_price
target_weight
estimated_fee
deferred_quantity
status
adjustment_reason
```

### 5.4 `manual_fills`

```text
fill_id
account_id
planned_order_id
broker_fill_id
trade_date
trade_time
symbol
side
quantity
price
commission
stamp_tax
transfer_fee
other_fee
source
created_at
```

`broker_fill_id` 在同一账户内唯一，避免重复导入成交。

### 5.5 `position_lots`

```text
lot_id
account_id
symbol
buy_trade_date
settle_date
original_quantity
remaining_quantity
unit_cost
source_fill_id
status
```

### 5.6 `account_snapshots`

```text
snapshot_id
account_id
as_of
cash_total
cash_available_to_trade
cash_withdrawable
cash_frozen
total_equity
source
reconciliation_status
created_at
```

### 5.7 `snapshot_positions`

```text
snapshot_id
symbol
total_quantity
sellable_quantity
frozen_quantity
cost_price
market_price
market_value
```

### 5.8 `reconciliations`

```text
reconciliation_id
account_id
as_of
ledger_snapshot_id
broker_snapshot_id
status
cash_difference
position_difference_count
resolution
confirmed_by
confirmed_at
```

## 6. 模块规划

建议新增：

```text
quart/manual_trading/
├── models.py              # 计划、成交、快照、对账模型
├── repository.py          # SQLite 持久化
├── plan_service.py        # 创建、审批、过期交易计划
├── fill_service.py        # 录入和导入人工成交
├── settlement.py          # T+1 可卖数量和交易日推进
├── accounting.py          # 由成交重建现金和持仓
├── reconciliation.py      # 与券商快照对账
├── imports/
│   ├── csv_generic.py     # 通用 CSV 模板
│   └── broker_profiles.py # 各券商列名映射
└── migration.py           # holdings.json 初始化迁移
```

接口原则：

- `pipeline.py` 从账户服务读取已对账状态；
- `pipeline.py` 生成 `TradePlan`，不直接写持仓；
- 只有成交记录和明确的对账调整可以改变账户账本；
- 用户修改计划不会伪造成真实成交；
- 未来 Broker Adapter 调用相同 `fill_service` 写入成交。

## 7. 前端功能规划

### 7.1 账户初始化

- 从现有 `holdings.json` 导入；
- 手工录入券商账户快照；
- 显示导入前后差异；
- 用户确认后建立第一份已对账快照。

### 7.2 今日交易计划

- 显示信号日期和计划交易日期；
- 显示策略建议、当前状态和目标状态；
- 显示可卖数量和 T+1 延迟数量；
- 支持批准、调减、跳过和备注；
- 禁止超过现金、可卖数量和风险限额；
- 导出适合人工下单的 CSV。

### 7.3 成交回填

- 逐笔人工录入；
- 批量导入 CSV/XLSX；
- 自动匹配计划订单；
- 检测重复成交编号；
- 显示计划数量、已成交和剩余数量；
- 支持无法自动匹配时人工关联。

### 7.4 收盘对账

- 导入或录入券商账户快照；
- 显示现金和持仓差异；
- 对差异进行分类和备注；
- 用户确认后标记已对账；
- 未对账账户禁止生成正式下一日计划。

### 7.5 交易复盘

- 计划价与成交价偏差；
- 计划数量与成交数量偏差；
- 人工调整原因；
- 未成交原因；
- 实际费用；
- 计划后组合与真实组合偏差；
- 回测滑点与真实滑点对比。

## 8. 命令行与 API 规划

第一阶段建议提供：

```text
quart account init
quart account snapshot import <file>
quart plan create --signal-date YYYY-MM-DD
quart plan approve <plan_id>
quart fills add
quart fills import <file>
quart reconcile <snapshot_file>
quart account show
```

对应服务 API：

```text
GET    /accounts/{id}
POST   /accounts/{id}/snapshots
POST   /trade-plans
POST   /trade-plans/{id}/approve
POST   /trade-plans/{id}/cancel
POST   /fills/import
POST   /reconciliations
GET    /reconciliations/{id}
```

API 只操作本地交易账本，不向券商发送订单。

## 9. 与现有代码的衔接

### 9.1 `load_holdings`

迁移期：

1. 优先从 `trading.db` 读取最近已对账账户；
2. 数据库不存在时兼容读取 `holdings.json`；
3. 前端提示用户完成账户初始化；
4. 迁移完成后禁止直接修改 `holdings.json`。

### 9.2 `generate_orders`

继续复用现有订单生成算法，但输入需要增加：

- `sellable_positions`；
- `cash_available_to_trade`；
- `intended_trade_date`；
- `account_id`；
- `source_run_id`。

输出从临时列表升级为可持久化的 `TradePlan`。

### 9.3 `run_daily`

目标流程：

```text
读取最近已对账快照
-> 生成目标组合
-> 风控
-> 创建 DRAFT 交易计划
-> 保存 Artifact 和 TradePlan
-> 推送计划摘要
-> 等待用户审批和手动执行
```

信号生成不得直接改变账户状态。

## 10. 异常处理

### 10.1 未回填成交

- 计划到期仍无成交时标记 `EXPIRED`；
- 不假设计划已成交；
- 下一日继续使用最近已对账账户状态；
- 提示用户完成成交或账户快照同步。

### 10.2 部分成交

- 计划状态标记 `PARTIAL`；
- 账户按实际成交更新；
- 未成交部分不自动顺延；
- 下一交易日根据最新目标重新计算。

### 10.3 临时人工交易

用户未按平台计划进行的交易也必须录入：

- `planned_order_id` 为空；
- `source = MANUAL_EXTERNAL`；
- 记录人工原因；
- 纳入账户和策略偏离分析。

### 10.4 无法对账

- 账户状态标记 `UNRECONCILED`；
- 禁止生成“正式”交易计划；
- 允许生成只读预览计划；
- 用户修正成交、费用或确认调整后恢复。

### 10.5 数据或市场异常

- 数据未收盘、缺失或过期时不生成正式计划；
- 股票停牌或无有效价格时冻结对应持仓；
- 涨跌停风险作为明确提示；
- 规则无法解析时阻断相关订单。

## 11. Artifact 与审计要求

每份交易计划必须关联：

- 信号 Artifact `run_id`；
- 数据版本；
- 策略和配置版本；
- 账户快照 ID；
- 风险检查结果；
- 用户审批记录；
- 实际成交记录；
- 收盘对账结果。

禁止覆盖历史记录。修正采用新增事件或冲正记录，而不是修改既有成交事实。

## 12. 未来券商 API 接入方式

未来增加统一接口：

```python
class BrokerAdapter:
    def submit_order(self, order): ...
    def cancel_order(self, broker_order_id): ...
    def query_orders(self, trade_date): ...
    def query_fills(self, trade_date): ...
    def query_account_snapshot(self): ...
```

手动模式和 API 模式的差异：

```text
手动模式：用户下单 -> 人工录入/文件导入 -> FillService
API 模式：BrokerAdapter 下单 -> 回报订阅/查询 -> FillService
```

两种模式共用：

- TradePlan；
- PlannedOrder；
- Fill；
- PositionLot；
- AccountSnapshot；
- Reconciliation；
- 风控与审计。

因此手动同步阶段不是临时代码，而是自动交易系统的基础领域层。

## 13. 分阶段实施

### 阶段 A：账户快照与初始化

- [x] 定义 SQLite Schema；
- [x] 实现账户和快照 Repository；
- [x] 从 `holdings.json` 迁移；
- [ ] 前端显示当前账户状态；
- [x] 增加 JSON 快照导入和校验。

验收：无需直接编辑 JSON 即可初始化和查看账户。

### 阶段 B：交易计划持久化与审批

- [x] 将每日信号保存为 `TradePlan`；
- [x] 保存策略建议数量和用户批准数量；
- [x] 增加审批和取消状态；
- [ ] 增加按交易日自动过期；
- [ ] 导出人工下单 CSV；
- [x] 报告和推送中显示 plan_id。

验收：每份人工下单计划都有唯一 ID、来源和审批记录。

### 阶段 C：人工成交回填

- [x] 实现逐笔成交录入；
- [x] 实现通用 CSV 导入；
- [x] 通过 planned_order_id 匹配计划订单；
- [x] 防止重复导入；
- [x] 按成交更新账户账本。
- [ ] 增加基于代码、方向、日期的自动模糊匹配。

验收：部分成交、多笔成交和计划外交易均能正确处理。

### 阶段 D：T+1 结算与可卖数量

- [x] 实现持仓批次；
- [x] 实现工作日结算推进和显式 settle_date；
- [ ] 接入权威 A 股交易日历；
- [ ] 生成计划时限制可卖数量；
- [ ] 显示延期卖出数量；
- [x] 添加周末和 T+1 测试；
- [ ] 添加法定节假日测试。

验收：当日买入不会被同日卖出计划使用，下一交易日按规则转为可卖。

### 阶段 E：收盘对账与复盘

- [x] 导入 JSON 券商账户快照；
- [x] 自动比较现金和持仓；
- [x] 差异分类和人工确认；
- [ ] 未对账阻断正式计划；
- [ ] 输出计划与真实成交偏差。

验收：下一日计划只使用已对账账户，且账户状态可由账本重建。

### 阶段 F：券商 API 准备

- [ ] 定义 `BrokerAdapter`；
- [ ] 建立模拟 Adapter；
- [ ] 将人工导入和 Adapter 回报统一写入 FillService；
- [ ] 实现 API 订单状态机；
- [ ] 保留人工模式作为应急通道。

验收：接入具体券商时不修改策略、组合、风控和账户核心模型。

## 14. 第一批建议开发任务

建议立即实施的最小闭环：

- [x] 新建 `state/trading.db` Schema 和 Repository；
- [x] 实现从 `holdings.json` 创建初始已对账快照；
- [x] 将 `run_daily` 输出同时保存为 `DRAFT TradePlan`；
- [ ] 增加计划确认前端界面（CLI 已完成）；
- [x] 增加通用成交 CSV 模板和导入功能；
- [x] 根据成交计算账户和持仓批次；
- [x] 增加收盘账户快照导入和对账；
- [x] 下一日信号改为读取最近已对账账户；
- [x] 增加 T+1、部分成交和重复导入测试；
- [ ] 增加计划外交易与端到端每日流水线测试；
- [ ] 稳定运行后再设计具体券商 Adapter。

## 15. 本阶段完成定义

满足以下条件时，手动交易 T+1 同步阶段完成：

- 用户不再直接修改 `holdings.json`；
- 每份信号都有唯一交易计划；
- 每份计划都能追踪批准、执行和完成状态；
- 实际成交而不是计划数量决定账户状态；
- 当日买入和可卖数量严格区分；
- 部分成交和计划外交易可处理；
- 每日可与券商账户完成现金和持仓对账；
- 下一日计划只使用最近已对账状态；
- 所有人工调整和差异都有审计记录；
- 未来券商 API 可以通过 Adapter 接入现有流程。

# UI-AUDIT-001：页面直读盘点、交互原型、DTO 需求

> 批次 0 / 泳道 D（前端盘点）| 主责：前端工程师/产品  
> 目标：列出页面直读、命令入口、状态字段和缺失操作；绘制 Job/Order/Risk 页面原型，形成 UI-001 清单  
> 边界：API 合同冻结前只做原型和 mock，不新增状态源

---

## 1. 页面直读盘点矩阵

### 1.1 数据读取方式图例

| 标记 | 含义 |
|------|------|
| API:ModuleName | 通过后端 API 模块读取（合规） |
| FS:/path | 直接读取文件系统（需整改） |
| DB:table | 直接读取数据库（需整改） |
| MEM:object | 内存对象读取（进程内合法） |
| EXT:script | 调用外部脚本（需白名单化） |

### 1.2 页面 × 数据源矩阵

#### 🏠 首页 (home.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 数据新鲜度 | API:BarStore (quart.data.store) | API:data_api.get_stock_stats() | 已合规 |
| 下一交易日 | API:TradingRepository.next_trade_date | API:data_api / control_api | 可保留 |
| 账户现金/总资产 | API:manual_trading_api.account_view + 二次查询 | API:manual_trading_api.account_view | 需优化（二次查询消除） |
| 回测摘要 | API:backtest_api.get_backtest_summary | 同 | 已合规 |
| 窗口统计 | API:backtest_api.get_window_stats | 同 | 已合规 |
| 扫描结果 | API:research_api.latest_sweep_headlines | 同 | 已合规 |
| 策略目录 | API:strategy_api.strategy_catalog | 同 | 已合规 |

**问题**：`_today_status_card()` 在渲染时直接 `import` 并调用 `BarStore()` 和 `Repository()`，不是直读文件系统，但与前端层职责不清（应统一走 api 层封装）。

---

#### 📈 回测中心 (backtest.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 回测列表 | API:backtest_api.scan_summaries | 同 | 已合规 |
| 摘要 | API:backtest_api.get_backtest_summary | 同 | 已合规 |
| 净值曲线 | API:backtest_api.get_equity_curve | 同 | 已合规 |
| 交易记录 | API:backtest_api.get_trades | 同 | 已合规 |
| 成本分解 | API:backtest_api.get_cost_breakdown | 同 | 已合规 |
| 扫描文件 | API:research_api.list_sweeps / load_sweep | 同 | 已合规 |
| 研究报告 | API:research_api.list_research_reports / load_research_report | 同 | 已合规 |
| 制品面板 | API:artifacts_api.runs_table / run_detail_md / wfa_panel_md | 同 | 已合规 |

**状态**：✅ 完全合规，无直读。

---

#### 📋 每日信号 (daily_signal.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 信号报告列表 | FS:reports/signal_*.md (reports_dir().glob) | API:research_api 统一入口 | 需统一 |
| 信号报告内容 | FS:reports/signal_YYYYMMDD.md (open/read) | API:research_api.load_signal_report | ⚠️ 直读文件 |
| 交易计划列表 | API:manual_trading_api.plans_view | 同 | 已合规 |
| 计划详情 | API:manual_trading_api.plan_view | 同 | 已合规 |
| ML 预测分数 | FS:data/scores/preds.csv (pd.read_csv) | API:research_api / data_api | ⚠️ 直读文件 |

**问题**：`_load_signal()` 和 ML 分数直接读取文件系统。虽然通过 `common.safe_path()` 做了路径安全检查，但违反了"前端不直读"原则。

---

#### 🧰 操作中心 (operations.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 任务提交 | API:task_queue.submit + 白名单校验 | 同 | 已合规 |
| 任务日志 | API:task_queue.get_output | 同 | 已合规 |
| 队列状态 | API:task_queue.get_status_summary | 同 | 已合规 |
| 策略列表 | API:strategy_api.strategy_choices / live_signal_choices | 同 | 已合规 |

**状态**：✅ 完全合规。所有 CLI 执行均通过白名单校验的 task_queue 提交。

---

#### 💼 手动交易 (manual_trading.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 账户快照 | API:manual_trading_api.account_view | 同 | 已合规 |
| 持仓明细 | API:manual_trading_api.account_view | 同 | 已合规 |
| 计划列表 | API:manual_trading_api.plans_view | 同 | 已合规 |
| 计划详情 | API:manual_trading_api.plan_view | 同 | 已合规 |
| 订单列表 | API:manual_trading_api.plan_view | 同 | 已合规 |
| 成交列表 | API:manual_trading_api.fills_view | 同 | 已合规 |
| 执行复盘 | API:manual_trading_api.execution_view | 同 | 已合规 |
| 审批/取消/调减 | API:manual_trading_api.approve/cancel/adjust | 同 | 已合规 |
| 成交录入 | API:manual_trading_api.record_fill_action | 同 | 已合规 |
| 对账 | API:manual_trading_api.reconcile_action | 同 | 已合规 |
| 导出计划 | API:manual_trading_api.export_plan_action | 同 | 已合规 |
| 导入成交 | API:manual_trading_api.import_fills_action | 同 | 已合规 |
| Paper 模拟 | API:manual_trading_api.paper_trade_action | 同 | 已合规 |

**状态**：✅ 完全合规。

---

#### 📡 策略监控 (strategy_monitor.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 任务队列 | API:task_queue | 同 | 已合规 |
| 策略目录 | API:strategy_api.strategy_catalog | 同 | 已合规 |
| 白名单 | API:strategy_api.live_signal_choices | 同 | 已合规 |
| 调仓日历 | API:strategy_api.configured_strategy_schedule | 同 | 已合规 |
| 持仓分析 | API:manual_trading_api (repository.account_state + latest_prices) | 同 | 需封装为独立 API |

**问题**：`_get_holdings_data()` 直接调用 `repository().account_state()` 和 `load_stock_names()`，应封装到 api 层。

---

#### 🛡️ 风险管理 (risk_management.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 持仓数据 | API:portfolio_api.current_holdings | 同 | 已合规 |
| 行情数据 | API:portfolio_api.holding_bars / holding_price_frame | 同 | 已合规 |
| 股票名称 | common.load_stock_names (FS:data/stock_names.parquet) | API:data_api | ⚠️ 直读（缓存文件，可接受但需统一） |

**状态**：基本合规。`load_stock_names()` 读取缓存 parquet 文件，属于性能优化缓存层。

---

#### 🔬 因子研究 (factor_research.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 因子审计汇总 | API:research_api.factor_audit_summary | 同 | 已合规 |
| 因子 IC 历史 | API:research_api.factor_ic_history | 同 | 已合规 |
| 因子相关性 | API:research_api.factor_correlation | 同 | 已合规 |
| 因子定义 | quart.research.factor_audit.FACTOR_SPECS (MEM) | 同 | 已合规 |
| 状态摘要 | API:research_api.factor_audit_status_md | 同 | 已合规 |

**状态**：✅ 完全合规。

---

#### 🌿 因子生态 (factor_ecology.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 同因子研究 | API:research_api.* | 同 | 已合规 |

**状态**：✅ 完全合规。

---

#### 🔍 回测诊断 (backtest_diagnostics.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| WFA 制品 | API:artifacts_api.latest_wfa / read_table | 同 | 已合规 |

**状态**：✅ 完全合规。

---

#### 🧩 归因分析 (attribution.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 行业分布 | API:portfolio_api.latest_industry_trade_summary | 同 | 已合规 |
| 月度收益 | API:portfolio_api.latest_monthly_returns | 同 | 已合规 |
| 因子暴露 | API:portfolio_api.portfolio_factor_exposure | 同 | 已合规 |

**状态**：✅ 完全合规。

---

#### 🗃️ 数据总览 (data_overview.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 股票统计 | API:data_api.get_stock_stats | 同 | 已合规 |
| 股票池 | API:data_api.get_universe | 同 | 已合规 |
| 指数覆盖 | API:data_api.get_index_coverage | 同 | 已合规 |
| 股票列表 | API:data_api.get_stock_list | 同 | 已合规 |
| 日线数据 | API:data_api.get_stock_data | 同 | 已合规 |

**状态**：✅ 完全合规。

---

#### 📖 参数词典 (glossary.py)

| 数据 | 当前读取方式 | 目标读取方式 | 状态 |
|------|-------------|-------------|------|
| 配置值 | quart.config.load_config (MEM) | API:config_api | 需封装 |

**问题**：直接 import `quart.config.load_config` 和 `quart.strategy.build_strategy`，属于进程内调用但应走 api 层。

---

### 1.3 直读问题汇总

| 编号 | 页面 | 直读内容 | 风险等级 | 整改方案 |
|------|------|---------|---------|---------|
| DR-01 | daily_signal.py | FS:reports/signal_*.md | 中 | 由 research_api 统一读取并暴露 |
| DR-02 | daily_signal.py | FS:data/scores/preds.csv | 中 | 由 research_api / data_api 封装 |
| DR-03 | home.py | import BarStore/Repository 直调 | 低 | 封装为 data_api.get_freshness()、trading_api.get_account_summary() |
| DR-04 | risk_management.py | common.load_stock_names() | 低 | 封装为 data_api.get_stock_names() |
| DR-05 | glossary.py | quart.config.load_config() | 低 | 封装为 config_api.get_config_snapshot() |
| DR-06 | strategy_monitor.py | repository 直接调用 + load_stock_names | 低 | 封装为 manual_trading_api.get_holdings_summary() |

---

## 2. 交互原型

### 2.1 页面 × API 矩阵

```
┌─────────────────┬──────────────────────────────────────────────────────────────────────────┐
│    前端页面                      后端 API 依赖                                │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ home.py         │ backtest_api, research_api, strategy_api, manual_trading_api,           │
│                 │ data_bus                                                                 │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ backtest.py     │ backtest_api, research_api, strategy_api, task_api,                      │
│                 │ artifacts_panel, data_bus                                                 │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ daily_signal.py │ manual_trading_api, research_api, data_bus                               │
│                 │ [直读 reports/signal_*.md, data/scores/preds.csv]                        │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ operations.py   │ task_api, strategy_api                                                   │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ manual_trading  │ manual_trading_api, data_bus                                             │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ strategy_monitor│ task_api, strategy_api, manual_trading_api, data_bus                     │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ risk_management │ portfolio_api                                                            │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ factor_research │ research_api, quart.research.factor_audit                                │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ factor_ecology  │ research_api                                                             │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ backtest_diag   │ artifacts_api                                                            │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ attribution     │ portfolio_api                                                            │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ data_overview   │ data_api, task_api                                                       │
├─────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ glossary        │ [直读 quart.config.load_config]                                          │
└─────────────────┴──────────────────────────────────────────────────────────────────────────┘
```

### 2.2 API × 后端领域直调矩阵

```
┌─────────────────────────┬─────────────────────────────────────────────────────────────────┤
│    API 模块                              后端领域直调                      │
├─────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ task_api.py             │ subprocess (白名单校验)                                         │
├─────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ backtest_api.py         │ reports_dir/*.json, reports_dir/*.csv, BarStore                 │
├─────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ research_api.py         │ ArtifactStore, reports_dir/*.csv, reports_dir/*.md              │
├─────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ strategy_api.py         │ quart.strategy (REGISTRY), quart.config                         │
├─────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ manual_trading_api.py   │ TradingRepository (SQLite), BarStore, Fees                       │
├─────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ portfolio_api.py        │ TradingRepository, BarStore, quart.research                     │
├─────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ data_api.py             │ BarStore, data/universe, data/meta                              │
├─────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ artifacts_api.py        │ ArtifactStore (artifacts/*)                                     │
├─────────────────────────┼─────────────────────────────────────────────────────────────────┤
└─────────────────────────┴─────────────────────────────────────────────────────────────────┘
```

### 2.3 Job 控制台原型（待建设）

当前无独立 Job 控制台页面。任务队列信息分布在：
- `operations.py` — 「📡 当前任务队列」区（仅状态摘要 + 刷新）
- `strategy_monitor.py` — 「📋 任务队列」（摘要 + 取消）

**缺失交互**：
- 无 Job 历史列表（已完成/失败的任务不可追溯）
- 无 Job 详情页（参数、时间戳、数据版本、运行日志完整视图）
- 无 Job 与 Run Artifact 的关联（点击 Job → 产出的净值曲线/摘要）

### 2.4 Order/Risk 控制台原型（待建设）

当前订单和风控信息分布在：
- `manual_trading.py` — 仅 T+1 计划审批与成交回填（针对手动交易）
- `risk_management.py` — 持仓风险指标（无订单维度）

**缺失交互**：
- 无 OMS 订单列表/状态机视图
- 无风控审批/拒绝/限额交互
- 无订单-成交-持仓完整链路

### 2.5 交互流程原型（用户旅程）

#### 旅程 A：运行回测 → 查看结果

```
[首页] → (点击「📈 运行回测」) → [回测中心] → 选择策略/参数 → 点击「🚀 运行回测」
    ↓
[任务队列] 流式日志 → 完成后 data_bus 通知
    ↓
[回测中心] 自动刷新列表 → 点击行 → 查看净值/回撤/交易/成本
```

#### 旅程 B：数据刷新 → 生成信号 → 审批 → 成交

```
[操作中心] → 选择股票池/参数 → 点击「🔄 执行数据刷新」→ 完成
    ↓
[操作中心] → 选择策略 → 点击「生成信号与 DRAFT 计划」→ 完成
    ↓
[每日信号] / [手动交易] → 查看计划 → 审批/调减/取消
    ↓
[手动交易] → 成交录入/导入 → 对账
```

#### 旅程 C：因子审计 → 准入决策

```
[操作中心] → 因子审计参数 → 点击「运行统一因子审计」→ 完成
    ↓
[因子研究] → 查看 IC/ICIR/覆盖率/衰减
    ↓
[因子生态] → 查看预警（失效/冗余/覆盖退化）
    ↓
[研究报告] → 人工决策（不自动修改白名单）
```

---

## 3. DTO 需求清单

### 3.1 设计原则

1. DTO 字段与 ARCH-001 合同（OrderIntent / RiskDecision / ExecutionReport）对齐
2. 所有时间字段统一 ISO 8601 格式（`YYYY-MM-DD` 或 `YYYY-MM-DDTHH:MM:SS`）
3. 所有金额字段统一为浮点数值（元），不传格式化字符串
4. 枚举值使用字符串字面量，与后端 StrEnum 保持一致
5. 可选字段使用 `Nullable[T]` / `T | None`，不传空字符串代替

### 3.2 Job DTO（任务控制台）

#### JobSummary — 任务列表项

```python
@dataclass
class JobSummary:
    instance_id: str          # 任务实例 ID（如 "backtest#2"）
    task_id: str              # 任务族 ID（如 "backtest"）
    name: str                 # 显示名（如 "运行回测"）
    icon: str                 # 图标 emoji
    status: TaskStatus        # pending | running | completed | failed | cancelled
    progress_hint: str        # 人类可读进度
    created_at: str           # ISO 时间戳
    started_at: str | None    # ISO 时间戳
    ended_at: str | None      # ISO 时间戳
    resource: str             # "data" | "compute"
    returncode: int | None    # 进程返回码
    has_strategy_select: bool # 是否有策略选择
    result_tab: str | None    # 结果页签（如 "📈 回测中心"）
```

#### JobDetail — 任务详情

```python
@dataclass
class JobDetail:
    summary: JobSummary
    command: list[str]        # 执行的命令行（含参数）
    output_lines: list[str]   # 完整输出日志
    artifacts: list[JobArtifact]  # 产出文件清单
    strategy: str | None      # 关联策略
    params: dict[str, str]    # 运行参数

@dataclass
class JobArtifact:
    label: str                # 显示名（如 "回测摘要"）
    path: str                 # 文件路径
    size_kb: float            # 文件大小
    modified_at: str          # ISO 时间戳
    artifact_type: str        # "summary" | "equity" | "trades" | "report" | "other"
```

#### JobQueueStatus — 队列状态

```python
@dataclass
class JobQueueStatus:
    running: list[JobSummary]
    pending: list[JobSummary]
    recent_finished: list[JobSummary]  # 最近 5 个
    resource_usage: dict[str, int]     # {"data": 1, "compute": 2}
    resource_limits: dict[str, int]    # {"data": 1, "compute": 2}
```

### 3.3 Order DTO（订单控制台 — OMS）

#### OrderIntent — 订单意图（ARCH-001 统一合同）

```python
@dataclass
class OrderIntent:
    symbol: str               # 股票代码
    side: str                 # "BUY" | "SELL"
    quantity: int             # 数量
    order_type: str           # "MARKET" | "LIMIT"
    limit_price: float | None # 限价（市价为 None）
    strategy: str             # 策略标识
    planned_order_id: str | None  # 关联计划订单 ID
    client_order_id: str      # 客户端唯一 ID
    idempotency_key: str      # 幂等键
```

#### OrderState — 订单状态

```python
@dataclass
class OrderState:
    broker_order_id: str      # 券商/模拟订单 ID
    client_order_id: str      # 客户端 ID
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    avg_fill_price: float | None
    status: str               # "PENDING" | "SUBMITTED" | "PARTIAL" | "FILLED" | "CANCELED" | "REJECTED" | "EXPIRED"
    created_at: str
    updated_at: str
    rejects: list[RejectInfo] # 拒绝记录
    fills: list[FillRecord]   # 成交明细

@dataclass
class FillRecord:
    fill_id: str
    quantity: int
    price: float
    traded_at: str
    commission: float
    broker_fill_id: str | None

@dataclass
class RejectInfo:
    code: str                 # 拒绝码
    reason: str               # 原因
    rejected_at: str          # 时间
```

#### OrderListQuery — 订单查询参数

```python
@dataclass
class OrderListQuery:
    from_date: str | None     # 起始日期
    to_date: str | None       # 结束日期
    symbol: str | None        # 代码筛选
    status: str | None        # 状态筛选
    strategy: str | None      # 策略筛选
    limit: int = 50           # 每页数量
    offset: int = 0           # 偏移
```

### 3.4 Risk DTO（风控控制台）

#### RiskDecision — 风控决策（ARCH-001 统一合同）

```python
@dataclass
class RiskDecision:
    order_id: str             # 关联订单 ID
    decision: str             # "PASS" | "REJECT" | "ADJUST"
    rules_triggered: list[RiskRuleHit]  # 触发的规则
    adjusted_quantity: int | None  # 调整后数量
    max_allowed_quantity: int | None  # 最大允许数量
    reason: str               # 决策原因
    evaluated_at: str         # 评估时间

@dataclass
class RiskRuleHit:
    rule_name: str            # 规则名
    rule_type: str            # "POSITION_LIMIT" | "DAILY_LOSS" | "LIQUIDITY" | "CONCENTRATION"
    threshold: float          # 阈值
    actual: float             # 实际值
    action: str               # "WARN" | "REJECT" | "ADJUST"
```

#### RiskSnapshot — 实时风险快照

```python
@dataclass
class RiskSnapshot:
    as_of: str                # 评估时间
    total_value: float        # 总资产
    cash_total: float         # 总现金
    market_value: float       # 持仓市值
    hhi: float                # HHI 集中度
    effective_n: float        # 有效持仓数
    max_position_weight: float  # 最大单股权重
    var_95: float | None      # 日 VaR
    cvar_95: float | None     # 日 CVaR
    daily_pnl: float | None   # 当日盈亏
    daily_pnl_pct: float | None  # 当日收益率
    risk_level: str           # "low" | "mid" | "high"
    alerts: list[RiskAlert]   # 预警列表

@dataclass
class RiskAlert:
    level: str                # "info" | "warning" | "critical"
    category: str             # "concentration" | "liquidity" | "daily_loss" | "factor"
    message: str              # 预警信息
    symbol: str | None        # 相关代码
    value: float | None       # 当前值
    threshold: float | None   # 阈值
```

#### RiskLimits — 风控限额配置

```python
@dataclass
class RiskLimits:
    max_position_pct: float   # 单股最大权重
    max_daily_loss_pct: float # 单日最大亏损
    min_avg_amount: float     # 最低日均成交额
    max_weight_pct: float     # 策略单股目标权重
    max_positions: int | None # 最大持仓数
    max_sector_weight: float | None  # 单行业最大权重
```

### 3.5 Account DTO（账户/对账）

#### AccountSnapshot — 账户快照

```python
@dataclass
class AccountSnapshot:
    account_name: str
    as_of: str                # 日期
    cash_total: float         # 总现金
    cash_available: float     # 可交易现金
    cash_withdrawable: float  # 可取现金
    cash_frozen: float        # 冻结资金
    market_value: float       # 持仓市值
    total_value: float        # 总资产
    unrealized_pnl: float     # 浮动盈亏
    reconciliation_status: str  # 对账状态
    positions: list[Position] # 持仓明细

@dataclass
class Position:
    symbol: str
    name: str                 # 股票名称
    total_quantity: int       # 总持仓
    sellable_quantity: int    # 可卖数量
    frozen_quantity: int      # 冻结数量
    cost_price: float         # 成本价
    latest_price: float       # 最新价
    market_value: float       # 市值
    weight_pct: float         # 权重 %
    unrealized_pnl: float     # 浮动盈亏
    unrealized_pnl_pct: float # 浮动收益率 %
```

#### ReconcileResult — 对账结果

```python
@dataclass
class ReconcileResult:
    as_of: str
    confirmed: bool
    cash_total_diff: float    # 现金总额差异
    cash_available_diff: float  # 可用资金差异
    position_diffs: list[PositionDiff]  # 持仓差异
    preview: bool             # 是否为预览模式

@dataclass
class PositionDiff:
    symbol: str
    broker_quantity: int      # 券商数量
    local_quantity: int       # 本地数量
    quantity_diff: int        # 数量差异
    broker_price: float | None  # 券商价格
```

### 3.6 Strategy DTO（策略管理）

#### StrategyMeta — 策略元数据

```python
@dataclass
class StrategyMeta:
    name: str                 # 英文标识
    label: str                # 中文名
    status: str               # "准入" | "研究" | "候选"
    admitted: str             # 准入标记（"准入" / "-"）
    default_rebalance: int    # 默认调仓周期（交易日）
    default_top_k: int        # 默认持仓数
    description: str          # 说明
    current_params: dict[str, Any]  # 当前生效参数
```

#### StrategyCatalog — 策略目录响应

```python
@dataclass
class StrategyCatalog:
    strategies: list[StrategyMeta]
    live_allowlist: list[str]  # 白名单策略名列表
    default_strategy: str     # 当前默认策略
    next_rebalance_date: str  # 下次调仓日
    rebalance_countdown_days: int  # 倒计时（自然日）
```

### 3.7 Research DTO（研究审计）

#### FactorAuditSummary — 因子审计汇总

```python
@dataclass
class FactorAuditSummary:
    run_id: str               # 运行 ID
    run_timestamp: str        # 运行时间
    data_first_date: str      # 数据起始
    data_last_date: str       # 数据结束
    evaluation_first_date: str  # 评估起始
    evaluation_last_date: str  # 评估结束
    symbols: int              # 标的数量
    label: str                # 标签
    sample_points: int        # 样本点数
    factors: list[FactorMetric]

@dataclass
class FactorMetric:
    factor: str
    status: str               # 结论（候选/观察/冗余/失效）
    category: str
    is_new: bool
    in_strategy: bool
    ic: float
    icir: float
    positive_rate: float
    early_ic: float
    late_ic: float
    recent_ic: float
    fdr_qvalue: float
    coverage: float
    max_abs_corr: float
    corr_peer: str
```

### 3.8 Artifact DTO（运行制品）

#### ArtifactRun — 运行记录

```python
@dataclass
class ArtifactRun:
    run_id: str
    task_id: str
    task_name: str
    status: str               # "OK" | "FAILED" | "RUNNING"
    created_at: str
    fingerprint: str          # 数据指纹
    strategy: str | None
    params: dict[str, str]
    key_metrics: dict[str, float]  # 关键指标（CAGR/夏普/回撤等）
    artifact_paths: dict[str, str]  # 产出路径
```

---

## 4. UI-001 整改清单

基于盘点结果，前端直读整改需要的具体工作：

| 编号 | 页面 | 问题 | 优先级 | 工作量 | 依赖 |
|------|------|------|--------|--------|------|
| UI-001-01 | daily_signal.py | 信号报告直读 → API 封装 | P0 | 0.5d | RESEARCH-001 |
| UI-001-02 | daily_signal.py | ML 分数直读 → API 封装 | P0 | 0.5d | DATA-001 |
| UI-001-03 | home.py | 前端直接 import 领域对象 → API 封装 | P1 | 0.5d | ARCH-001 |
| UI-001-04 | strategy_monitor.py | 持仓直调 repository → portfolio_api | P1 | 0.5d | OMS-001 |
| UI-001-05 | risk_management.py | load_stock_names → data_api | P2 | 0.25d | DATA-001 |
| UI-001-06 | glossary.py | load_config 直调 → config_api | P2 | 0.25d | ARCH-001 |
| UI-001-07 | 全局 | 缺失 Job 控制台页面 | P1 | 2d | API-001 |
| UI-001-08 | 全局 | 缺失 Order/Risk 控制台页面 | P1 | 3d | OMS-001, RISK-001 |
| UI-001-09 | 全局 | 缺失 Strategy 配置管理页 | P2 | 1d | API-001 |

---

## 5. 与 DEVELOPMENT_COORDINATION.md 的映射

### 5.1 泳道 D 交付物 vs 本文档映射

| 泳道 D 交付物 | 本文档章节 |
|--------------|-----------|
| 页面/API 矩阵 | §2.1, §2.2 |
| 交互稿 | §2.3, §2.4, §2.5 |
| DTO 需求清单 | §3 |
| UI-001 清单 | §4 |

### 5.2 批次入口门槛检查

- [x] 领域合同 v1 与合同测试骨架通过评审（批次 0 出口）— ✅ 已完成（ARCH-001，2026-09-01）
- [x] 前端只做原型和 mock，不新增状态源 — **已遵守**
- [x] API 合同冻结前不新增状态字段 — ✅ 已满足（ARCH-001 完成，Control API v1 合同已冻结）

---

## 6. 后续行动

1. **批次 0 剩余**：与架构师确认 DTO §3 的字段命名与 ARCH-001 合同一致
2. **批次 1 启动**：开始 Job/Order/Risk 控制台的 mock 页面开发（用 §3 DTO 做 mock 数据）
3. **批次 2 同步**：API-001 完成后，将 mock 页面切换为真实 API
4. **批次 3 完成**：UI-001 全部整改关闭，frontend/ 无直读

---

*文档生成时间：2026-08-31*  
*关联工作项：UI-AUDIT-001 (批次 0), UI-001 (批次 3)*  
*关联文档：DEVELOPMENT_COORDINATION.md, TARGET_ARCHITECTURE_V3.md*

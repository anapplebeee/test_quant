# 策略与因子研究备忘录（RESEARCH 2026-09-02 核查 + 修复）

> 本备忘录为对前期"四部分调研结论"的**逐项核查记录**，以及随之执行的
> 策略/因子修复与 N1 因子验证。所有结论均附源码行号或本地数据证据；
> 无法在当前工作区验证的声明一律标注 `[MISSING]` 或 `[NOT REPRODUCIBLE]`。

---

## 0. 前置核查：引擎两个污染源是否真修好（结论：是）

| 污染源 | 核查位置 | 结论 |
|---|---|---|
| 卖滑点方向 | `quart/execution/backtest_model.py:53-55` | ✅ 正确：BUY→`base×(1+slip)`，SELL→`base×(1−slip)`（均为不利方向） |
| 同日未来函数 | `quart/backtest/engine.py:145,211,237` + `_rebalance` | ✅ 无泄漏：T 日收盘产信号（`signal_i = signal_offset + i`），T+1 开盘执行（`resolve_execution_prices(..., "open")`） |
| 执行日取价 | `engine.py:343-349 _previous_closes` | ✅ 用信号日收盘估值，无当日信息 |

**结论：负 alpha 是真实信号问题，不是测量假象。** 此结论与前期报告一致，
予以确认。`factor_audit_summary.csv` 也给出独立佐证：lowvol 系因子
`vol20_neg` ICIR=0.014、`amp20_neg` ICIR=0.022，基本无预测力——"只是带
摩擦费的 Beta"的说法有数据支撑。

---

## 1. 报告核查结论（哪些有问题）

### 1.1 ✅ 确认无误的结论

1. **dual_ma 无 CASH/FLAT 态（核心 bug）**：`quart/strategy/base.py:41-43`
   契约明确 `{}`=保持持仓、`{FLAT:1.0}`=清仓。`dual_ma.py`（旧版）在无金叉
   标的时返回 `{}` → 引擎"保持持仓" → 跌破均线的票被死扛。**实锤**。
2. **dual_ma 专挑最超买**：旧版 `dual_ma.py:58` 按 `fast/slow − 1` 降序选
   top-K = 追最强势。**实锤**。
3. **ml_rank 非 bug**：`ml_rank.py:94-96` 缺 `data/scores/preds.csv` 会抛
   `FileNotFoundError`，先跑 `train_ml.py` 即可。**实锤**。
4. **lowvol 系因子层 ICIR≈0**：`reports/factor_audit_summary.csv` 中
   `vol20_neg`/`amp20_neg` ICIR 分别为 0.014/0.022。**实锤**。

### 1.2 ❌ 报告有误 / 需修正的结论

| # | 报告原话 | 核查结果 | 严重度 |
|---|---|---|---|
| 1 | dual_ma "权重可超 100%" | **数学上不成立**：旧版 `weight = min(1/len(active), max_weight)`，总权重 `n×min(1/n, max_weight) ≤ 1.0`。这不是 bug，修复时不应改此逻辑 | 高（误导修复方向） |
| 2 | "factor_portfolio 的 600372 报错是并购退市 legacy 持仓" | **归因错误**：600372 是**中航机载**（原中航电子），吸收合并**中航机电(002013)** 后存续更名，**从未退市**。本地数据 `year=2019..2026/600372.parquet` 每年 230+ 行**连续无缺口**（2026 有 159 行真实行情）。真正被吸收方 002013 在本地**无数据**。artifacts 中也**无 factor_portfolio 失败运行记录** | 高（错误归因） |
| 3 | "N1 行业中性动量 ★优先，策略注释已证 ICIR 最稳" | **方向存疑**：A 股日线动量 IC 为负（`mom60` IC=−0.095、`mom120` IC=−0.040），行业相对**反转** `rel_ind_rev20` ICIR=0.74 才是候选；"行业中性动量"若做多行业强势，实证大概率 IC 为负。**须先跑单因子检验定方向**（见 §4） | 中（方向错误风险） |
| 4 | "完整备忘录在 factor_strategy_research.md / rebench_compare.csv" | **当前工作区不存在这两个文件**（全仓搜索无果），双基准原始数字（−6%~−52%）**无法复核** | 中（可追溯性） |

### 1.3 ⚠️ 报告未指出、但核查发现的真实问题

1. **`data/meta/security_master.parquet` 不存在** → `rule_resolver.py:106-108`
   的退市检查依赖 `delisted_at` 但该数据从未落地，退市过滤**形同虚设**。
2. **`filter_st` 每次回测都打东财网络接口**：`universe.py` 旧版 `filter_st`
   → `fetch_stock_list()` → `ak.stock_info_a_code_name()`（东财），接口
   慢/断网时回测**卡死数十分钟**（本次实测卡 23 分钟无输出）。已在本次修复
   中改为本地 `stock_names.parquet` 快照优先（见 §3）。
3. **申万一级行业数据缺失**：`data/universe/sw_industry.parquet` 不存在
   （`sw_industry_failed.txt` 为空），行业中性因子实际回退到统计聚类行业
   （`stat_industry.parquet`，40 类）。N1 验证须注明此口径。

---

## 2. dual_ma 修复（对应任务二）

### 修复内容（`quart/strategy/dual_ma.py`）

1. **无金叉 → 清仓**：`target_weights` 中 active 为空时返回 `{FLAT: 1.0}`
   而非 `{}`。
2. **剔除超买**：新增参数 `max_overshoot_pct`（默认 0.15），乖离
   `fast/slow − 1 > 阈值` 的标的剔除；全部超买则清仓等待回踩。
3. **金叉新鲜度优先**：选股从"乖离降序（最超买）"改为"乖离升序（刚上穿）"。
4. 保留等权 + `max_weight` 上限逻辑（**该逻辑本身无 bug，不修改**）。

### 测试

新增 `tests/test_dual_ma_strategy.py`（4 用例）：全下行→FLAT、超买剔除、
金叉新鲜度优先 + 权重上限、state 往返。全部通过（与既有 17 个回归用例
共 21 passed）。

### 回测验证

待 §5 回测对比结果（旧版 HEAD vs 新版）。

---

## 3. factor_portfolio / 退市处理修复（对应任务三）

### 3.1 数据层防线：退市过滤

新增 `quart/data/delisted.py`：
- `load_delisted()` / `delisted_map()`：读取 `data/meta/delisted.parquet`
  （code/name/delisted_at），文件缺失告警并返回空清单（不阻断）。
- `filter_delisted_bars()`：剔除退市日（含）之后的 bar，**保留退市前历史**
  （避免幸存者偏差）。

`filter_for_simulation()` 新增 `exclude_delisted=True` 参数并集成。

### 3.2 策略层防御：factor_portfolio 价格过滤

`quart/strategy/factor_portfolio.py:target_weights`：候选在 volume>0 过滤
基础上，新增"当日 `close_val > 0`"过滤——退市/停牌/无行情标的即使残留
alpha 面板值也不得进入 Constructor，避免不可估值持仓触发
`PortfolioInfeasibleError`。

### 3.3 基础设施：filter_st 离线化（本次核查发现的真 bug）

`quart/data/universe.py`：`filter_st` 优先读本地 `data/stock_names.parquet`
（5551 只快照），网络接口仅作兜底。实测映射构建 1.7s（此前卡 23 分钟）。

### 3.4 测试

新增 `tests/test_delisted_filter.py`（5 用例）+ factor_portfolio 价格防御
用例。全部通过（16 passed）。

### 3.5 关于"600372 报错"的最终裁定

当前工作区**无法复现**报告所称的 factor_portfolio 600372 报错：
- 600372 从未退市（中航机载），数据连续真实；
- artifacts 中无 factor_portfolio 失败运行记录；
- 002013（真正退市方）本地无数据。

判断：报告的"幸存者 bug"归因建立在**并购双方混淆**的错误事实上。本次
修复聚焦真实缺口（退市过滤缺失 + 网络依赖卡死），并以价格防御兜底。

---

## 4. N1 因子验证（对应任务四，待审计结果填充）

- 新增因子 `rel_ind_mom20`（行业中性 20 日动量）到 `FACTOR_SPECS`
  （`quart/research/factor_audit.py`），实现 `_relative_industry_momentum()`。
- 与既有 `rel_ind_rev20`（行业中性反转）同口径对比，跑
  `scripts/factor_audit.py` 五步流水线（winsorize 由
  `factor_audit.py:454-455` 的横截面 rank 完成、T+1 可执行标签、
  分层多空）。
- 行业口径：申万一级缺失，实际为统计聚类行业（40 类）。
- **结论以 IC/RankIC/ICIR 实证方向为准**。

### 4.1 N1 审计结果（2022-01 起，月度样本，T+1 可执行标签）

| 因子 | 状态 | IC | ICIR | 后半段 | 近期 | 覆盖 |
|---|---|---|---|---|---|---|
| rel_ind_rev20（行业中性反转） | 观察 | **+0.0372** | **+0.27** | +0.0537 | +0.0523 | 98% |
| rel_ind_mom20（行业中性动量） | 淘汰 | **−0.0372** | **−0.27** | −0.0537 | −0.0523 | 98% |

两者互为反方向（|corr|=1.00）。结论：

1. **报告建议的 N1（做多行业强势）方向错误**：`rel_ind_mom20` IC=−0.037、
   ICIR=−0.27、后半段/近期均为负 → 在 A 股主板上"行业中性动量"不成立，
   **淘汰**。
2. **有效方向是行业中性反转**：`rel_ind_rev20` IC=+0.037、ICIR=+0.27，
   后半段 +0.054、近期 +0.052 方向稳定；此前全样本审计（
   `reports/factor_audit_summary.csv`）其 ICIR=0.74、状态"候选"。两窗口
   方向一致 → **N1 的正确形态 = 行业中性反转，即复用/强化 rel_ind_rev20，
   无需新增同构动量因子**。
3. 注意事项：ICIR 0.27~0.74 未稳定越过 0.5 门槛（受窗口影响），且行业
   口径为统计聚类（申万一级缺失）。**rel_ind_rev20 的工程化应待申万一级
   行业数据补齐后复验**，当前列为"观察/候选"而非直接入策略。

---

## 5. 回测结果

### 5.1 dual_ma 修复前后对比（2023-01 ~ 2025-12，主板池，含成本）

| 指标 | OLD（HEAD 修复前） | NEW（修复后） | 变化 |
|---|---|---|---|
| 年化收益 | −56.8% | −20.8% | 显著改善 |
| Sharpe | −1.65 | −0.87 | 改善 |
| **最大回撤** | **−94.8%** | **−56.6%** | **灾难性回撤消除** |
| 累计收益 | −91.9% | −50.2% | 改善 |
| 交易笔数 | 2130 | 2734 | — |

**解读**：修复消除了"死扛 + 追最超买"导致的 −95% 级回撤；但策略在
2023-2025 震荡市**仍亏损**（Sharpe −0.87），说明均线金叉趋势策略在该
市况无 alpha——与"负 alpha 是真实信号问题，非测量假象"的结论一致。

### 5.2 factor_portfolio 可行性验证（2021-01 起，vol20_neg+amp20_neg+lottery20_neg）

- **回测成功跑通**（EXIT 0，1832 笔交易，无 `PortfolioInfeasibleError`、
  无 600372 报错）→ 报告所称"组合不可行"**在当前环境不可复现**。
- 指标：cagr −8.9%，Sharpe −0.55，最大回撤 −50.0%，
  **相对 000300 指数基准超额 −7.2% / 相对等权基准超额 −22.9%**。
- **解读**：组合构建机制工作正常；负超额来自 lowvol 系因子本身
  ICIR≈0.01~0.02（纯 Beta），再次印证"负 alpha 是真实信号问题"。

### 5.3 基础设施修复验证

- `filter_st` 离线化：本地 `stock_names.parquet` 快照构建映射 1.7s
  （此前每次回测阻塞在东财网络接口，实测卡 23 分钟无输出）。
- 退市过滤：`data/meta/delisted.parquet` 尚缺失（须先运行
  `scripts/build_delisted_list.py` 拉取退市清单），缺失时告警降级不阻断。

---

## 6. 引擎全面检视（2026-09-02 追加）

### 6.1 重大修复：超额年化口径（commit 81c8c20）

**发现**：`metrics.py._bench_metrics` 原先用
`excess_cagr = cagr(策略) − cagr(基准)`（算术差）。基准强势时（如主板
等权年化 +100% 以上）会把"跑输"夸大近一个量级。实测：策略 +20% vs
基准 +145% → 算术差报 **−125%**，相对净值年化仅 **−51%**。

**修复**：`{name}_excess_cagr` 改为相对净值口径
`cagr((eq/eq0)/(bench/bench0))`，与 `factor_audit` 的 `relative_bp` 一致。
键名不变，前端/api/WFA 选择指标零改动兼容。

**含义**：前期报告/回测中的"对等权宇宙负超额 −6%~−52%"是算术差口径，
**数字被系统性夸大**；方向（负 alpha）不变，但量级需按相对净值重新解读。

### 6.2 审查确认无问题/已知限制

| 项 | 结论 |
|---|---|
| T+1 卖出限制 | ✅ `test_t_plus_one_buy_not_sold_same_day` 覆盖，先卖后买天然满足 |
| 停牌过滤 | ✅ 通过执行价 NaN → skipped "停牌/无行情"，tradable 掩码实盘路径使用 |
| 整手取整/现金缓冲 | ✅ `cash_buffer=0.995` + 整手取整 + 费用预留，无漏洞 |
| 涨跌停拒单 | ✅ 开盘一字涨停买不进/跌停卖不出（RuleBook + 板块幅度） |
| vwap 成交场景 | ⚠️ 已知限制：qfq 价格 vs 真实成交额的复权失真相位时 vwap 失真，超出 [low,high] 时回退 typical（有 fallback 计数） |
| 退市股 ffill 幽灵估值 | ⚠️ 已知限制：`close_val = closes.ffill()` 会把退市股最后价延伸到近期；配合 delisted 数据层过滤后缓解，估值仍按最后价（保守不清零） |
| WFA 边界 | ⚠️ `_ScheduledStrategy.sync_positions` 仅在 fold 切换后下一次撮合才同步给新 fold，持仓记忆类策略首日可能用旧持仓（轻微） |
| 行业中性因子 | ⚠️ 申万一级数据缺失，实际用统计聚类行业（40 类）；静态映射非 PIT（provisional） |

### 6.3 新因子挖掘（2026-09-02，commit 85acbfd）

新增 4 因子审计结果（2022-01 起，月度样本，T+1 可执行标签）：

| 因子 | 状态 | IC | ICIR | 后半段 | 近期 |
|---|---|---|---|---|---|
| **intraday_rev10_neg**（10 日日内收益反转） | **候选** | **+0.0726** | **+0.45** | +0.0657 | +0.0891 |
| rel_ind_rev60（60 日行业中性反转） | 观察 | +0.0342 | +0.28 | +0.0416 | +0.0606 |
| close_pos20_neg（收盘位置） | 观察 | +0.0065 | +0.03 | +0.0104 | +0.0698 |
| overnight_rev10（隔夜反转） | 淘汰 | +0.0035 | +0.03 | −0.0068 | −0.0113 |

**结论**：`intraday_rev10_neg` 是本次挖出的最佳新 alpha（ICIR 0.45 候选、
方向稳定）；`rel_ind_rev60` 佐证行业中性反转在 60 日窗口仍成立（弱于 20 日）；
隔夜反转与收盘位置无效。新策略组合回测见 §7。

### 6.4 停牌超限持仓崩溃修复（commit 4e7b537）

**真实 bug（实测）**：factor_portfolio 新因子组合回测在 600803 停牌日抛
`PortfolioInfeasibleError「不可交易持仓超过单票上限」`——持仓中某股停牌
（volume=0 → frozen）且权重被动超 max_weight 时 constructor 直接 raise，
整个回测崩溃。**这正是前期报告所述"组合不可行"bug 的真实形态**：
股票不是 600372（归因错误），但"停牌/退市持仓未容错"类型属实。

修复：冻结持仓豁免单票上限（保留至解冻后自然回落，frozen 总权重仍受
investable_cap 硬约束）；可交易持仓仍由贪心分配限制在 max_weight 内。
新增测试：冻结超限容忍（30% > 上限 10% 保留不报错）。

---

## 7. 新策略组合回测（2026-09-02）

factor_portfolio 新旧因子组合对比（2021-01 起，修复后相对净值口径）：

| 指标 | 旧配置（vol20_neg+amp20_neg+lottery20_neg） | 新配置（intraday_rev10_neg+rel_ind_rev20+rev5） |
|---|---|---|
| 年化收益 | −8.90% | **−6.25%** |
| Sharpe | −0.55 | **−0.03** |
| 最大回撤 | −50.0% | −57.8% |
| 对 000300 指数超额 | −7.29% | **−4.59%** |
| 对主板等权超额 | −20.05% | **−17.73%** |
| 交易笔数 | 1832 | 3161 |

**结论**：
1. 新因子组合**跑通**（600803 停牌崩溃已修复），且**显著改善**：Sharpe
   −0.55 → −0.03，对等权负超额收窄 2.3pp。
2. 仍为**负 alpha**——单因子 ICIR 0.45 未过 0.5 门槛、top-10 月度调仓
   仍不足以战胜主板等权基准（年化 +13.95%）。与"先拉 ICIR 过 0.5 再谈
   正 alpha"的顺序判断一致。
3. 回撤变差（−50% → −57.8%）：反转类因子组合在趋势市（2024-2025 部分
   时段）承受更高波动，需结合择时/降频调仓缓解。

**下一步（行动分类·按性价比）**：
① 把 `intraday_rev10_neg` 与 `rel_ind_rev20` 纳入合成并调参
（top_k/调仓频率）→ ② 补申万一级行业数据复验反转因子 →
③ 验证 ICIR 是否过 0.5 → ④ 达标后再接入正式策略组合。

---

## 附：数据与文件

- 修改：`quart/strategy/dual_ma.py`、`quart/strategy/factor_portfolio.py`、
  `quart/data/universe.py`、`quart/research/factor_audit.py`、
  `tests/test_factor_portfolio_strategy.py`
- 新增：`quart/data/delisted.py`、`tests/test_delisted_filter.py`、
  `tests/test_dual_ma_strategy.py`
- 证据：`reports/factor_audit_summary.csv`、`data/daily/year=*/600372.parquet`、
  `data/stock_names.parquet`、`data/universe/stat_industry.parquet`

> 本报告仅供研究参考，不构成个人投资建议。

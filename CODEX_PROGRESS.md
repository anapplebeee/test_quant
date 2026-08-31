# 执行进度快照（2026-08-31 深夜更新）

> 本文件由 **GLM**（WorkBuddy 会话）维护，记录 Codex 规划的执行进度。
> 轮次：
> - R1（Codex）：手动交易闭环 + 默认策略切换 + 交易日历/模拟盘 → commit `77a182e` / `2e7f1fb`
> - R2（GLM）：规划遗留项收尾 + 全量测试 → `2650497`
> - R3（GLM）：策略根因分析与参数落地 + 前端体验修复 → `fe234e3` / `57e1789`
> - R4（GLM）：前后端交互梳理 + 指数板块分类 → `1daa81f` / `c068084` / `0e6d5a8`
> - R5（GLM）：阶段 F 券商 API 准备 + 全量代码审查 → `055e132` / `6bfa5b4`
> - R6（CatPaw）：研报因子落地（技术因子库/打分择时/PIT价值成长接入 ML）→ `6da0917` / `836c17f`；本轮完成 P0#1 数据质量治理（分类阻断）+ P1#3 准入自动化门禁（见下）

## 一、Codex 规划事项完成度总表

### FRONTEND_STRATEGY_DEVELOPMENT_PLAN.md

| 事项 | 状态 |
|---|---|
| P0 前后端账户与交易闭环（api/manual_trading_api 隔离、手动交易页、统一 SQLite 读取、调减/取消/过期/CSV 导出、成交匹配与偏差统计、对账门禁） | ✅ 全部完成 |
| P0 命令操作前端化（任务白名单、参数化表单、长任务队列/日志/取消、写盘二次确认） | ✅ 全部完成 |
| P1 默认策略切换 lowvol_indz、配置优先级修复（显式>overrides>全局） | ✅ 完成 |
| P1 低换手/缓冲带/分散持仓 | ✅ 完成；全市场复验后回退至 **top30/45d/rev0**（小池 top50/60/rev0.3 结论不迁移） |
| P1 前端区分研究/候选/准入状态 | ✅ 完成（准入状态表 + live_allowlist 对齐测试） |
| **P1 低波策略加入稳健截尾、多窗口风险和趋势质量过滤** | ⬜ **未完成**（待做，见第五节） |
| **P1 策略变更的准入自动化校验（回测+成本压力+WFA 通过后才升级正式信号）** | ⬜ **未完成**（流程已定义在报告中，自动化校验待补，见第五节） |
| P1 文档与验证（README 重构、规划文档同步、回归测试） | ✅ 完成 |

### MANUAL_TRADING_T1_SYNC_PLAN.md

| 事项 | 状态 |
|---|---|
| 阶段 A-E（账本模型/计划语义/T+1 工作流/前端） | ✅ 全部完成 |
| 阶段 F：BrokerAdapter 契约、PaperBroker 状态机、**回报统一写入 FillService**（`quart/broker/sync.py`）、**API 订单状态机**（`paper_trade_action`）、人工模式保留 | ✅ 全部完成（R5） |
| XLSX 券商成交列映射 + 自动分流 | ✅ 完成（R5，`convert_broker_xlsx` + `import_broker_fill_file`） |
| 具体券商 SDK 接入 | ⬜ 待做（需券商权限/联调环境，按规划"稳定运行后"实施） |

## 二、GLM 各轮工作摘要

- **R2**：券商 CSV 映射、端到端流水线测试、节假日测试、策略准入表、store 空 glob 修复（274 tests）
- **R3**：15 组参数实验 + WFA；市场环境分析（2026-08 末 PE 分位 87-97% 高估/高低切换）→ 曾落地 lowvol_indz top50/60d/rev0.3，后经全市场复验回退 top30/45d/rev0；前端红涨绿跌等修复
- **R4**：ARCHITECTURE_OVERVIEW.md（分层/时序/映射/不变量）；修复 data_api/backtest_api 旧布局直读（股票数=0）、归因/诊断/生态页假数据；指数按板块分类（9 指数拉取至 2026-08-28）
- **R5**：阶段 F 券商 API 准备；**全量代码审查修 11 项真实问题**（财务因子前视偏差、PIT 空列表回退、腾讯源 volume 单位 100x、制品指纹失效等）

## 三、全量测试

```text
.venv/Scripts/python.exe -m pytest tests/ -q
=> 330 passed, 6 warnings（本轮已完成；pytest 临时目录置于工作区后已清理）
新增测试累计：e2e 流水线、broker 映射/同步、节假日、准入对齐、volume 单位归一化、
WFA 历史预热/连续账户等
```

## 四、关键决策记录

| 决策 | 内容 | 依据 |
|---|---|---|
| 默认策略参数落地 | lowvol_indz → **top30/45d/rev_weight=0** | 全市场 CAGR 7.53%/Sharpe 0.69；2 倍成本仍 CAGR 6.09%/Sharpe 0.57，优于 top50/60/rev0.3 |
| 参数预期打折 | 自适应 WFA 衰减比 0.93，但参数一致率 50%/62%/62%，3/8 折无成交；固定当前参数 OOS CAGR 5.46% 亦成交稀疏 | `reports/full_market_validation_2026-08-31.md` |
| 数据源现状 | 腾讯主源 + 东财兜底；volume 单位已统一为手；9 指数已覆盖 | `reports/code_review_2026-08-31.md` |

## 五、未完成事项清单（按优先级）

1. **数据质量治理**（P0/P1）：全市场已覆盖 5409 只，但仍有 1251 个单日绝对收益超过 25% 跳变；需清洗/阻断后复验
2. **P1 低波策略增强**：稳健截尾、多窗口风险、趋势质量过滤（FRONTEND 规划 [ ]）
3. **P1 准入自动化校验**：策略变更跑通"回测+成本压力+WFA"门禁后自动升级正式信号（当前靠人工流程）
4. **阶段 F 真实券商 SDK 接入**：需券商权限与联调环境
5. **口径变更待确认**：ST 历史过滤、等权基准停牌口径、qfq/hfq 边界混仓（见 `reports/code_review_2026-08-31.md` 暂缓清单）

## 六、待办（用户下一轮指令）

- [ ] 接入更多稳定数据源（当前腾讯+东财双源，可补充新浪等）
- [x] 数据更新支持全量刷新模式（`scripts/update_data.py --full-refresh`，空响应不覆盖旧数据）
- [x] WFA 连续上下文与预热（默认 continuous；`--account-mode independent` 可做单折诊断）
- [x] 正式回测 PIT 股票池门禁（`--research-mode formal` + `filter_for_pit_universe`；探索模式标记 `NON_PIT`）

## 七、本轮研报策略落地与验证（2026-08-31）

### 已完成

- [x] 完成 `D:\doc\邢不行——量化投资新手学习大礼包\3 邢不行-量化投资研报合集\邢不行研报合集` 下 15 份研报的逐篇分析；结论按可复现性、数据要求和当前平台适配性分级，未将报告原参数直接升级为实盘信号。
- [x] 新增 `quart/research/momentum.py`，实现简单动量、跳过近期收益动量、平滑路径动量和近似去涨停动量。
- [x] 因子研究目录注册 `RankMom120_20`、`Smooth240`、`Ret240_20_RemoveUpLimit`；滚动 VWAP 统一为 `Σamount / Σvolume`，避免日均 VWAP 口径偏差。
- [x] 新增研究策略 `momentum_path`：默认 Smooth240、20 日调仓、Top20、score timing；仅作为 research 状态，未加入 `strategy.live_allowlist`。
- [x] 完成 CLI、任务 API 白名单、策略元数据、前端策略目录和因子研究脚本接线。
- [x] 修正 WFA 衰减诊断：样本内指标均值非正时不再用“负/负”比值误判为参数稳健。
- [x] 已生成真实本地行情验证产物：`reports/momentum_path_validation_2026-08-31/`；所有结果明确标记为 `NON_PIT exploratory`。

### 验证结论

- [x] `momentum_path` 未通过当前准入：Smooth240 在 0x/1x/2x 成本下 CAGR 分别为约 `+1.02%/-0.36%/-2.37%`，Sharpe 分别为 `0.15/0.08/-0.02`。
- [x] 固定参数 WFA（8 折、连续账户）OOS 累计 `-59.06%`、年化 `-19.37%`、Sharpe `-0.94`、最大回撤 `-67.92%`；结论为不稳健，禁止晋级实盘。
- [x] RankMom120/20 与 RemoveUpLimit240/20 在当前探索数据上同样未通过；月度 RankIC/ICIR 也未显示稳定正向优势（Smooth240 约 `-0.046/-0.29`）。
- [x] 当前本地股票池仅有少量日期快照，历史成分、ST/停牌状态和财务披露时点不完整；因此以上结果不能替代正式 PIT 研究结论。

## 八、未完成事项（本轮更新）

### P0：研究结论和生产准入前置条件

1. **正式 PIT 数据补齐**：建立逐交易日指数成分、上市/退市、ST、停牌和行业历史；补齐财务报表实际披露日及修订版本，才能重跑研报因子与 WFA。
2. ~~**数据质量治理**~~ ✅ **R6 已完成代码与复验**（详见第九节）：跳变分类器落地，1251 行跳变复验为 832 行合法（涨停/新股/复牌）+ 419 行异常（161 只符号）；阻断/隔离机制已接入回测与更新器，`--apply` 隔离待用户确认执行。
3. ~~**准入自动化门禁**~~ ✅ **R6 已完成**（详见第九节）：`quart/research/admission.py` + `scripts/admission_gate.py`，0/1/2x 成本压力 + WFA 自动评估，`admission_status.csv` 台账 + grandfathered 引导，pytest 强制白名单准入记录。

### P1：策略、数据和交易接入

4. **低波策略增强**：✅ 代码落地（R6，详见第九节）——多因子合成（`vg_weight`）+ 组合构造（`weight_mode=inv_vol/zscore`）+ PIT 财务面板管线均已就位并可配置；真实数据 4 组对比为**负结果**（基准 equal 7.56%/0.69 优于全部变体），默认配置保持不变。负结果根因：financials 仅覆盖沪深300（~5%符号），合成稀释低波信号。**复验前置条件：PIT 财务覆盖扩至全市场**（P0#1 关联）。
5. **研报动量策略后续**：在 PIT 数据、涨跌停/板块规则准确后仅做复验；当前不继续调参，也不提升为正式信号。
6. **涨跌停与证券状态精确化**：`remove_limit_up` 目前为阈值近似，需接入板块、ST、历史涨跌停规则字段。
7. **容量与可交易性评估**：补充 ADV、自由流通市值、成交额冲击和组合容量约束。
8. **稳定数据源扩展**：在腾讯+东财双源之外补充并验证第三数据源，建立源间一致性监控。
9. **阶段 F 真实券商 SDK**：需明确券商、权限、联调环境和回报字段映射后实施。
10. **口径待确认**：历史 ST 过滤、等权基准停牌处理、qfq/hfq 边界混仓规则仍需业务确认并写入配置/测试。

## 九、R6（CatPaw）：数据质量治理 + 准入自动化门禁（2026-08-31）

### 数据质量治理（P0#2）

**新模块**：

- `quart/data/quality.py`：A股涨跌停幅度按板位判定（主板±10%、创业板300/301与科创板688±20%、北交所43/83/87/88/92±30%）；`classify_jumps` 将巨幅跳变分为 4 类，按优先级：`resume_gap`（前日停牌）→ `new_stock`（上市≤10个交易日）→ `limit_move`（|ret| ≤ 1.05×板位涨跌停幅度）→ `anomaly`（物理不可能，判为数据缺陷）；`build_blocklist` 仅收 anomaly 符号；`save/load_blocklist`（`data/meta/quality_blocklist.csv`）；`quarantine_symbols` 将隔离符号的行情文件移入 `data/quarantine/`。
- `scripts/data_quality_scan.py`：重写为逐符号流式扫描（避免全市场 concat 内存峰值），输出分类明细表 + `reports/data_quality_scan.csv`；默认只读报告，`--apply` 才写阻断清单并隔离数据文件。
- 接入点：`BarStore.load(exclude_symbols=...)` 双路径过滤；`scripts/run_backtest.py` 回测前加载阻断清单剔除；`quart/data/updater.py` 更新时跳过阻断符号，防止缺陷数据回流。

**全市场复验结果**（5411 只，|ret|>25% 阈值）：

| 指标 | 数值 |
|---|---|
| 跳变行总数 | 1251 |
| ↳ limit_move（涨跌停，合法） | 657 |
| ↳ new_stock（新股首期，合法） | 173 |
| ↳ resume_gap（停牌复牌，合法） | 2 |
| ↳ **anomaly（异常）** | **419 行 / 161 只符号** |

典型异常样本：601225 2019-11-25 ret=+1700%、601088 +889% —— 与暂缓清单记录的 **qfq/hfq 边界混仓** 已知缺陷精确吻合；另有 '2087'/'2088' 等 4 位短代码符号疑似代码解析问题。**结论：419 行异常即复权混仓/源数据缺陷的定位结果。**

**待用户决策**：执行 `uv run python scripts/data_quality_scan.py --apply` 会把 161 只符号的数据文件物理隔离到 `data/quarantine/` 并写入阻断清单（可回滚）。鉴于部分异常行是复权伪影（整段历史可能被污染），建议确认后执行；或将隔离粒度细化到日期区间（后续优化项）。

**测试**：`tests/test_data_quality_and_admission.py` 覆盖板位幅度、各分类、阻断清单读写、隔离、exclude_symbols 双路径、门禁评估等 18 例，全量回归 348 passed。

### 准入自动化门禁（P0#3）

- `quart/research/admission.py`：默认阈值（2x 成本 CAGR≥0、1x Sharpe≥0.5、1x 最大回撤≤-0.45、1x 超额 CAGR≥0、交易数≥30、WFA OOS CAGR≥0 / 回撤≤-0.60）；`evaluate_gates` 缺任何成本维度或 WFA 结果即 FAIL；`admission_status.csv` 台账保留 grandfathered 标记；`seed_grandfathered` 首次引导存量白名单；`admission_ok(strategy)` 供前端/监控查询。
- `scripts/admission_gate.py`：进程内跑 0/1/2x 成本压力回测（`Fees.from_config().scaled()`）+ subprocess WFA（`scripts/walk_forward.py`，解析 artifacts 路径取 oos_summary.json）→ rich 结果表 → 未通过除 `--no-apply` 外自动写台账并 exit(1)。用法：`uv run python scripts/admission_gate.py --strategy momentum_rotation [--skip-wfa]`。
- 强制执行：pytest 用例 `test_live_allowlist_has_admission_record` —— 白名单策略若台账缺失且非首次引导即测试失败，实现"未过门禁不得进 live_allowlist"。

### 低波策略增强（P1#4）：多因子合成 + 组合构造升级（含负结果）

**代码落地**：

- `quart/research/value_growth.py` 新增 `pit_panels()`：PIT 价值成长因子宽表面板（date×symbol，报告期+120d 披露时滞防前视），供策略层 prepare() 一次构建。
- `quart/strategy/lowvol_composite.py` 新增参数：
  - `vg_weight`（默认 0=关闭）：复合分 = (1-w)·低波z + w·价值成长z（roe_improve/profit_yoy/ep/bp 截面 z 等权）。无财务覆盖的符号中性填 0，缺文件/损坏优雅降级为纯低波。
  - `weight_mode`（默认 equal）：`inv_vol` 波动率倒数加权（单票风险预算均等）、`zscore` 因子分数加权（保留因子强度横截面信息），迭代截断至 max_weight_pct 并归一化。
- `scripts/run_backtest.py` 暴露 `--weight-mode` / `--vg-weight`。
- 测试：`tests/test_portfolio_construction.py` 13 例（pit_panels 防前视/定价、vg 混合公式、三种权重模式归一化/截断/NaN回填、equal 历史行为保持、未知模式回退）。全量回归 360 passed。

**真实数据 4 组对比**（lowvol_indz 全配置，2020-01~2026-08，1x 成本，top30/45d/buffer0.5/industry_z）：

| 组 | CAGR | Sharpe | MDD | 超额年化 | Calmar |
|---|---|---|---|---|---|
| A 基准（equal） | **7.56%** | **0.69** | -23.06% | **5.97%** | **0.33** |
| B inv_vol | 7.06% | 0.68 | **-22.75%** | 5.48% | 0.31 |
| C vg_weight=0.3 | 6.74% | 0.65 | -23.39% | 5.16% | 0.29 |
| D vg0.3+inv_vol | 6.41% | 0.65 | -23.86% | 4.83% | — |

**诚实结论：负结果，默认配置不变**（equal + vg_weight=0，即现行已准入形态）。根因分析：

1. **覆盖不足（主因）**：`data/factors/financials.parquet` 仅覆盖沪深300（约 300 只 / 全池 ~5400 只 ≈ 5%）。vg 合成只牵引 5% 符号，其余中性填 0 → 经 industry_z 后产生结构性扭曲（有财务的票整体向中性回归），稀释而非增强低波截面。
2. **ep/bp 口径缺陷**：EPS/BPS 为报告期累计（非 TTM），季节性导致 Q1/Q3 与 Q2/Q4 截面不可比。
3. inv_vol 轻微降收益但回撤改善极小（-0.31pp）：top30 分散后个票波动差异已小，风险预算均衡化空间有限。

**诊断路线图的验证**："因子广度是第一放大器"成立，但前置条件是**因子覆盖全池**——下一步应把 `quart/data/factors.py` 的 fetch_financials 从沪深300 扩到全市场（与 P0#1 PIT 数据补齐合并执行），并修 ep/bp 为 TTM 口径后再复验 vg 合成。基础设施已就位，届时只需改配置即可重跑对比。

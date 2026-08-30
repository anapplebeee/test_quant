# Quart — A股中低频量化研究 & 选股信号平台

基于成熟开源组件构建的 A 股日线级量化工具链：**AKShare 数据 → 本地 Parquet/DuckDB 仓库 → A股规则回测引擎 → 动量/双均线策略 → 每日自动选股信号推送（钉钉）→ 人工下单执行**。

## 架构

```
data/            AKShare(东财→腾讯自动降级) 日线采集, 前复权
quart/
├── data/        BarStore(Parquet+DuckDB) / 多源采集 / 中证指数成分股票池
├── backtest/    T+1 · 100股整手 · 佣金/印花税/过户费/滑点 撮合引擎 + 绩效指标
├── strategy/    统一 Strategy 接口: momentum_rotation / dual_ma / ml_rank / lowvol_composite / lowvol_indz (可注册扩展)
├── risk/        单票权重上限校验、持仓集中度告警
├── manual_trading/ SQLite 账户账本 / T+1 持仓批次 / 计划审批 / 成交回填 / 对账
├── notify/      钉钉机器人推送(支持加签)
├── pipeline.py  每日流水线: 数据更新→选股→风控→交易计划→报告/推送
scripts/         update_data.py · run_backtest.py · daily_signal.py
run_scheduler.py APScheduler 每交易日 17:30 自动执行
tests/           pytest: 撮合精度/T+1/无未来函数/指标数学 验证
```

## 快速开始

```powershell
# 1. 环境 (Python 3.12, uv 管理)
uv sync

# 2. 下载沪深300成分股日线 (首次约10分钟, 之后增量秒级)
uv run python scripts/update_data.py --index 000300 --start 20240101

# 3. 回测
uv run python scripts/run_backtest.py --strategy momentum_rotation --start 2024-01-01
uv run python scripts/run_backtest.py --strategy dual_ma --no-regime

# 4. 生成当日选股信号 (控制台 + reports/signal_YYYYMMDD.md)
uv run python scripts/daily_signal.py

# 5. 常驻定时任务 (可选)
uv run python run_scheduler.py
```

## 手动交易 T+1 同步

当前阶段仍由用户在券商客户端手动下单。平台负责：

```text
T 日收盘生成信号
→ 创建 T+1 DRAFT 交易计划
→ 用户审批并在券商端手动下单
→ 人工录入或 CSV 导入真实成交
→ 更新现金、费用、持仓批次和 T+1 可卖数量
→ 收盘后与券商账户快照对账
→ 使用已对账状态生成下一交易日计划
```

账户状态保存在 `state/trading.db`。`state/holdings.json` 仅作为首次迁移兼容格式：

```json
{
  "cash": 50000,
  "positions": { "600519": 200, "601318": 800 }
}
```

### 1. 初始化账户

已有 `state/holdings.json`：

```powershell
uv run python scripts/manual_trade.py init --as-of 2026-08-28
```

空账户：

```powershell
uv run python scripts/manual_trade.py init --as-of 2026-08-28 --cash 1000000
```

初始化后不再直接编辑 `holdings.json`。

### 2. 生成并审批 T+1 计划

```powershell
# 普通工作日自动取下一工作日；节假日前建议显式传计划交易日
uv run python scripts/daily_signal.py --trade-date 2026-08-31

uv run python scripts/manual_trade.py plans
uv run python scripts/manual_trade.py plan plan_20260828_xxxxxxxx
uv run python scripts/manual_trade.py approve plan_20260828_xxxxxxxx
```

每日信号报告会显示 `plan_id`、计划交易日和 `DRAFT` 状态。重复运行只会替换未审批草稿；已审批或执行中的同日计划不会被覆盖。

### 3. 回填真实成交

单笔录入：

```powershell
uv run python scripts/manual_trade.py fill `
  --trade-date 2026-08-31 --trade-time 09:35:00 `
  --symbol 600519 --side BUY --quantity 100 --price 1500 `
  --planned-order-id 1 --broker-fill-id broker-001
```

未提供费用时会按配置估算，收盘对账时以券商快照为准。也可生成并导入通用 CSV：

```powershell
uv run python scripts/manual_trade.py fills-template
uv run python scripts/manual_trade.py fills-import state/fills_template.csv
```

真实成交是账户变化的依据；未成交计划不会自动改变持仓。支持部分成交、计划外交易和重复成交编号拦截。

### 4. 查看 T+1 账户状态

```powershell
uv run python scripts/manual_trade.py show --as-of 2026-08-31
uv run python scripts/manual_trade.py show --as-of 2026-09-01
```

当日买入计入总持仓，但在同一交易日的可卖数量为 0；下一交易日转为可卖。当前无权威交易日历时只自动跳过周末，节假日成交请在成交 CSV 或命令中显式提供 `settle_date`。

### 5. 收盘对账

账户快照 JSON 示例：

```json
{
  "as_of": "2026-08-31",
  "cash_total": 50000,
  "cash_available_to_trade": 50000,
  "cash_withdrawable": 30000,
  "cash_frozen": 0,
  "positions": {
    "600519": {
      "total_quantity": 200,
      "sellable_quantity": 100,
      "cost_price": 1488.5
    }
  }
}
```

先预览差异，再明确确认：

```powershell
uv run python scripts/manual_trade.py reconcile state/broker_snapshot.json
uv run python scripts/manual_trade.py reconcile state/broker_snapshot.json `
  --confirm --resolution "以券商收盘快照为准"
```

详细设计和后续前端、券商 API 计划见 `MANUAL_TRADING_T1_SYNC_PLAN.md`。信号仅供参考，不构成投资建议；回测表现 ≠ 实盘表现。

## 配置 (config/settings.yaml)

| 节 | 关键项 |
|---|---|
| `data` | 前复权采集 · 板块/ST/次新股(上市<120天)过滤 · hfq_pins 防复权再污染 · **退市股回填(195只, baostock, 幸存者偏差实测 -2.0~-2.6pp/yr)** |
| `backtest` | 初始资金、佣金万2.5最低5元、印花税万5(卖出)、过户费、滑点千1(双边不利方向) |
| `strategy` | top_k=10 · lookback=60日动量 · 每5日调仓 · MA20择时(指数跌破空仓) · 单票上限15% |
| `risk` | 单票仓位上限25%、单日亏损阈值 |
| `manual_trading` | 手动交易账本开关、账户名、SQLite 路径、旧持仓自动迁移 |
| `notify` | 钉钉 webhook + 加签 secret（可用环境变量 QUART_DINGTALK_WEBHOOK/SECRET 覆盖） |

## 设计要点

- **无未来函数**：T 日收盘决策，T+1 开盘撮合；引擎与策略接口强制隔离历史窗口
- **A股规则完整**：T+1、整手买卖、双边费用差异化、停牌(NaN)跳过
- **多源容灾**：东方财富接口失败自动切换腾讯源，全局 socket 超时防挂死
- **同一套代码**：研究回测与每日实盘信号共用 Strategy 实现，杜绝两套逻辑漂移

## 前端界面 (Streamlit)

平台提供基于 Streamlit 的 Web 界面，包含以下模块：

### 界面架构

```
streamlit_app.py          主入口 - 策略概览 + 导航
pages/
├── 1_data_overview.py    数据总览 - 股票池/数据状态
├── 2_factor_research.py  因子研究 - IC/ICIR分析
├── 3_backtest.py         回测中心 - 净值/交易/参数扫描
├── 4_daily_signal.py     每日信号 - 持仓建议/ML分数
├── 5_strategy_monitor.py 策略监控 - 任务执行/持仓分析
├── 6_attribution.py      归因分析 - Brinson/因子暴露
├── 7_risk_management.py  风险管理 - VaR/流动性/集中度
├── 8_factor_ecology.py   因子生态 - IC衰减/拥挤度
├── 9_backtest_diagnostics.py 回测诊断 - WFA/Monte Carlo
└── 10_parameter_glossary.py  参数词典 - 量化参数说明
ui_components.py          UI组件库 - 统一样式/可复用组件
.streamlit/config.toml    Streamlit主题配置
```

### 前端优化特性

1. **统一样式系统**
   - 渐变色指标卡片（蓝/绿/橙/红/紫/青）
   - 响应式导航卡片
   - 状态徽章和信息盒子

2. **交互增强**
   - 数据导出按钮（CSV）
   - 图表悬停交互
   - 可折叠区域

3. **数据可视化**
   - Plotly图表统一配置
   - 表格条件格式化
   - 热力图/柱状图/折线图

### 启动界面

```powershell
# 方式1: 启动 Gradio 界面 (推荐，交互性更强)
uv run python run_gradio.py

# 方式2: 直接启动 Gradio
uv run python app.py

# 方式3: 启动 Streamlit 界面 (旧版)
uv run streamlit run streamlit_app.py
```

### Gradio 新增功能

- ✅ 实时任务监控（后台执行+实时输出）
- ✅ 参数滑块即时回测
- ✅ 交互式图表（缩放/悬停/导出）
- ✅ 响应式布局（移动端适配）
- ✅ 更好的状态管理

## Roadmap

- [x] Qlib 集成：Alpha158 因子 + LightGBM 滚动训练（见下）
- [x] 前端界面优化：统一样式/组件库/响应式布局
- [x] **walk-forward 滚动参数验证**（`scripts/walk_forward.py`，含过拟合诊断）
- [x] **制品仓库 ArtifactStore**（run_id + 参数/数据/代码版本指纹，结果可复现可追溯）
- [x] **存储按年分区**（增量只重写当年分区，查询按年份裁剪）
- [ ] WFA 结论回填：把 README 的历史数字全部重跑为样本外口径
- [ ] 前端按 run_id 展示制品（当前仍读 `reports/`，双写过渡中）
- [ ] MiniQMT(xtquant) 自动执行通道（需券商权限）
- [ ] ClickHouse 云端化迁移

## Walk-Forward 样本外验证

此前所有结论都是**全样本同期优化**：用 2020-2026 选参数，再用同一段报收益。
WFA 把时间切成若干折，每折只在 train 段选参数，再在紧接的 test 段记录样本外净值，
最后把 test 段按复利链接成一条完整的 OOS 曲线。

```powershell
# 固定参数的样本外滚动（检验稳健性，不调参）
uv run python scripts/walk_forward.py --strategy lowvol_indz

# 每折在 train 段搜参数，再在 test 段验证
uv run python scripts/walk_forward.py --strategy lowvol_indz `
    --grid top_k=10,20,30 --grid rebalance_days=20,45

# 锚定窗口 + 更长隔离带
uv run python scripts/walk_forward.py --strategy lowvol_indz `
    --anchored --embargo 10 --train 756 --test 126
```

输出含过拟合诊断：

| 指标 | 含义 | 判读 |
|---|---|---|
| `衰减比 OOS/IS` | 样本外指标 / 样本内指标 | ≥0.8 稳健；0.4~0.8 存在过拟合；<0.4 基本在挑噪声 |
| `参数一致率` | 各折选中同一参数的比例 | 1.0 = 每折选中同一组，真稳健 |
| `n_folds_with_trades` | 样本外有成交的折数 | 为 0 说明窗口太短/过滤太严，此时衰减比无意义 |

**防泄漏机制**：每个 fold 用 `MarketData.slice_by_pos()` 重切子面板，策略 `prepare()`
会在子面板上重算滚动窗口，因此 train 段不可能用到 test 段数据；train 与 test 之间
默认留 5 日 embargo，避免日频因子的持仓跨越边界。

## 制品仓库 (ArtifactStore)

scripts → api → frontend 此前靠 `glob + mtime` 猜产出，无法回答"这个数字是哪次运行产生的"。
现在每次运行都落一个显式契约：

```
artifacts/backtest_lowvol_indz_20260830_205701_189008_0db476/
├── manifest.json      run_id / 参数 / 数据版本 / 代码版本 / 指纹 / 指标 / 产出清单
├── equity.parquet
├── trades.parquet
└── summary.json
```

`fingerprint = hash(参数 + 数据版本 + 代码版本)`：**同指纹 = 同输入**，
数据或配置一变，指纹就变，旧结论自动失效，不必靠人工记忆哪轮作废。

```python
from api import artifacts_api
artifacts_api.latest_run("backtest_lowvol_indz")   # 参数 + 指标 + 产出清单
artifacts_api.latest_wfa()                          # 过拟合诊断
artifacts_api.failed_runs()                         # 失败的运行（此前只在日志里）
```

过渡期 `reports/` 与 `artifacts/` **双写**，前端逐步迁移到按 run_id 查询。
`artifacts/` 已加入 .gitignore（与 reports/ 同为生成产物）。

## 存储分区

`data/daily/{symbol}.parquet` → `data/daily/year=YYYY/{symbol}.parquet`。

| | 旧布局 | 分区布局 |
|---|---|---|
| 增量写入 | 重写该股**全史** | 只重写当年分区 |
| 全市场扫描 | 5000+ 路径拼进 SQL | 按年份 glob，DuckDB `hive_partitioning` 裁剪 |
| 读取兼容 | — | 新旧布局自动识别，可共存 |

```powershell
uv run python scripts/migrate_partition_store.py --dry-run   # 预演
uv run python scripts/migrate_partition_store.py              # 执行（幂等，旧文件迁完删除）
```

实测：61 只标的迁移后，回测结果与迁移前**逐位一致**（289 笔 / CAGR 6.08% / Sharpe 0.80 / MDD -3.41%）。

## ML 研究层 (Qlib + LightGBM)

研究/运行时分离架构：pyqlib 仅用于离线训练，生产流水线只读分数文件。

```powershell
# 安装研究依赖组
uv sync --group research

# 导出本地仓库 -> Qlib 二进制格式
uv run python scripts/export_to_qlib.py

# 逐月滚动 walk-forward 训练 (Alpha158 特征, LGBM, 前瞻月度预测)
uv run python scripts/train_ml.py --start 20190101

# 用模型分数回测
uv run python scripts/sweep.py --strategy ml_rank --start 2024-01-01

# 每日信号切换为 ML 选股
uv run python scripts/daily_signal.py --strategy ml_rank
```

## 因子研究管线

`scripts/factor_research.py` 在全市场（5214只，2019起）做横截面 Rank IC 研究，支持月频/周频采样、分时段稳定性、基准调整后多空价差。

**核心研究结论（2026-08 完成，勿外推）：**

1. A股全市场 5日横截面上，**价格动量整族呈显著负 IC**（mom60 -0.055）；沪深300池内的动量收益是股票池风格红利，不可迁移
2. **低波动/低振幅/低彩票性因子族 IC≈+0.065 且七年不衰减**——但超额主要在空头端；纯多头 top-k 在诚实成本下经换手缓冲带优化后约打平（lowvol_indz top20 buffer=0.5 年化 -0.1%，详见 `reports/turnover_buffer_2026-08-28.md`），不可直接变现
3. IC 正确≠策略赚钱：先验证"多头端能否吃到价差"，再谈上线

数据质量保障：前复权以单次全史请求保证一致性；增量更新带重叠区漂移自愈（>0.2% 自动全史重拉）。

## 实测基准 (2026-08-28 引擎修复 + 统一股票池后的终版口径，此前所有数字作废)

本次口径：卖出不利方向滑点(1-slip) + 板块/ST/上市<120天过滤(5214→3215只) + 买入预算制。
基准沪深300：总收益 +11.5%，CAGR +1.7%（2020-01 ~ 2026-08）。

| 策略 | 最优参数 | 总收益 | 年化 | 夏普 | 最大回撤 | 超额年化 |
|---|---|---|---|---|---|---|
| momentum_rotation | top20 | -58.5% | -12.8% | -0.36 | -71.8% | -14.5% |
| momentum_rotation 无择时 | top10 | -96.2% | -40.0% | -0.94 | -97.9% | -41.7% |
| lowvol_composite | top20 | -45.0% | -8.9% | -0.91 | -50.5% | -10.6% |
| lowvol_indz（行业z） | top20 | -27.7% | -4.9% | -0.39 | -53.7% | -6.7% |
| **lowvol_indz + 换手缓冲带（5d）** | **top20, buffer=0.5** | **-0.9%** | **-0.1%** | **+0.05** | **-34.5%** | **-1.8%** |
| **lowvol_indz 低频（45d）** | **top30, buffer=0.5** | **+60.6%** | **+7.4%** | **+0.67** | **-22.6%** | **+5.7%** |

完整 18 组合参数扫描见 `reports/param_sweep_repaired_engine_2026-08-28.md`；缓冲带/退市股隔离/旧值结案见 `reports/turnover_buffer_2026-08-28.md`；周期曲线/换手地板/随机定标见 `reports/rebalance_period_2026-08-28.md`；近1年/近半年窗口见 `reports/recent_windows_2026-08-28.csv`。

注（08-28 晚口径修正）：`_group_z` 改样本口径（ddof=1，与全市场 z 一致）后重测，top30@45d 全周期 CAGR +7.4%→+7.8%、近1年 +18.7%→+20.4%；top20@45d +7.1%→+6.5%。量级在参数敏感度内，结论不变。

注：上表前四行跑在早期朴素 MA 择时口径（无迟滞带）；lowvol_indz 旧值 -4.9% 已由 `scripts/diag_regime_band.py` 结案——差异 100% 来自择时迟滞带（+4.1pp/yr），旧 sweep 作废，以缓冲带行为准。

### 回测正确性验证（2026-08-28 终审通过）

针对"-53% 回测暗示引擎缺陷"的外部质疑，完成随机信号基线 + 孪生对账三层验证：

- 20 种子随机 Top10（与实盘同引擎路径）：年化 -22.1% ± 3.8%，日胜率 48%（持仓日口径正常）
- 恒等式闭合：零成本孪生 +13.1% − 几何成本拖累 33.3% − 执行摩擦 1.8% = −20.0% ≈ 引擎实测 −20.1%（终残差 +0.2pp）
- 执行摩擦仅含涨跌停拒单/停牌冻结/整手取整，方向正确；零费用引擎与孪生参照收敛
- **5 日高换手策略的诚实成本 ~33%/yr（几何）**：成本按当期净值复利，简单求和口径会严重低估
- "日胜率 23.6%"为误读：49.7% 是择时空仓日，持仓日胜率 46.5% 完全正常
- 等权宇宙"每日再平衡"基准含 ~10pp/yr 波动收割伪影，勿与低频策略直接比超额

细节见 `reports/backtest_verification_2026-08-28.md`；执行路径回归测试 `tests/test_random_baseline.py`（零费引擎 vs 孪生参照偏差 <0.3% + 成本单调性）。

**重验后的最终结论（三级置信）：**
1. 【铁】全市场个股动量为负向信号（IC 层面），诚实成本下无可挽救结构
2. 【铁】修复前所有"可行"策略数字均含三类幻觉：假择时/免费成交/零冲击成本
3. 【铁】卖出滑点曾按有利方向成交（已修复为 1-slip）：修复前所有正收益数字均含免费成交幻觉
4. 【铁】引擎通过随机基线终审（残差 +0.2pp）：策略亏损源于选股 alpha 为负 + 高换手几何成本，而非引擎
5. 【铁】幸存者偏差实测 -2.0pp/yr（等权宇宙）/ -2.56pp/yr（引擎级随机）：195 只 2020 后退市股已回填入池，此前所有策略数字偏乐观约 2pp，全部结论不变且更强
6. 【已解除】反转/空头研究的最大障碍（幸存者偏差）已消除：退市股回填后即可开展；残余缺口为 2019 前退市股与单一行情源（baostock）未交叉校验
7. 【修正 2026-08-28 晚】"long-only 不可变现"系 5d 换手口径的局部结论：调仓周期是成本的一阶杠杆（**每 1x 单边换手成本 0.72%/yr 恒定**），20-45d 低频 + buffer + 择时后 lowvol_indz 成本存活（CAGR +4~7%、Sharpe 0.4~0.67、MDD -20~-30%）。但随机基线定标显示：45d 下随机 Top20 也能 +4.9%±4.8%，策略的真实优势在成本结构/回撤控制/风险调整后 alpha（Sharpe +0.4），而非 CAGR 显著性 → 定位为股票端防御 sleeve，与 ETF 择时互补，不推翻结论 7 本身
8. 【新】工程事故记录：strategy.overrides 曾静默劫持 sweep 显式参数（yaml 优先级 bug），已修为显式 params > overrides > 全局（tests/test_param_precedence.py）；参数解析层的合并方向是隐性正确性风险，sweep 前应跑同参不同值 sanity 对



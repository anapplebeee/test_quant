# Quart — A 股研究、信号与手动 T+1 交易平台

Quart 是面向单机研究和人工交易确认的 A 股中低频平台：**数据治理 → 策略研究 → 成本后回测/WFA → T 日收盘信号 → T+1 计划审批 → 券商端手工下单 → 成交回填 → 收盘对账**。

当前不会自动连接券商或自动报单。默认正式信号仅允许 `lowvol_indz` 低频防御候选；动量、双均线和 ML 仍属于研究策略。任何历史结果均不代表未来收益，也无法保证“高额稳定收益”。

## 架构

```
data/            AKShare(东财→腾讯自动降级) 日线采集, 前复权
quart/
├── data/        BarStore(Parquet+DuckDB) / 多源采集 / 中证指数成分股票池
├── backtest/    T+1 · 100股整手 · 佣金/印花税/过户费/滑点 撮合引擎 + 绩效指标
├── strategy/    统一 Strategy 接口: momentum_rotation / dual_ma / ml_rank / lowvol_composite / lowvol_indz (可注册扩展)
├── risk/        单票权重上限校验、持仓集中度告警
├── manual_trading/ SQLite 账户账本 / T+1 持仓批次 / 计划审批 / 成交回填 / 对账
├── broker/      BrokerAdapter 契约 / PaperBroker 模拟订单状态机
├── notify/      钉钉机器人推送(支持加签)
├── pipeline.py  每日流水线: 数据更新→选股→风控→交易计划→报告/推送
api/             前端应用服务 / 输入校验 / 任务参数白名单
frontend/        Gradio 操作中心 / 手动交易 / 回测研究 / 风险与监控页面
scripts/         自动化与应急 CLI；日常操作优先使用前端
run_scheduler.py APScheduler 每交易日 17:30 自动执行
tests/           pytest: 撮合精度/T+1/无未来函数/指标数学 验证
```

## 快速开始

```powershell
# 1. 安装环境（Python 3.12，uv 管理）
uv sync

# 2. 启动前端（默认仅监听本机）
uv run python run_gradio.py
```

浏览器打开 `http://127.0.0.1:7860`。首次启动后按下面顺序操作：

1. **🧰 操作中心 → 更新交易日历**；
2. **🧰 操作中心 → 数据刷新**，股票池选 `index`、指数填 `000300`；
3. **💼 手动交易 → 账户状态与初始化**，录入券商收盘现金和持仓；
4. **📈 回测中心**，运行默认 `lowvol_indz` 回测和 Walk-Forward；
5. **🧰 操作中心 → 生成每日 T+1 信号**；
6. **💼 手动交易**，完成计划审批、成交回填、收盘对账和执行复盘。

### 局域网访问

默认仅本机访问。如需局域网访问，必须同时设置监听地址和认证：

```powershell
$env:QUART_SERVER_NAME = "0.0.0.0"
$env:QUART_AUTH = "user:请替换为强密码"
uv run python app.py
```

不要把未启用认证的页面暴露到公网；前端可以触发数据下载、训练、回测和写盘迁移。

## 前端日常使用

| 页面 | 主要用途 | 日常操作 |
|---|---|---|
| `🧰 操作中心` | 替代常用脚本命令 | 数据刷新、交易日历、PIT 股票池、质量扫描、参数扫描、信号生成、存储迁移 |
| `💼 手动交易` | 人工 T+1 闭环 | 账户初始化、计划调减/审批/取消、CSV 导出、成交录入/导入、对账、偏差复盘 |
| `📈 回测中心` | 策略验证 | 参数回测、成本分解、完整成交、WFA、制品追溯 |
| `📡 策略监控` | 长任务与账户监控 | 队列、日志、取消任务、SQLite 账户持仓 |
| `🛡️ 风险管理` | 组合风险 | 集中度、VaR/CVaR、流动性；与手动交易页使用同一 SQLite 账户 |
| `📋 每日信号` | 查看历史报告 | 查看 `reports/signal_YYYYMMDD.md`；生成操作在操作中心 |

所有长任务都进入后端任务队列。前端只能传递白名单参数，不能执行任意 shell；写盘迁移默认仅预演并要求二次确认。

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

以下操作均已在 `💼 手动交易` 页面提供。命令行保留给自动化、故障排查和前端不可用时的应急操作，不再是日常使用的前置条件。

账户状态保存在 `state/trading.db`。`state/holdings.json` 仅作为首次迁移兼容格式：

```json
{
  "cash": 50000,
  "positions": { "600519": 200, "601318": 800 }
}
```

### 1. 初始化账户

前端：`💼 手动交易 → 账户状态与初始化 → 首次初始化 / 以券商快照覆盖`。

CLI 备用方式：

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

前端：`🧰 操作中心 → 生成每日 T+1 信号`，随后进入 `💼 手动交易 → T+1 交易计划审批`。审批前可以调减数量，不允许增加策略计划外风险；信号日收盘账户未对账时审批会被阻断。

```powershell
# 普通工作日自动取下一工作日；节假日前建议显式传计划交易日
uv run python scripts/daily_signal.py --trade-date 2026-08-31

uv run python scripts/manual_trade.py plans
uv run python scripts/manual_trade.py plan plan_20260828_xxxxxxxx
uv run python scripts/manual_trade.py approve plan_20260828_xxxxxxxx
```

每日信号报告会显示 `plan_id`、计划交易日和 `DRAFT` 状态。重复运行只会替换未审批草稿；已审批或执行中的同日计划不会被覆盖。过期且未成交的 DRAFT/APPROVED 计划会自动标记为 `EXPIRED`，前端可导出人工下单 CSV。

### 3. 回填真实成交

前端：`💼 手动交易 → 真实成交回填`。计划订单 ID 可留空，系统会按交易日、代码、方向和剩余数量自动匹配唯一订单。

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

券商客户端导出的成交 CSV（中文列名、`20260831`/`2026/8/31` 日期、`买入`/`证券卖出` 方向、`600000.SH` 代码后缀）可直接导入，列名经 `quart/manual_trading/broker_profiles.py` 归一化；无法识别的列会显式报错而不是静默丢弃。

真实成交是账户变化的依据；未成交计划不会自动改变持仓。支持部分成交、计划外交易和重复成交编号拦截。

### 4. 查看 T+1 账户状态

前端：`💼 手动交易 → 账户状态与初始化`。策略监控和风险管理页读取同一 SQLite 账本，不再读取旧 `holdings.json`。

```powershell
uv run python scripts/manual_trade.py show --as-of 2026-08-31
uv run python scripts/manual_trade.py show --as-of 2026-09-01
```

当日买入计入总持仓，但同一交易日可卖数量为 0；下一交易日转为可卖。先在 `🧰 操作中心` 更新本地交易日历缓存；如果缓存尚未覆盖未来日期，系统退化为工作日规则，节假日前必须显式填写计划交易日或 `settle_date`。

### 5. 收盘对账

前端：`💼 手动交易 → 收盘账户对账`，先预览差异，再勾选确认覆盖。

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

完成对账后可在 `计划与成交偏差复盘` 查看完成率、方向调整后的不利滑点、费用偏差和延期数量。详细设计见 `MANUAL_TRADING_T1_SYNC_PLAN.md`；前后端与策略升级计划见 `FRONTEND_STRATEGY_DEVELOPMENT_PLAN.md`。

## 配置 (config/settings.yaml)

| 节 | 关键项 |
|---|---|
| `data` | 前复权采集 · 板块/ST/次新股(上市<120天)过滤 · hfq_pins 防复权再污染 · **退市股回填(195只, baostock, 幸存者偏差实测 -2.0~-2.6pp/yr)** |
| `backtest` | 初始资金、佣金万2.5最低5元、印花税万5(卖出)、过户费、滑点千1(双边不利方向) |
| `strategy` | 默认 `lowvol_indz` · 正式信号白名单 · Top30 · 45日调仓 · `rank_buffer=0.5` · 行业内 z-score；其他策略使用独立 overrides |
| `risk` | 单票仓位上限25%、单日亏损阈值 |
| `manual_trading` | 手动交易账本开关、账户名、SQLite 路径、旧持仓自动迁移 |
| `notify` | 钉钉 webhook + 加签 secret（可用环境变量 QUART_DINGTALK_WEBHOOK/SECRET 覆盖） |

## 设计要点

- **无未来函数**：T 日收盘决策，T+1 开盘撮合；引擎与策略接口强制隔离历史窗口
- **A股规则完整**：T+1、整手买卖、双边费用差异化、停牌(NaN)跳过
- **多源容灾**：东方财富接口失败自动切换腾讯源，全局 socket 超时防挂死
- **同一套代码**：研究回测与每日实盘信号共用 Strategy 实现，杜绝两套逻辑漂移

## 前端架构

当前唯一维护的 Web 前端是 Gradio，入口为 `app.py`。页面不直接执行 SQL 或拼接 shell 命令：

```text
frontend/pages/*
  → api/*（校验、展示模型、任务编排）
    → quart/*（策略、风险、执行、账本领域逻辑）
      → data/、state/trading.db、artifacts/
```

主要安全与一致性约束：

- 任务参数由 `api/task_api.py` 白名单校验，未注册参数默认拒绝；
- 同一资源任务串行、计算任务限并发，并支持日志、取消和超时；
- 手动交易页、策略监控和风险管理统一读取 `state/trading.db`；
- 计划不改变账户，只有真实成交和确认后的券商快照可以改变账户；
- 正式信号只允许 `strategy.live_allowlist` 中的策略；
- 回测、WFA 和信号产物写入 ArtifactStore，保留参数、数据版本和代码指纹。

CLI 仍保留并与前端共用同一领域代码，适用于调度器、CI、批处理和前端故障时的应急操作。

## Roadmap

- [x] Qlib 集成：Alpha158 因子 + LightGBM 滚动训练（见下）
- [x] 前端界面优化：统一样式/组件库/响应式布局
- [x] **walk-forward 滚动参数验证**（`scripts/walk_forward.py`，含过拟合诊断）
- [x] **制品仓库 ArtifactStore**（run_id + 参数/数据/代码版本指纹，结果可复现可追溯）
- [x] **存储按年分区**（增量只重写当年分区，查询按年份裁剪）
- [x] **前端操作中心**（数据、日历、质量、股票池、扫描、信号和迁移）
- [x] **手动 T+1 前端闭环**（账户、审批、成交、对账、执行偏差）
- [x] **BrokerAdapter + PaperBroker 状态机**（真实券商接入前联调契约）
- [ ] WFA 结论回填：把 README 的历史数字全部重跑为样本外口径
- [x] 前端按 run_id 展示制品（回测中心 Artifact 面板；`reports/` 仍双写兼容）
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

# ML 仅作为研究策略回测；通过 WFA、漂移和模拟盘验收后，
# 再人工加入 config/settings.yaml 的 strategy.live_allowlist
uv run python scripts/walk_forward.py --strategy ml_rank
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



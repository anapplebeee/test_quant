# Quart — A股中低频量化研究 & 选股信号平台

基于成熟开源组件构建的 A 股日线级量化工具链：**AKShare 数据 → 本地 Parquet/DuckDB 仓库 → A股规则回测引擎 → 动量/双均线策略 → 每日自动选股信号推送（钉钉）→ 人工下单执行**。

## 架构

```
data/            AKShare(东财→腾讯自动降级) 日线采集, 前复权
quart/
├── data/        BarStore(Parquet+DuckDB) / 多源采集 / 中证指数成分股票池
├── backtest/    T+1 · 100股整手 · 佣金/印花税/过户费/滑点 撮合引擎 + 绩效指标
├── strategy/    统一 Strategy 接口: momentum_rotation / dual_ma (可注册扩展)
├── risk/        单票权重上限校验、持仓集中度告警
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

## 手动交易模式

策略输出目标权重 → 与 `state/holdings.json` 当前持仓做差 → 生成次日开盘委托计划：

```json
{
  "cash": 50000,
  "positions": { "600519": 200, "601318": 800 }
}
```

每次成交后请人工更新该文件。信号仅供参考，不构成投资建议；回测表现 ≠ 实盘表现。

## 配置 (config/settings.yaml)

| 节 | 关键项 |
|---|---|
| `backtest` | 初始资金、佣金万2.5最低5元、印花税万5(卖出)、过户费、滑点千1 |
| `strategy` | top_k=10 · lookback=60日动量 · 每5日调仓 · MA20择时(指数跌破空仓) · 单票上限15% |
| `risk` | 单票仓位上限25%、单日亏损阈值 |
| `notify` | 钉钉 webhook + 加签 secret |

## 设计要点

- **无未来函数**：T 日收盘决策，T+1 开盘撮合；引擎与策略接口强制隔离历史窗口
- **A股规则完整**：T+1、整手买卖、双边费用差异化、停牌(NaN)跳过
- **多源容灾**：东方财富接口失败自动切换腾讯源，全局 socket 超时防挂死
- **同一套代码**：研究回测与每日实盘信号共用 Strategy 实现，杜绝两套逻辑漂移

## Roadmap

- [x] Qlib 集成：Alpha158 因子 + LightGBM 滚动训练（见下）
- [ ] walk-forward 滚动参数验证、子区间稳定性评估
- [ ] MiniQMT(xtquant) 自动执行通道（需券商权限）
- [ ] ClickHouse 云端化迁移

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
2. **低波动/低振幅/低彩票性因子族 IC≈+0.065 且七年不衰减**——但超额主要在空头端，纯多头 top-k 结构实测年化仅 ~3%（详见 sweep 报告），不可直接变现
3. IC 正确≠策略赚钱：先验证"多头端能否吃到价差"，再谈上线

数据质量保障：前复权以单次全史请求保证一致性；增量更新带重叠区漂移自愈（>0.2% 自动全史重拉）。

## 实测基准 (2024-01 ~ 2026-08)

| 策略 | 池 | 年化 | 夏普 | 最大回撤 |
|---|---|---|---|---|
| 动量轮动+MA20择时 | 沪深300 | 72.5%* | 1.39 | ≈ -43% |
| 动量轮动(无过滤) | 全市场 | 5.4% | 0.37 | -52% |
| lowvol_composite top20+bounce | 全市场 | 3.1% (2020起) | 0.27 | -21% |
| ml_rank (IC≈0.01) | 沪深300 | 17.0% | 0.67 | -31% |

\* 样本内牛市结果；全市场验证表明对池子高度敏感。


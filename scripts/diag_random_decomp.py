"""随机基线缺口分解诊断。

问题：等权宇宙(每日再平衡、零成本) CAGR +14.5%，但随机 Top10(5日调仓) 仅约 -22%，
缺口 ~36pp 远超成本解释范围。本脚本把缺口拆到四个桶：
  A. 流动性合格池偏移：随机抽取池(20日均额>=5000万 & 价格>=2) vs 全宇宙
  B. 再平衡频率伪影(波动收割)：每日再平衡 vs 5日 vs 买入持有
  C. 交易成本：从引擎 trades 明细直接汇总(显性费用 + 滑点近似)
  D. 选股集中度噪声：10 只 vs 3215 只的种子间方差

用法：.venv/Scripts/python.exe scripts/diag_random_decomp.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine, MarketData
from quart.config import load_config
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.research.baseline import RandomTopKStrategy
from quart.strategy.filters import apply_liquidity

console = Console()


def yearly(curve: pd.Series) -> pd.Series:
    g = curve.groupby(curve.index.year)
    return g.last() / g.first() - 1.0


def k_day_rebal(rets: pd.DataFrame, k: int) -> pd.Series:
    """每 k 日再平衡的等权组合，返回以每段末日为索引的段收益率序列。"""
    n = len(rets)
    seg_sum = np.log1p(rets).groupby(np.arange(n) // k).sum()  # NaN 跳过=停牌不计息
    seg_ret = np.expm1(seg_sum.mean(axis=1))  # 段内复利 -> 跨股票等权
    end_labels = [rets.index[min((g + 1) * k, n) - 1] for g in seg_ret.index]
    seg_ret.index = pd.DatetimeIndex(end_labels)
    return seg_ret


def main() -> None:
    cfg = load_config()
    store = BarStore()
    bars = store.load(start="2020-01-01")
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[bench["date"] >= "2020-01-01"]
    data_cfg = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )
    md = MarketData.from_bars(bars, benchmark=bench)
    closes, rets = md.closes, md.closes.pct_change(fill_method=None).iloc[1:]
    idx = rets.index
    years = len(idx) / 252.0

    bench_close = bench.set_index("date")["close"].reindex(idx).ffill()

    # --- A. 流动性合格池 vs 全宇宙 ---
    avg_amt = md.amounts.rolling(20).mean().iloc[1:]
    elig = (avg_amt >= 50e6) & (closes.iloc[1:] >= 2.0)
    ew_all = rets.mean(axis=1)
    ew_elig = rets.where(elig).mean(axis=1)

    # --- B. 再平衡频率伪影 ---
    ew_all_5d = k_day_rebal(rets, 5)
    bh_all = (1 + rets.fillna(0.0)).cumprod().mean(axis=1).pct_change()

    # --- 随机组合（3 个种子 + seed0 trades 成本明细）---
    params = dict(
        top_k=10,
        rebalance_days=5,
        max_weight_pct=0.15,
        min_avg_amount=cfg["strategy"].get("min_avg_amount"),
        liquidity_days=cfg["strategy"].get("liquidity_days", 20),
        min_price=cfg["strategy"].get("min_price"),
    )
    curves, trades0 = {}, None
    for seed in range(3):
        eng = BacktestEngine(md, RandomTopKStrategy(**{**params, "seed": seed}))
        eq = eng.run()
        curves[seed] = eq
        if seed == 0:
            trades0 = pd.DataFrame([t.__dict__ for t in eng.trades])

    # --- C. 成本明细（seed0，初始资金按 cfg）---
    init_cash = float(cfg["backtest"]["initial_cash"])
    buys = trades0[trades0["side"] == "BUY"]
    sells = trades0[trades0["side"] == "SELL"]
    fee_sum = float(trades0["fee"].sum())
    slip_sum = 0.001 * (float(buys["amount"].sum()) + float(sells["amount"].sum()))
    turnover_1side = (float(buys["amount"].sum()) + float(sells["amount"].sum())) / 2.0 / init_cash / years
    console.print(f"年限: {years:.2f} | 单边年换手: {turnover_1side:.1f}x")
    console.print(
        f"显性费用拖累: {-fee_sum / init_cash / years:+.2%}/yr | "
        f"滑点拖累(10bp近似,含冲击另计): {-slip_sum / init_cash / years:+.2%}/yr"
    )
    console.print(f"seed0 trades: {len(trades0)} 笔 | BUY {len(buys)} | SELL {len(sells)}")

    # --- 年度表 ---
    t = Table(title="年度收益分解")
    for c in ["年份", "000300", "EW全宇宙(日)", "EW合格池(日)", "EW全宇宙(5日)", "EW买入持有", "随机x3均值"]:
        t.add_column(c, justify="right")
    r_bench = yearly(bench_close)
    r_all = yearly((1 + ew_all).cumprod())
    r_elig = yearly((1 + ew_elig.fillna(0)).cumprod())
    r_5d = yearly((1 + ew_all_5d).cumprod())
    r_bh = yearly((1 + bh_all.fillna(0)).cumprod())
    rnd_y = pd.DataFrame({s: yearly(eq) for s, eq in curves.items()}).mean(axis=1)
    for y in sorted(r_bench.index):
        t.add_row(
            str(y),
            f"{r_bench[y]:+.1%}",
            f"{r_all.get(y, np.nan):+.1%}",
            f"{r_elig.get(y, np.nan):+.1%}",
            f"{r_5d.get(y, np.nan):+.1%}",
            f"{r_bh.get(y, np.nan):+.1%}",
            f"{rnd_y.get(y, np.nan):+.1%}",
        )
    cagr = lambda v: float(v) ** (1.0 / years) - 1.0
    t.add_row(
        "全期CAGR",
        f"{cagr(bench_close.iloc[-1] / bench_close.iloc[0]):+.1%}",
        f"{cagr(float((1 + ew_all).prod())):+.1%}",
        f"{cagr(float((1 + ew_elig.fillna(0)).prod())):+.1%}",
        f"{cagr(float((1 + ew_all_5d).prod())):+.1%}",
        f"{cagr(float((1 + bh_all.fillna(0)).prod())):+.1%}",
        f"{pd.Series({s: float(eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 for s, eq in curves.items()}).mean():+.1%}",
    )
    console.print(t)
    console.print(f"合格池平均宽度: {elig.sum(axis=1).mean():.0f} / {rets.shape[1]} 只")


if __name__ == "__main__":
    main()

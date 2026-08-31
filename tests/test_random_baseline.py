"""回测正确性回归测试：随机信号基线（对拍验证）。

核心思想：在无摩擦合成市场（无涨跌停、无停牌、开盘=昨收、零费用）上，
随机选股策略的引擎净值必须与向量孪生参照（收盘进出、等权5日持有）收敛。
该测试守护引擎的整条执行路径：T+1 开盘撮合、先卖后买、现金预算、整手取整、
权重->股数换算。若有人引入执行方向/时序错误（如卖出有利滑点、假择时），
本测试会立刻失败。

历史背景：2026-08-28 曾据此外排流程定位过"卖出 1+slip 有利成交"与"分解口径
假象"两类问题，终审残差 +0.2pp（真实市场、含费用）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quart.backtest.engine import FLAT, BacktestEngine, Fees, MarketData
from quart.research.baseline import RandomTopKStrategy


def _synthetic_market(n_sym: int = 40, n_days: int = 320, seed: int = 7) -> pd.DataFrame:
    """无摩擦合成市场：日波动 2%（远低于 10% 涨跌停）、开盘=昨收、无停牌。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    frames = []
    for s in range(n_sym):
        sym = f"{600000 + s}"
        rets = rng.normal(0, 0.02, n_days)
        close = 50.0 * np.cumprod(1 + rets)
        open_ = np.empty(n_days)
        open_[0] = close[0]
        open_[1:] = close[:-1]  # 开盘=昨收：引擎成交价与孪生参照逐分一致
        frames.append(
            pd.DataFrame(
                {
                    "symbol": sym,
                    "date": dates,
                    "open": open_,
                    "high": np.maximum(open_, close) * 1.005,
                    "low": np.minimum(open_, close) * 0.995,
                    "close": close,
                    "volume": 1e6,
                    "amount": 1e6 * close,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def test_random_baseline_engine_matches_twin():
    bars = _synthetic_market()
    md = MarketData.from_bars(bars)
    closes = md.closes.ffill()
    n_days = len(md.dates)
    params = dict(top_k=10, rebalance_days=5, max_weight_pct=0.15,
                  min_avg_amount=None, liquidity_days=20, min_price=None, seed=42)

    # 重放选股序列（与引擎内 rng 消耗完全一致）
    strat = RandomTopKStrategy(**params)
    strat.prepare(md)
    picks = {}
    for i in range(n_days):
        w = strat.target_weights(i)
        if w and FLAT not in w:
            picks[i] = list(w.keys())
    assert len(picks) > 30  # 确保确实发生了持续调仓

    # 孪生参照：信号日收盘 -> 5 日后收盘，段间复利
    pos, vals = 1.0, [1.0] * n_days
    segs = iter(
        (i, min(i + 5, n_days - 1), float((closes[syms].iloc[min(i + 5, n_days - 1)] / closes[syms].iloc[i] - 1).mean()))
        for i, syms in picks.items()
    )
    cur = next(segs, None)
    for d in range(n_days):
        if cur is not None and d >= cur[1]:
            pos *= 1 + cur[2]
            cur = next(segs, None)
        vals[d] = pos

    # 零费用引擎（同一策略类需重新实例化以重放 rng）
    eng = BacktestEngine(md, RandomTopKStrategy(**params),
                         fees=Fees(0, 0.0, 0, 0, 0, 0.0), initial_cash=1_000_000.0)
    eq = eng.run() / 1_000_000.0

    # 无摩擦市场中两者必须几乎逐分一致（允许整手取整误差）
    rel_gap = abs(float(eq.iloc[-1] / vals[-1]) - 1.0)
    assert rel_gap < 0.003, f"引擎净值偏离孪生参照 {rel_gap:.4%}，执行路径存在缺陷"

    # 全费用（10bp 滑点 + 默认费率）必须严格不优于零费用（成本单调性）
    eng_cost = BacktestEngine(md, RandomTopKStrategy(**params), initial_cash=1_000_000.0)
    eq_cost = eng_cost.run() / 1_000_000.0
    assert float(eq_cost.iloc[-1]) < float(eq.iloc[-1]), "含费净值竟优于零费净值，成本方向错误"


if __name__ == "__main__":
    test_random_baseline_engine_matches_twin()
    print("random baseline OK")

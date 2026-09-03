"""ht_engine_smoke.py — 在正式 BacktestEngine 上验证 hot_rotation 策略能跑通。

用 exploratory(非 formal PIT)流程 + 默认风控(单票上限)跑 hot_rotation(momentum)，
确认 target_weights/板块热度/权重校验 在引擎内无异常，并产出净值。
"""
from __future__ import annotations

import pandas as pd

from quart.config import load_config
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.execution.fees import Fees
from quart.risk.rules import make_weight_validator
from quart.strategy import build_strategy


def main() -> None:
    cfg = load_config()
    store = BarStore()
    start, end = "2024-01-01", "2025-06-30"
    bars = store.load(start=start, end=end)
    bench = store.load_benchmark(cfg["benchmark"])
    bars = filter_for_simulation(
        bars,
        exclude_star=True, exclude_chinext=True, exclude_st=True, min_list_days=0,
    )
    if bars.empty:
        raise SystemExit("empty bars after simulation filter")
    md = MarketData.from_bars(bars, benchmark=bench)

    for selector in ["momentum", "ml_score"]:
        params = {"selector": selector}
        if selector == "ml_score":
            params["scores_path"] = "reports/ht_ml_scores.csv"
        strat = build_strategy("hot_rotation", **params)
        violations: list[str] = []
        risk = make_weight_validator(float(cfg["risk"]["max_position_pct"]), collect=violations)
        from quart.backtest.engine import BacktestEngine
        res = BacktestEngine(md, strat, fees=Fees.from_config(), risk_pipeline=risk).run_result()
        eq = res.equity
        total = float(eq.iloc[-1] / eq.iloc[0] - 1) if len(eq) > 1 and eq.iloc[0] > 0 else float("nan")
        print(f"[{selector}] strategy={strat.name} trades={len(res.trades)} "
              f"days={len(eq)} total_ret={total:+.2%} risk_viol={len(violations)}")
        # 持仓天数
        held = int((res.trades.groupby("side").size().get("BUY", 0))) if not res.trades.empty else 0
        print(f"[{selector}] n_buy_orders={held}")


if __name__ == "__main__":
    main()

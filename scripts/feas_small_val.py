"""3.2 审阅步骤 2：1.5 万元可行性核查（新基线腿单独回测，CAP=15000 vs 30000）。

统计：实际持仓只数分布、拒单/延迟单、权重偏离、CAGR/MDD 与 3 万版差距。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from audit_small_val import TTMFinPIT, build_ttm_fin  # noqa: E402
from backtest_small_val import (  # noqa: E402
    BarStore, SmallCapStrategy, V1, load_config, load_fundamental_panel,
    load_listed_days, load_md,
)
from quart.backtest.engine import BacktestEngine  # noqa: E402
from quart.execution.fees import Fees  # noqa: E402

SC_PARAMS = {**V1, "n": 10, "timing": "none", "calendar": "simple",
             "use_score": True, "w_cap": 0.45, "w_rev": 0.30, "w_to": 0.25, "w_roe": 0.0}


def run_cap(md, bt, cap: float):
    fees = Fees(bt["commission_rate"], bt["commission_min"], bt["stamp_tax_rate"],
                bt["transfer_fee_rate"], bt["slippage_rate"],
                float(bt.get("impact_coef", 0.0)))
    fund = load_fundamental_panel(md.dates)
    listed = load_listed_days(md.dates)
    idx = BarStore().load_benchmark("000852")
    idx_close = pd.Series(idx["close"].values, index=pd.to_datetime(idx["date"]))
    fin_ttm = TTMFinPIT(build_ttm_fin(pd.read_parquet(
        "data/factors/financials.parquet",
        columns=["symbol", "date", "announcement_date", "revenue", "net_profit", "roe"],
    )))
    strat = SmallCapStrategy(idx_close, fin_ttm, listed, fund, **SC_PARAMS)
    engine = BacktestEngine(md, strat, fees=fees, initial_cash=cap,
                            max_adv_participation=0.05)
    result = engine.run_result()
    eq = result.equity
    eq.index = md.dates[: len(eq)]
    return engine, result, eq


def main():
    md = load_md()
    bt = load_config()["backtest"]
    stats = {}
    for cap in (30_000.0, 15_000.0):
        engine, result, eq = run_cap(md, bt, cap)
        eqv = eq[eq.index >= "2019-01-01"]
        yrs = len(eqv) / 244.0
        cagr = (eqv.iloc[-1] / eqv.iloc[0]) ** (1 / yrs) - 1
        mdd = float((eqv / eqv.cummax() - 1).min())
        n_trades = len(engine.trades)
        deferred = getattr(result, "deferred_orders", None)
        n_def = 0 if deferred is None or deferred.empty else len(deferred)
        # 实际持仓只数：从成交重建（每月末快照）
        holdings: dict[pd.Timestamp, set] = {}
        pos: set = set()
        shares_pos: dict[str, float] = {}
        for t in engine.trades:
            d = pd.Timestamp(t.date)
            s = str(t.symbol)
            if t.side == "BUY":
                shares_pos[s] = shares_pos.get(s, 0) + float(t.shares)
                pos.add(s)
            elif t.side == "SELL":
                shares_pos[s] = shares_pos.get(s, 0) - float(t.shares)
                if shares_pos[s] <= 0:
                    pos.discard(s)
            month_end = d + pd.offsets.MonthEnd(0)
            holdings[month_end] = set(pos)
        counts = [len(v) for v in holdings.values()]
        stats[cap] = {"cagr": cagr, "mdd": mdd, "trades": n_trades,
                      "deferred": n_def, "counts": counts, "engine": engine,
                      "final": float(eqv.iloc[-1])}
        print(f"CAP={cap:,.0f}: CAGR {cagr*100:6.2f}%  MDD {mdd*100:7.2f}%  "
              f"终值 {eqv.iloc[-1]:,.0f}  成交{n_trades}笔  延迟/拒单{n_def}笔")
        if counts:
            import collections
            cc = collections.Counter(counts)
            print(f"  月末持仓只数分布: {dict(sorted(cc.items()))}")

    gap = stats[30_000.0]["cagr"] - stats[15_000.0]["cagr"]
    print(f"\nCAGR 差（3万 - 1.5万）: {gap*100:+.2f}pp")
    print("判读: 持仓稳定≥8只 且 差<2pp → 可接入(注明最低资金)；"
          "常只有5-6只 → 价格上限降12-15元(1配置验证) 或 标注最低总资金6万")


if __name__ == "__main__":
    main()

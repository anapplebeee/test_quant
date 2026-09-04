"""小市值策略诊断：成本归因 + 小市值贝塔基准（配合 backtest_small_val.py）。

D1 原版-零成本          → 分离执行摩擦
D2 全量优化-零成本      → 同上
D3 避雷+Top60等权-无择时无日历-零成本 → 小市值因子贝塔（避雷池）
D3c 同 D3 但不避雷-零成本           → 避雷贡献
D4 D3-全成本                        → 贝塔的成本后形态
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backtest_small_val import (  # noqa: E402
    FULL, ORIG, V1, V2, BarStore, FinPIT, SmallCapStrategy, CAP, load_config,
    load_fundamental_panel, load_listed_days, load_md, run_once,
)
import pandas as pd  # noqa: E402


def main():
    md = load_md()
    fund = load_fundamental_panel(md.dates)
    fin = FinPIT()
    listed = load_listed_days(md.dates)
    idx = BarStore().load_benchmark("000852")
    idx_close = pd.Series(idx["close"].values, index=pd.to_datetime(idx["date"]))
    bt = load_config()["backtest"]

    beta10 = {"n": 10, "timing": "none", "calendar": "none"}
    cases = [
        ("D1 原版-零成本", ORIG, 0.0),
        ("D2 全量-零成本", FULL, 0.0),
        ("E1 贝塔10不避雷-零成本", {**beta10}, 0.0),
        ("E2 贝塔10避雷-零成本", {**V1, **beta10}, 0.0),
        ("E3 贝塔10避雷-全成本", {**V1, **beta10}, 1.0),
        ("E4 V2缓冲区-零成本", V2, 0.0),
    ]
    for label, params, mult in cases:
        strat = SmallCapStrategy(idx_close, fin, listed, fund, **params)
        r = run_once(md, bt, strat, label, cost_mult=mult)
        print(f"{label:<26} CAGR {r['cagr']*100:7.2f}%  MDD {r['mdd']*100:7.2f}%  "
              f"Sharpe {r['sharpe']:5.2f}  换手 {r['annual_turnover']*100:5.0f}%  "
              f"终值 {r['final']:>10,.0f}")


if __name__ == "__main__":
    main()

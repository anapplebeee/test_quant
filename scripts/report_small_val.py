"""H5 胜出配置终验：成本鲁棒性 0/1/2/3x + 分年度 + 压力段 + 双基准。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backtest_small_val import (  # noqa: E402
    V1, BarStore, FinPIT, SmallCapStrategy, START, yearly_returns,
    seg_return, load_config, load_fundamental_panel, load_listed_days, load_md,
    run_once,
)
import pandas as pd  # noqa: E402

# RESEARCH-015 胜出配置：贝塔10 + 二级因子打分 + 避雷 + 仅1月/4月20后空仓
H5 = {**V1, "n": 10, "timing": "none", "calendar": "simple", "use_score": True}


def main():
    md = load_md()
    fund = load_fundamental_panel(md.dates)
    fin = FinPIT()
    listed = load_listed_days(md.dates)
    idx = BarStore().load_benchmark("000852")
    idx_close = pd.Series(idx["close"].values, index=pd.to_datetime(idx["date"]))
    bt = load_config()["backtest"]

    print("== H5 成本鲁棒性 ==")
    for mult in (0.0, 1.0, 2.0, 3.0):
        r = run_once(md, bt, SmallCapStrategy(idx_close, fin, listed, fund, **H5),
                     f"H5-{mult}x", cost_mult=mult)
        print(f"  成本{mult:.0f}x: CAGR {r['cagr']*100:6.2f}%  MDD {r['mdd']*100:6.2f}%  "
              f"Sharpe {r['sharpe']:5.2f}  换手 {r['annual_turnover']*100:4.0f}%")

    r = run_once(md, bt, SmallCapStrategy(idx_close, fin, listed, fund, **H5), "H5")
    print("\n== H5 分年度 ==")
    yearly = yearly_returns(r["equity"])
    for yr, ret in yearly.items():
        print(f"  {yr}: {ret*100:7.2f}%")
    print("\n== H5 压力段 ==")
    stress = {}
    for s, e, name in [("2024-01-02", "2024-02-08", "24Q1小微盘流动性危机"),
                       ("2024-04-12", "2024-06-30", "24Q2国九条退市恐慌"),
                       ("2026-01-01", "2026-08-31", "2026YTD")]:
        stress[name] = seg_return(r["equity"], s, e)
        print(f"  {name}: {stress[name]*100:7.2f}%")
    print(f"\n终值 {r['final']:,.0f}（本金 30,000，{START.year}-2026）")

    import json
    out = {
        "winner": {"label": "H5 贝塔10+二级因子打分+避雷+1月4月空仓", "params": H5},
        "metrics": {k: v for k, v in r.items() if k != "equity"},
        "cost_robustness": {}, "yearly": yearly, "stress": stress,
    }
    for mult in (0.0, 1.0, 2.0, 3.0):
        rr = run_once(md, bt, SmallCapStrategy(idx_close, fin, listed, fund, **H5),
                      f"H5-{mult}x", cost_mult=mult)
        out["cost_robustness"][f"{mult:.0f}x"] = {
            k: v for k, v in rr.items() if k != "equity"}
    Path("reports/small_val_final_2026-09-04.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=float)
    )
    print("-> reports/small_val_final_2026-09-04.json")


if __name__ == "__main__":
    main()

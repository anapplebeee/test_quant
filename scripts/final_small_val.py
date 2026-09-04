"""F 组：贝塔10 框架 + 做减法的择时（真·优化叠加收官）。

结论链（前序诊断）：小市值贝塔=24.7%/年(零成本,Top10等权避雷池)；
原版择时+日历+Top5集中 → 9.4%；成本拖累 8~10pp。
F 组在贝塔10 上逐项加回轻量风控，找全成本下 CAGR/MDD 最优组合。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backtest_small_val import (  # noqa: E402
    FULL, ORIG, V1, V2, BarStore, FinPIT, SmallCapStrategy, load_config,
    load_fundamental_panel, load_listed_days, load_md, run_once,
)
import pandas as pd  # noqa: E402

RISK = dict(port_dd_half=0.15, port_dd_flat=0.25, rel_stop=0.12)
F1 = {**V1, "n": 10, "timing": "none", "calendar": "none",
      "use_extreme": True, **RISK}
F2 = {**F1, "calendar": "simple"}
F3 = {**F1, "calendar": "upgraded"}
F4 = {**F2, "vol_target": True}
F5 = {**F2, "buf_mult": 3.0}
BETA10 = {**V1, "n": 10, "timing": "none", "calendar": "none"}
G1 = {**BETA10, "port_dd_half": 0.15, "port_dd_flat": 0.25}
G2 = {**BETA10, "rel_stop": 0.12}
G3 = {**BETA10, "use_extreme": True}
G4 = {**BETA10, "calendar": "simple"}
H1 = {**G4, "buf_mult": 3.0}
H2 = {**G4, "reb_day": 3}
H3 = {**G4, "buf_mult": 3.0, "reb_day": 3}
H4 = {**G4, "calendar": "upgraded"}
H5 = {**G4, "use_score": True}
H6 = {**G4, "vol_target": True}
J1 = {**H5, "calendar": "upgraded"}
J2 = {**H5, "score_top": 30}
J3 = {**H5, "score_top": 100}
J4 = {**H5, "n": 5}
J5 = {**H5, "n": 8}


def main():
    md = load_md()
    fund = load_fundamental_panel(md.dates)
    fin = FinPIT()
    listed = load_listed_days(md.dates)
    idx = BarStore().load_benchmark("000852")
    idx_close = pd.Series(idx["close"].values, index=pd.to_datetime(idx["date"]))
    bt = load_config()["backtest"]

    cases = [
        ("F1 贝塔10+极端日+回撤分级", F1),
        ("F2 F1+1月4月空仓", F2),
        ("F3 F1+升级日历", F3),
        ("F4 F2+波动率目标", F4),
        ("F5 F2+缓冲区", F5),
        ("G1 贝塔10+仅回撤分级", G1),
        ("G2 贝塔10+仅相对止损", G2),
        ("G3 贝塔10+仅极端日", G3),
        ("G4 贝塔10+仅1月4月空仓", G4),
        ("H1 G4+缓冲区", H1),
        ("H2 G4+错峰调仓", H2),
        ("H3 G4+缓冲+错峰", H3),
        ("H4 G4+升级日历", H4),
        ("H5 G4+二级因子打分", H5),
        ("H6 G4+波动率目标", H6),
        ("J1 H5+升级日历", J1),
        ("J2 H5+score_top30", J2),
        ("J3 H5+score_top100", J3),
        ("J4 H5+Top5", J4),
        ("J5 H5+Top8", J5),
    ]
    for label, params in cases:
        strat = SmallCapStrategy(idx_close, fin, listed, fund, **params)
        r = run_once(md, bt, strat, label)
        print(f"{label:<24} CAGR {r['cagr']*100:7.2f}%  MDD {r['mdd']*100:7.2f}%  "
              f"Sharpe {r['sharpe']:5.2f}  Calmar {r['calmar']:5.2f}  "
              f"换手 {r['annual_turnover']*100:5.0f}%  终值 {r['final']:>10,.0f}")


if __name__ == "__main__":
    main()

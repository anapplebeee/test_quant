"""3.2 审阅修正：MDD 起止日期核查（判定回撤是否仍在进行中/防御腿相关性结构）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd  # noqa: E402

from portfolio_small_val import (  # noqa: E402
    COST_ETF, COST_SC, combine, load_etf_nav, run_sc_leg,
)


def mdd_window(nav: pd.Series) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, float]:
    """返回 (峰值日, 谷底日, 修复日, mdd)。修复日=None 表示仍在水下。"""
    cummax = nav.cummax()
    dd = nav / cummax - 1
    trough = dd.idxmin()
    peak = nav.loc[:trough].idxmax()
    mdd = float(dd.min())
    after = dd.loc[trough:]
    rec = after[after >= -1e-9]
    recover = rec.index[0] if len(rec) else None
    return peak, trough, recover, mdd


def main():
    sc = run_sc_leg()
    idx = sc.index
    navs = {
        "SC": sc,
        "BOND": load_etf_nav("511260", idx),
        "GOLD": load_etf_nav("518880", idx),
    }
    months = pd.Series(idx, index=idx).dt.to_period("M")
    reb = idx[~months.duplicated()][1:]

    cases = {
        "B0 SC100%": navs["SC"],
        "B1 债50+金50": combine({"BOND": navs["BOND"], "GOLD": navs["GOLD"]},
                               {"BOND": 0.5, "GOLD": 0.5},
                               {"BOND": COST_ETF, "GOLD": COST_ETF}, reb),
        "P30": combine({"SC": navs["SC"], "BOND": navs["BOND"], "GOLD": navs["GOLD"]},
                       {"SC": 0.30, "BOND": 0.35, "GOLD": 0.35},
                       {"SC": COST_SC, "BOND": COST_ETF, "GOLD": COST_ETF}, reb),
        "P40": combine({"SC": navs["SC"], "BOND": navs["BOND"], "GOLD": navs["GOLD"]},
                       {"SC": 0.40, "BOND": 0.30, "GOLD": 0.30},
                       {"SC": COST_SC, "BOND": COST_ETF, "GOLD": COST_ETF}, reb),
        "P50": combine({"SC": navs["SC"], "BOND": navs["BOND"], "GOLD": navs["GOLD"]},
                       {"SC": 0.50, "BOND": 0.25, "GOLD": 0.25},
                       {"SC": COST_SC, "BOND": COST_ETF, "GOLD": COST_ETF}, reb),
        "P60": combine({"SC": navs["SC"], "BOND": navs["BOND"], "GOLD": navs["GOLD"]},
                       {"SC": 0.60, "BOND": 0.20, "GOLD": 0.20},
                       {"SC": COST_SC, "BOND": COST_ETF, "GOLD": COST_ETF}, reb),
    }
    print(f"{'配置':<12} {'MDD':>8} {'峰值日':>12} {'谷底日':>12} {'修复日':>12} 状态")
    for name, nav in cases.items():
        p, t, r, m = mdd_window(nav)
        status = "已修复" if r is not None else "⚠ 仍在水下"
        print(f"{name:<12} {m*100:>7.2f}% {p.date()!s:>12} {t.date()!s:>12} "
              f"{(r.date() if r is not None else '—')!s:>12} {status}")
    # 单腿 MDD 窗口（定位防御腿尾部来源）
    for name, nav in (("长债511260", navs["BOND"]), ("黄金518880", navs["GOLD"])):
        p, t, r, m = mdd_window(nav)
        status = "已修复" if r is not None else "⚠ 仍在水下"
        print(f"{name:<12} {m*100:>7.2f}% {p.date()!s:>12} {t.date()!s:>12} "
              f"{(r.date() if r is not None else '—')!s:>12} {status}")


if __name__ == "__main__":
    main()

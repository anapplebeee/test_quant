"""3.2 组合层放置（审阅执行规格，预算 11~14/14）。

配置：P30/P40/P50/P60 = 新基线 × {30,40,50,60}% + 长债(511260)与黄金(518880)各半；
对照：B0=新基线100%，B1=长债50%+黄金50%，B2=新基线40%+沪深300 ETF(510300)60%。

规格要点：
- 新基线 = G-noROE（贝塔10+避雷+4月+1月空仓+45/30/25/0，TTM 口径，自身全成本）；
- 空仓期资金留在新基线腿内（不转移，不污染归因）；
- 月末收盘定目标、次月首个交易日执行；成本：新基线腿 delta×0.006（保守，单边0.3%×2），
  ETF 腿 delta×0.0011（万1佣金+滑点0.1%，无印花税/impact）；
- 判据：全样本 MDD ≥ -20%、Calmar ≥ 0.8、23-26 段 CAGR ≥ 8%。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from audit_small_val import H5, TTMFinPIT, build_ttm_fin  # noqa: E402
from backtest_small_val import (  # noqa: E402
    BarStore, SmallCapStrategy, V1, load_config, load_fundamental_panel,
    load_listed_days, load_md, run_once,
)

CAP = 30_000
COST_SC, COST_ETF = 0.006, 0.0011
SEG1, SEG2 = ("2019-01-01", "2022-12-31"), ("2023-01-01", "2026-08-31")


def run_sc_leg() -> pd.Series:
    """新基线腿日净值（含自身全部成本），2019 起归一。"""
    md = load_md()
    fund = load_fundamental_panel(md.dates)
    listed = load_listed_days(md.dates)
    idx = BarStore().load_benchmark("000852")
    idx_close = pd.Series(idx["close"].values, index=pd.to_datetime(idx["date"]))
    bt = load_config()["backtest"]
    fin_ttm = TTMFinPIT(build_ttm_fin(pd.read_parquet(
        "data/factors/financials.parquet",
        columns=["symbol", "date", "announcement_date", "revenue", "net_profit", "roe"],
    )))
    sc_params = {**V1, "n": 10, "timing": "none", "calendar": "simple",
                 "use_score": True, "w_cap": 0.45, "w_rev": 0.30, "w_to": 0.25, "w_roe": 0.0}
    r = run_once(md, bt, SmallCapStrategy(idx_close, fin_ttm, listed, fund, **sc_params),
                 "SC-leg")
    eq = r["equity"]
    eq = eq[eq.index >= "2019-01-01"]
    return eq / eq.iloc[0]


def load_etf_nav(code: str, index: pd.DatetimeIndex) -> pd.Series:
    df = BarStore().load(symbols=[code])
    s = pd.Series(df["close"].values, index=pd.to_datetime(df["date"]))
    s = s.reindex(index).ffill()
    return s / s.iloc[0]


def combine(nav_dict: dict[str, pd.Series], weights: dict[str, float],
            cost_dict: dict[str, float], rebalance_dates: pd.DatetimeIndex) -> pd.Series:
    """按规格骨架：每日漂移，再平衡日回到目标并扣 delta 加权成本。"""
    px = pd.concat(nav_dict, axis=1).dropna()
    rets = px.pct_change().fillna(0)
    idx = px.index
    w = pd.Series(weights, index=px.columns)
    cost = pd.Series(cost_dict, index=px.columns)
    cur = w.copy()
    nav, navs, turn = 1.0, [], []
    reb = set(rebalance_dates)
    for d in idx:
        cur = cur * (1 + rets.loc[d])
        tot = cur.sum()
        nav *= tot
        cur /= tot
        if d in reb:
            delta = (w - cur).abs()
            c = float((delta * cost).sum())
            nav *= 1 - c
            turn.append(float(delta.sum()))
            cur = w.copy()
        navs.append(nav)
    s = pd.Series(navs, index=idx)
    yrs = (idx[-1] - idx[0]).days / 365.25
    s.attrs["annual_turnover"] = sum(turn) / yrs
    return s


def seg_stats(nav: pd.Series, start=None, end=None) -> dict:
    n = nav.loc[start:end] if start or end else nav
    n = n / n.iloc[0]
    yrs = (n.index[-1] - n.index[0]).days / 365.25
    cagr = n.iloc[-1] ** (1 / yrs) - 1
    mdd = float((n / n.cummax() - 1).min())
    return dict(cagr=round(cagr * 100, 2), mdd=round(mdd * 100, 2),
                calmar=round(cagr / abs(mdd), 2) if mdd < 0 else np.nan)


def seg_ret(nav: pd.Series, start: str, end: str) -> float:
    n = nav.loc[start:end]
    return float(n.iloc[-1] / n.iloc[0] - 1)


def full_report(nav: pd.Series, name: str) -> dict:
    r = {"name": name}
    r.update({f"all_{k}": v for k, v in seg_stats(nav).items()})
    r.update({f"e_{k}": v for k, v in seg_stats(nav, *SEG1).items()})
    r.update({f"l_{k}": v for k, v in seg_stats(nav, *SEG2).items()})
    r["q1_24"] = seg_stats(nav, "2024-01-01", "2024-02-08")["mdd"]
    r["jan_20"] = seg_stats(nav, "2020-01-01", "2020-02-10")["mdd"]
    r["q2_24"] = seg_stats(nav, "2024-04-12", "2024-06-30")["mdd"]
    r["turnover"] = round(nav.attrs.get("annual_turnover", np.nan), 2)
    r["pass"] = (r["all_mdd"] >= -20) and (r["all_calmar"] >= 0.8) and (r["l_cagr"] >= 8)
    return r


def main():
    print("[1/3] 运行新基线腿（G-noROE, TTM）...")
    sc = run_sc_leg()
    etf_index = sc.index
    print("[2/3] 加载防御/对照 ETF...")
    navs = {
        "SC": sc,
        "BOND": load_etf_nav("511260", etf_index),
        "GOLD": load_etf_nav("518880", etf_index),
        "HS300": load_etf_nav("510300", etf_index),
    }
    # 再平衡日：每月首个交易日（跳过首个）
    months = pd.Series(etf_index, index=etf_index).dt.to_period("M")
    reb_dates = etf_index[~months.duplicated()][1:]
    print(f"    再平衡 {len(reb_dates)} 次（{reb_dates[0].date()} ~ {reb_dates[-1].date()}）")

    print("[3/3] 组合与报告...")
    cases = [
        ("B0 新基线100%", navs["SC"].rename("nav"), None),
        ("B1 长债50+黄金50", combine({"BOND": navs["BOND"], "GOLD": navs["GOLD"]},
                                    {"BOND": 0.5, "GOLD": 0.5},
                                    {"BOND": COST_ETF, "GOLD": COST_ETF}, reb_dates), None),
        ("B2 SC40+沪深300 60", combine({"SC": navs["SC"], "HS300": navs["HS300"]},
                                       {"SC": 0.4, "HS300": 0.6},
                                       {"SC": COST_SC, "HS300": COST_ETF}, reb_dates), None),
        ("P30 SC30+债35+金35", combine({"SC": navs["SC"], "BOND": navs["BOND"], "GOLD": navs["GOLD"]},
                                       {"SC": 0.30, "BOND": 0.35, "GOLD": 0.35},
                                       {"SC": COST_SC, "BOND": COST_ETF, "GOLD": COST_ETF}, reb_dates), None),
        ("P40 SC40+债30+金30", combine({"SC": navs["SC"], "BOND": navs["BOND"], "GOLD": navs["GOLD"]},
                                       {"SC": 0.40, "BOND": 0.30, "GOLD": 0.30},
                                       {"SC": COST_SC, "BOND": COST_ETF, "GOLD": COST_ETF}, reb_dates), None),
        ("P50 SC50+债25+金25", combine({"SC": navs["SC"], "BOND": navs["BOND"], "GOLD": navs["GOLD"]},
                                       {"SC": 0.50, "BOND": 0.25, "GOLD": 0.25},
                                       {"SC": COST_SC, "BOND": COST_ETF, "GOLD": COST_ETF}, reb_dates), None),
        ("P60 SC60+债20+金20", combine({"SC": navs["SC"], "BOND": navs["BOND"], "GOLD": navs["GOLD"]},
                                       {"SC": 0.60, "BOND": 0.20, "GOLD": 0.20},
                                       {"SC": COST_SC, "BOND": COST_ETF, "GOLD": COST_ETF}, reb_dates), None),
    ]
    reports = {}
    for name, nav, _ in cases:
        reports[name] = full_report(nav, name)

    hdr = (f"{'配置':<16} {'全CAGR':>7} {'MDD':>7} {'Cal':>5} {'19-22C':>7} {'19-22M':>7} "
           f"{'23-26C':>7} {'23-26M':>7} {'24Q1M':>7} {'20.01M':>7} {'24Q2M':>7} {'换手':>5} 判据")
    print(hdr)
    for name, r in reports.items():
        print(f"{name:<16} {r['all_cagr']:>6.2f}% {r['all_mdd']:>6.2f}% {r['all_calmar']:>5.2f} "
              f"{r['e_cagr']:>6.2f}% {r['e_mdd']:>6.2f}% {r['l_cagr']:>6.2f}% {r['l_mdd']:>6.2f}% "
              f"{r['q1_24']:>6.2f}% {r['jan_20']:>6.2f}% {r['q2_24']:>6.2f}% "
              f"{r['turnover']:>5.2f} {'PASS' if r['pass'] else 'fail'}")

    print("\n===== B1 防御腿对冲确认（区间收益）=====")
    b1 = cases[1][1]
    print(f"  24Q1(01-02~02-08): {seg_ret(b1, '2024-01-01', '2024-02-08')*100:+.2f}%")
    print(f"  2020.01(01-02~02-10): {seg_ret(b1, '2020-01-01', '2020-02-10')*100:+.2f}%")
    print(f"  2022 全年: {seg_ret(b1, '2022-01-01', '2022-12-31')*100:+.2f}%")
    print("\n===== 防御腿单独（先验对照：长期 5-7%、MDD -8~-12%）=====")
    print(f"  B1: 全样本 {reports['B1 长债50+黄金50']['all_cagr']:.2f}% / "
          f"MDD {reports['B1 长债50+黄金50']['all_mdd']:.2f}%")

    print("\n===== 先验对照（审阅第四节）=====")
    print(f"  B2 与 P40 的 MDD 差: {reports['B2 SC40+沪深300 60']['all_mdd'] - reports['P40 SC40+债30+金30']['all_mdd']:.2f}pp"
          f"（>5pp=防御腿有效；<3pp=无特异性）")
    print(f"  P60 组合 MDD vs 预估 -17~-20%: 实际 {reports['P60 SC60+债20+金20']['all_mdd']:.2f}%")


if __name__ == "__main__":
    main()

"""步骤 1（审阅修订版）：新基线 TTM 全区间 ± 1 月窗口 + 门禁正确对照复算。

1a：贝塔10 + 避雷 + 4 月空仓（无 1 月）
1b：贝塔10 + 避雷 + 4 月 + 1 月空仓（= G4'-TTM，3.5 门禁的正确对照——对照组只差一层）

输出：全样本 / 两段 / 分年 / MDD / 24Q1 段 / 换手。
门禁复算：用 1b 在 23-26 的数字重算 H5' 与 G-去ROE 的纯打分超额（对照=1b），
按 ≥ +3pp 门禁做打分层去留最终裁决。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd  # noqa: E402

from audit_small_val import H5, TTMFinPIT, build_ttm_fin  # noqa: E402
from backtest_small_val import (  # noqa: E402
    BarStore, SmallCapStrategy, V1, load_config, load_fundamental_panel,
    load_listed_days, load_md, run_once,
)

CAP = 30_000
SEG1 = ("2019-01-01", "2022-12-31")
SEG2 = ("2023-01-01", "2026-08-31")

# 对照组只差一层：贝塔10+避雷 + 日历变体
BETA10_BASE = {**V1, "n": 10, "timing": "none", "calendar": "simple"}
C1A = {**BETA10_BASE, "cal_jan_on": False}                       # 仅 4 月空仓
C1B = {**BETA10_BASE}                                             # 4 月 + 1 月
G_NOROE = {**H5, "w_cap": 0.45, "w_rev": 0.30, "w_to": 0.25, "w_roe": 0.0}


def seg_cagr(eq, s, e):
    x = eq[(eq.index >= s) & (eq.index <= e)]
    return float((x.iloc[-1] / x.iloc[0]) ** (1 / (len(x) / 244.0)) - 1)


def seg_mdd(eq, s, e):
    x = eq[(eq.index >= s) & (eq.index <= e)]
    return float((x / x.cummax() - 1).min())


def yearly(eq):
    y = eq.groupby(eq.index.year).agg(["first", "last"])
    out, prev = {}, None
    for yr, row in y.iterrows():
        base = prev if prev else row["first"]
        out[str(yr)] = float(row["last"] / base - 1)
        prev = row["last"]
    return out


def main():
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

    results = {}
    for label, params in (("1a 贝塔+4月空仓", C1A), ("1b 贝塔+4月+1月空仓", C1B),
                          ("H5'(TTM 40/25/20/15)", H5), ("G-去ROE(45/30/25/0)", G_NOROE)):
        r = run_once(md, bt, SmallCapStrategy(idx_close, fin_ttm, listed, fund, **params), label)
        results[label] = r
        eq = r["equity"]
        q1 = seg_cagr(eq, "2024-01-01", "2024-03-31")
        print(f"{label:<22} 全样本 {r['cagr']*100:6.2f}%  MDD {r['mdd']*100:7.2f}%  "
              f"Sharpe {r['sharpe']:5.2f}  换手 {r['annual_turnover']*100:4.0f}%  "
              f"[19-22 {seg_cagr(eq, *SEG1)*100:6.2f}% | 23-26 {seg_cagr(eq, *SEG2)*100:6.2f}% | "
              f"24Q1 {q1*100:6.2f}%]")

    print("\n===== 分年（1a / 1b / H5' / G-去ROE）=====")
    ys = {k: yearly(r["equity"]) for k, r in results.items()}
    print(f"{'年份':<6} {'1a':>8} {'1b':>8} {'H5p':>8} {'去ROE':>8} {'打分超额(去ROE-1b)':>18}")
    for yr in sorted(set(ys["1a 贝塔+4月空仓"])):
        row = f"{yr:<6}"
        for k in ("1a 贝塔+4月空仓", "1b 贝塔+4月+1月空仓", "H5'(TTM 40/25/20/15)", "G-去ROE(45/30/25/0)"):
            v = ys[k].get(yr)
            row += f"{v*100:>7.2f}%" if v is not None else f"{'NA':>8}"
        ex = ys["G-去ROE(45/30/25/0)"].get(yr, 0) - ys["1b 贝塔+4月+1月空仓"].get(yr, 0)
        row += f"{ex*100:>17.2f}pp"
        print(row)

    print("\n===== 门禁复算（正确对照=1b，23-26 段纯打分超额）=====")
    c_1b = seg_cagr(results["1b 贝塔+4月+1月空仓"]["equity"], *SEG2)
    for k in ("H5'(TTM 40/25/20/15)", "G-去ROE(45/30/25/0)"):
        c = seg_cagr(results[k]["equity"], *SEG2)
        print(f"  {k:<22} 23-26 {c*100:.2f}% - 1b {c_1b*100:.2f}% = 纯打分超额 {(c-c_1b)*100:+.2f}pp"
              f"  → {'过门禁(保留打分)' if (c-c_1b) >= 0.03 else '不过门禁(删除打分)'}")

    print("\n===== 1 月窗口去留（1a vs 1b）=====")
    ra, rb = results["1a 贝塔+4月空仓"], results["1b 贝塔+4月+1月空仓"]
    print(f"  全样本: 1a {ra['cagr']*100:.2f}% vs 1b {rb['cagr']*100:.2f}% "
          f"（差 {(rb['cagr']-ra['cagr'])*100:+.2f}pp）")
    print(f"  23-26 : 1a {seg_cagr(ra['equity'], *SEG2)*100:.2f}% vs "
          f"1b {seg_cagr(rb['equity'], *SEG2)*100:.2f}%")
    print(f"  MDD   : 1a {ra['mdd']*100:.2f}% vs 1b {rb['mdd']*100:.2f}%")
    print(f"  24Q1  : 1a {seg_cagr(ra['equity'], '2024-01-01', '2024-03-31')*100:.2f}% vs "
          f"1b {seg_cagr(rb['equity'], '2024-01-01', '2024-03-31')*100:.2f}%")


if __name__ == "__main__":
    main()

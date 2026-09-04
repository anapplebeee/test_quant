"""3.0 分段归因（审阅重排后必做项）：三序列两段表 + 2023-2026 分年。

序列：
- 同池等权（全池等权，无选股）
- 贝塔10（纯市值 Top10 等权 + 避雷，无打分无日历）——累计口径与 TTM 口径各一
- H5（完整，累计口径）/ H5'（完整，TTM 口径）

两段：2019-2022 / 2023-2026.08。
判决变量：H5' 相对贝塔10 的超额在两段的变化 → 区分"贝塔衰减 vs 阿尔法衰减"。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from audit_small_val import BETA10, H5, TTMFinPIT, build_ttm_fin  # noqa: E402
from backtest_small_val import (  # noqa: E402
    BarStore, FinPIT, SmallCapStrategy, load_config, load_fundamental_panel,
    load_listed_days, load_md, run_once,
)

SEG1 = ("2019-01-01", "2022-12-31")
SEG2 = ("2023-01-01", "2026-08-31")


def seg_cagr(eq: pd.Series, s: str, e: str) -> float:
    x = eq[(eq.index >= s) & (eq.index <= e)]
    return float((x.iloc[-1] / x.iloc[0]) ** (1 / (len(x) / 244.0)) - 1)


def seg_mdd(eq: pd.Series, s: str, e: str) -> float:
    x = eq[(eq.index >= s) & (eq.index <= e)]
    return float((x / x.cummax() - 1).min())


def main():
    md = load_md()
    fund = load_fundamental_panel(md.dates)
    listed = load_listed_days(md.dates)
    idx = BarStore().load_benchmark("000852")
    idx_close = pd.Series(idx["close"].values, index=pd.to_datetime(idx["date"]))
    bt = load_config()["backtest"]

    fin_cum = FinPIT()
    fin_ttm = TTMFinPIT(build_ttm_fin(pd.read_parquet(
        "data/factors/financials.parquet",
        columns=["symbol", "date", "announcement_date", "revenue", "net_profit", "roe"],
    )))

    eqs: dict[str, pd.Series] = {}
    # 同池等权（无选股基线）
    equal = md.close_val.mean(axis=1, skipna=True)
    eqs["同池等权"] = equal / equal.iloc[0] * 30_000
    for name, fin, params in (
        ("贝塔10(累计)", fin_cum, BETA10),
        ("贝塔10-TTM", fin_ttm, BETA10),
        ("H5(累计)", fin_cum, H5),
        ("H5'(TTM)", fin_ttm, H5),
    ):
        eqs[name] = run_once(md, bt, SmallCapStrategy(idx_close, fin, listed, fund, **params),
                             name)["equity"]

    print("===== 三序列两段表（CAGR | MDD）=====")
    print(f"{'序列':<12} {'19-22 CAGR':>10} {'19-22 MDD':>10} {'23-26 CAGR':>10} {'23-26 MDD':>10}")
    for name, eq in eqs.items():
        print(f"{name:<12} {seg_cagr(eq, *SEG1)*100:>9.2f}% {seg_mdd(eq, *SEG1)*100:>9.1f}% "
              f"{seg_cagr(eq, *SEG2)*100:>9.2f}% {seg_mdd(eq, *SEG2)*100:>9.1f}%")

    print("\n===== H5' 相对贝塔10-TTM 的超额（年度）=====")
    y_h5 = run_yearly(eqs["H5'(TTM)"])
    y_b = run_yearly(eqs["贝塔10-TTM"])
    y_e = run_yearly(eqs["同池等权"])
    print(f"{'年份':<6} {'贝塔10-TTM':>10} {'H5(TTM)':>10} {'超额':>8} {'同池等权':>10} {'贝塔超额':>8}")
    for yr in sorted(set(y_h5) | set(y_b)):
        e = y_h5.get(yr, np.nan) - y_b.get(yr, np.nan)
        eb = y_b.get(yr, np.nan) - y_e.get(yr, np.nan)
        print(f"{yr:<6} {y_b.get(yr, np.nan)*100:>9.2f}% {y_h5.get(yr, np.nan)*100:>9.2f}% "
              f"{e*100:>7.2f}pp {y_e.get(yr, np.nan)*100:>9.2f}% {eb*100:>7.2f}pp")

    print("\n===== 段内超额汇总 =====")
    for seg, tag in ((SEG1, "19-22"), (SEG2, "23-26")):
        c_h5 = seg_cagr(eqs["H5'(TTM)"], *seg)
        c_b = seg_cagr(eqs["贝塔10-TTM"], *seg)
        c_e = seg_cagr(eqs["同池等权"], *seg)
        print(f"{tag}: H5' {c_h5*100:.2f}% - 贝塔10 {c_b*100:.2f}% = 打分超额 {((c_h5-c_b)*100):+.2f}pp | "
              f"贝塔10 - 同池等权 {c_e*100:.2f}% = 贝塔10超额 {((c_b-c_e)*100):+.2f}pp")


def run_yearly(eq: pd.Series) -> dict[str, float]:
    y = eq.groupby(eq.index.year).agg(["first", "last"])
    out, prev = {}, None
    for yr, row in y.iterrows():
        base = prev if prev else row["first"]
        out[str(yr)] = float(row["last"] / base - 1)
        prev = row["last"]
    return out


if __name__ == "__main__":
    main()

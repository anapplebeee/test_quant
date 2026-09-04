"""3.5 因子减法门禁（预注册，2 个配置）：3.0 已触发停止条件前半
（H5' 近四年相对贝塔10 超额 -5.19pp < +3pp），按预注册跑确认测试：

- G-去ROE：权重 市值45 / 反转30 / 换手25 / ROE 0
- G-反转10：反转 25→10（40/10/20/15 归一化）

采纳判据：23-26 段相对贝塔10-TTM 超额是否回到 +3pp 以上；两段同向。
若无改善 → 删除打分层，H5 退化为"贝塔10 + 避雷 + 4 月空仓"，重新走 3.2。
全部 TTM 口径。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd  # noqa: E402

from audit_small_val import BETA10, H5, TTMFinPIT, build_ttm_fin  # noqa: E402
from backtest_small_val import (  # noqa: E402
    BarStore, SmallCapStrategy, load_config, load_fundamental_panel,
    load_listed_days, load_md, run_once,
)

SEG1 = ("2019-01-01", "2022-12-31")
SEG2 = ("2023-01-01", "2026-08-31")


def seg_cagr(eq, s, e):
    x = eq[(eq.index >= s) & (eq.index <= e)]
    return float((x.iloc[-1] / x.iloc[0]) ** (1 / (len(x) / 244.0)) - 1)


def cagr_ex2024(eq):
    a = eq[eq.index < "2024-01-01"]
    b = eq[eq.index >= "2025-01-01"]
    return float((a.iloc[-1] / a.iloc[0] * b.iloc[-1] / b.iloc[0]) ** (1 / ((len(a) + len(b)) / 244.0)) - 1)


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

    cases = [
        ("G-去ROE(45/30/25/0)", {**H5, "w_cap": 0.45, "w_rev": 0.30, "w_to": 0.25, "w_roe": 0.0}),
        ("G-反转10(40/10/20/15归一)", {**H5, "w_rev": 0.10}),
    ]
    for label, params in cases:
        r = run_once(md, bt, SmallCapStrategy(idx_close, fin_ttm, listed, fund, **params), label)
        eq = r["equity"]
        c1, c2 = seg_cagr(eq, *SEG1), seg_cagr(eq, *SEG2)
        print(f"{label:<26} 全样本 {r['cagr']*100:6.2f}%  MDD {r['mdd']*100:7.2f}%  "
              f"Sharpe {r['sharpe']:5.2f}  换手 {r['annual_turnover']*100:4.0f}%  "
              f"[19-22 {c1*100:.1f}% | 23-26 {c2*100:.1f}% | 剔2024 {cagr_ex2024(eq)*100:.1f}%]")

    print("\n基准（seg_attr 已测）：H5' 全样本 28.34% [19-22 42.28% | 23-26 13.68%]；"
          "贝塔10-TTM [19-22 11.80% | 23-26 18.87%]，23-26 打分超额 -5.19pp")
    print("门禁：23-26 段超额 ≥ +3pp 才保留打分层；否则删除打分层 → 贝塔10+避雷+4月空仓 重走 3.2")


if __name__ == "__main__":
    main()

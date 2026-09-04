"""opt_small_val.md 第 1~2 步检验（RESEARCH-015 审阅回应）。

Part A 幸存者偏差：退市覆盖统计 + 退市股退市前是否满足 H5 入选条件 +
       H5 历史持仓持有后 2 年内退市计数；
Part B TTM 口径修正：ROE/营收/净利 TTM 化 → 修正基线 H5'（全区间 + 2019-2022/2023-2026 分段）；
Part C 2023 分月归因（H5 vs 贝塔10 vs 同池等权）+ 剔除 2024 年后日历空仓贡献重测
       （拆 1 月 / 4 月两个子窗口）；
Part D 成本校准：H5 每笔 order_value/ADV20 分布 + 年换手。
"""
from __future__ import annotations

import gc
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from backtest_small_val import (  # noqa: E402
    FULL, ORIG, V1, V2, BarStore, FinPIT, SmallCapStrategy, START, load_config,
    load_fundamental_panel, load_listed_days, load_md, run_once, yearly_returns,
)
from quart.backtest.engine import BacktestEngine  # noqa: E402
from quart.execution.fees import Fees  # noqa: E402

CAP = 30_000
H5 = {**V1, "n": 10, "timing": "none", "calendar": "simple", "use_score": True}
BETA10 = {**V1, "n": 10, "timing": "none", "calendar": "none"}


# ----------------------------------------------------------------------------
# TTM 财务构造
# ----------------------------------------------------------------------------
def build_ttm_fin(fin: pd.DataFrame) -> pd.DataFrame:
    """把累计口径 ROE/营收/净利 TTM 化：TTM_t = 年报(上年) + 累计(t) - 累计(上年同期)。

    可用时点 = 当前报告期 announcement_date（与原口径一致，无前视）。
    """
    fin = fin.copy()
    fin["date"] = pd.to_datetime(fin["date"])
    fin = fin.sort_values(["symbol", "date"])
    key = fin.set_index(["symbol", "date"])[["roe", "revenue", "net_profit"]]
    fin["date_py"] = fin["date"] - pd.DateOffset(years=1)
    m = fin.merge(
        key.rename(columns={"roe": "roe_py", "revenue": "rev_py", "net_profit": "np_py"}),
        left_on=["symbol", "date_py"], right_index=True, how="left",
    )
    m["date_annual"] = pd.to_datetime(
        m["date"].dt.year.astype(str) + "-12-31"
    ) - pd.DateOffset(years=1)
    m = m.merge(
        key.rename(columns={"roe": "roe_a", "revenue": "rev_a", "net_profit": "np_a"}),
        left_on=["symbol", "date_annual"], right_index=True, how="left",
    )
    for dst, ann, cur, py in (
        ("roe_ttm", "roe_a", "roe", "roe_py"),
        ("rev_ttm", "rev_a", "revenue", "rev_py"),
        ("np_ttm", "np_a", "net_profit", "np_py"),
    ):
        m[dst] = m[ann] + m[cur] - m[py]
    # 上年年报或上年同期缺失（上市 <1 年）→ 退回累计口径
    for dst, cur in (("roe_ttm", "roe"), ("rev_ttm", "revenue"), ("np_ttm", "net_profit")):
        m[dst] = m[dst].where(m[dst].notna(), m[cur])
    out = pd.DataFrame({
        "symbol": m["symbol"],
        "announcement_date": m["announcement_date"],
        "revenue": m["rev_ttm"],
        "net_profit": m["np_ttm"],
        "roe": m["roe_ttm"],
    })
    return out.dropna(subset=["announcement_date"])


class TTMFinPIT(FinPIT):
    """财务快照改用 TTM 口径。"""

    def __init__(self, fin: pd.DataFrame):
        fin = fin.copy()
        fin["announcement_date"] = pd.to_datetime(fin["announcement_date"])
        self.fin = fin.sort_values(["announcement_date", "symbol"]).reset_index(drop=True)
        self._cache: dict[pd.Timestamp, pd.DataFrame] = {}


# ----------------------------------------------------------------------------
# Part A 幸存者偏差
# ----------------------------------------------------------------------------
def part_a(md, fund, fin, listed, idx_close, bt):
    print("=" * 70)
    print("Part A 幸存者偏差核查")
    print("=" * 70)
    dl = pd.read_parquet("data/meta/delisted.parquet")
    dl["delisted_at"] = pd.to_datetime(dl["delisted_at"])
    r = dl[(dl.delisted_at >= "2019-01-01") & (dl.delisted_at <= "2026-08-31")].copy()
    r["code"] = r["code"].astype(str).str.zfill(6)
    import pathlib

    daily = {p.stem for p in pathlib.Path("data/daily").glob("*.parquet")}
    r["in_daily"] = r["code"].isin(daily)
    print(f"2019-2026 退市 {len(r)} 只：行情库覆盖 {int(r.in_daily.sum())}，"
          f"缺失 {int((~r.in_daily).sum())}")

    # 有数据的退市股：退市前是否满足 H5 入选条件（价3-25/市值15-60亿/非ST/日均额5000万）
    ok_stocks = r[r.in_daily]["code"].tolist()
    cands = 0
    detail = []
    for sym in ok_stocks:
        try:
            bar = pd.read_parquet(f"data/daily/{sym}.parquet")
        except Exception:
            continue
        bar["date"] = pd.to_datetime(bar["date"])
        d_del = r.loc[r.code == sym, "delisted_at"].iloc[0]
        bar = bar[bar.date < d_del].tail(250)  # 退市前一年
        if bar.empty or "close" not in bar.columns:
            continue
        fm = fund["fmcap"][sym].reindex(bar.date) if sym in fund["fmcap"].columns else None
        px_ok = (bar["close"].between(3.0, 25.0)).any()
        mcap_ok = bool((fm.between(15e8, 60e8)).any()) if fm is not None else False
        amt_ok = bool((bar.get("amount", pd.Series(dtype=float)) >= 5e7).any())
        st_ok = True  # ST 序列在 fund["st"]；此处近似：价+市值双满足即视为曾入候选
        if px_ok and mcap_ok and amt_ok:
            cands += 1
            detail.append(sym)
    print(f"有数据退市股 {len(ok_stocks)} 中，退市前 250 日内曾满足 H5 价/市值/流动性条件的："
          f"{cands} 只（{cands/max(len(ok_stocks),1)*100:.0f}%）")
    print(f"  样例: {detail[:8]}")

    # 缺失的 125 只：fundamental_daily 里有没有（判断退市前市值分布）
    fnd = pd.read_parquet("data/factors/fundamental_daily.parquet",
                          columns=["symbol", "float_mcap"])
    fnd_syms = set(fnd.symbol.astype(str).str.zfill(6))
    miss = r[~r.in_daily]
    miss_in_fnd = miss["code"].isin(fnd_syms)
    print(f"缺失行情的 {len(miss)} 只退市股中，fundamental 有记录的：{int(miss_in_fnd.sum())}"
          f"（fundamental 若有则其市值分布可查）")
    if miss_in_fnd.any():
        mc = fnd[fnd.symbol.astype(str).str.zfill(6).isin(miss.loc[miss_in_fnd, "code"])]
        print(f"  其流通市值分布（全历史）: 中位 {mc.float_mcap.median()/1e8:.1f}亿, "
              f"p25 {mc.float_mcap.quantile(.25)/1e8:.1f}亿, p75 {mc.float_mcap.quantile(.75)/1e8:.1f}亿")

    # H5 历史持仓 → 持有后 2 年内退市
    strat = SmallCapStrategy(idx_close, fin, listed, fund, **H5)
    strat.prepare(md)
    picks_log = []
    for i in range(70, len(md.dates)):
        w = strat.target_weights(i)
        for s in (w or {}):
            if s != "__FLAT__":
                picks_log.append((md.dates[i], s))
    picks = pd.DataFrame(picks_log, columns=["date", "symbol"])
    picks["symbol"] = picks["symbol"].astype(str).str.zfill(6)
    uniq = picks["symbol"].unique()
    dd = dl.set_index("code")["delisted_at"]
    two_y = 0
    for s in uniq:
        if s in dd.index:
            d0 = picks.loc[picks.symbol == s, "date"].min()
            if dd[s] <= d0 + pd.Timedelta(days=730):
                two_y += 1
    print(f"H5 历史持仓 {picks.symbol.nunique()} 只（{len(picks)} 次选入）："
          f"首次选入后 2 年内退市的 {two_y} 只")
    # 选入时点该票当时是否已在退市清单（含库外退市）统计
    in_dl = picks["symbol"].isin(dd.index).sum()
    print(f"  历史选入记录中属于任一退市股的次数: {in_dl}/{len(picks)}")
    return picks


# ----------------------------------------------------------------------------
# Part B TTM 修正基线
# ----------------------------------------------------------------------------
def part_b(md, fund, listed, idx_close, bt, fin_orig):
    print("=" * 70)
    print("Part B TTM 口径修正基线 H5'")
    print("=" * 70)
    fin_ttm = build_ttm_fin(pd.read_parquet(
        "data/factors/financials.parquet",
        columns=["symbol", "date", "announcement_date", "revenue", "net_profit", "roe"],
    ))
    print(f"TTM 长表 {len(fin_ttm)} 行（原 {len(fin_orig.fin)} 行）")
    for label, fin in (("H5(累计口径)", fin_orig), ("H5'(TTM 口径)", TTMFinPIT(fin_ttm))):
        r = run_once(md, bt, SmallCapStrategy(idx_close, fin, listed, fund, **H5), label)
        eq = r["equity"]
        seg1 = eq[eq.index < "2023-01-01"]
        seg2 = eq[eq.index >= "2023-01-01"]
        c1 = (seg1.iloc[-1] / seg1.iloc[0]) ** (1 / (len(seg1) / 244)) - 1
        c2 = (seg2.iloc[-1] / seg2.iloc[0]) ** (1 / (len(seg2) / 244)) - 1
        print(f"{label:<14} CAGR {r['cagr']*100:6.2f}%  MDD {r['mdd']*100:7.2f}%  "
              f"Sharpe {r['sharpe']:5.2f}  换手 {r['annual_turnover']*100:4.0f}%  "
              f"[19-22 {c1*100:.1f}% | 23-26 {c2*100:.1f}%]")


# ----------------------------------------------------------------------------
# Part C 2023 归因 + 剔 2024 日历贡献
# ----------------------------------------------------------------------------
def part_c(md, fund, listed, idx_close, bt, fin_orig):
    print("=" * 70)
    print("Part C 2023 分月归因 + 剔 2024 日历贡献")
    print("=" * 70)
    eqs = {}
    eqs["BETA10(贝塔)"] = run_once(md, bt, SmallCapStrategy(
        idx_close, fin_orig, listed, fund, **{**BETA10, "calendar": "none"}), "b")["equity"]
    eqs["G4(贝塔+日历)"] = run_once(md, bt, SmallCapStrategy(
        idx_close, fin_orig, listed, fund, **BETA10 | {"calendar": "simple"}), "g")["equity"]
    eqs["H5(完整)"] = run_once(md, bt, SmallCapStrategy(
        idx_close, fin_orig, listed, fund, **H5), "h")["equity"]
    equal = md.close_val.mean(axis=1, skipna=True)
    equal = equal / equal.iloc[0] * CAP
    eqs["同池等权"] = equal

    print("\n-- 2023 分月收益 --")
    months = pd.period_range("2023-01", "2023-12", freq="M")
    header = "月份    " + "".join(f"{k[:12]:>14}" for k in eqs)
    print(header)
    for pm in months:
        row = f"{pm} "
        for name, eq in eqs.items():
            seg = eq[(eq.index >= str(pm.start_time.date())) & (eq.index <= str(pm.end_time.date()))]
            row += f"{(seg.iloc[-1]/seg.iloc[0]-1)*100:>13.2f}%" if len(seg) > 1 else f"{'NA':>14}"
        print(row)

    # 剔 2024：2019-2023 与 2025-2026.08 拼接
    def cagr_ex2024(eq):
        a = eq[(eq.index < "2024-01-01")]
        b = eq[(eq.index >= "2025-01-01")]
        total = a.iloc[-1] / a.iloc[0] * b.iloc[-1] / b.iloc[0]
        days = len(a) + len(b)
        return total ** (1 / (days / 244)) - 1

    print("\n-- 剔除 2024 年后的日历空仓贡献 --")
    for name in ("BETA10(贝塔)", "G4(贝塔+日历)", "H5(完整)"):
        print(f"  {name:<14} 全样本 CAGR {cagr_ex2024(eqs[name])*100:6.2f}%")

    # 拆 1 月 / 4 月贡献
    class CalJanOnly(SmallCapStrategy):
        def _calendar_mult_at(self, i, d):
            if d.month == 1:
                return 0.0
            return 1.0

    class CalAprOnly(SmallCapStrategy):
        def _calendar_mult_at(self, i, d):
            if d.month == 4 and d.day >= 20:
                return 0.0
            return 1.0

    for cls, name in ((CalJanOnly, "仅1月空仓"), (CalAprOnly, "仅4/20后空仓")):
        strat = cls(idx_close, fin_orig, listed, fund, **{**BETA10, "timing": "none"})
        r = run_once(md, bt, strat, name)
        print(f"  {name:<14} 全样本 CAGR {r['cagr']*100:6.2f}%  MDD {r['mdd']*100:7.2f}%  "
              f"剔2024 CAGR {cagr_ex2024(r['equity'])*100:6.2f}%")


# ----------------------------------------------------------------------------
# Part D 成本校准
# ----------------------------------------------------------------------------
def part_d(md, bt, fund, listed, idx_close, fin_orig):
    print("=" * 70)
    print("Part D 成本校准（order/ADV 分布）")
    print("=" * 70)
    fees = Fees(bt["commission_rate"], bt["commission_min"], bt["stamp_tax_rate"],
                bt["transfer_fee_rate"], bt["slippage_rate"], float(bt.get("impact_coef", 0.0)))
    strat = SmallCapStrategy(idx_close, fin_orig, listed, fund, **H5)
    engine = BacktestEngine(md, strat, fees=fees, initial_cash=CAP,
                            max_adv_participation=0.05)
    engine.run()
    trades = pd.DataFrame([{
        "date": t.date, "symbol": str(t.symbol).zfill(6), "amount": float(t.amount),
        "side": getattr(t, "side", "?"),
    } for t in engine.trades])
    trades["date"] = pd.to_datetime(trades["date"])
    adv20 = md.amounts.rolling(20, min_periods=5).mean()
    ratios = []
    for _, row in trades.iterrows():
        d, s = row["date"], row["symbol"]
        idx = md.dates.searchsorted(d)
        if idx > 0 and s in adv20.columns:
            v = adv20.iloc[idx - 1].get(s, np.nan)
            if pd.notna(v) and v > 0:
                ratios.append(row["amount"] / v)
    ratios = np.array(ratios)
    print(f"成交 {len(trades)} 笔；order_value/ADV20 分位数：")
    if len(ratios):
        for q in (0.1, 0.25, 0.5, 0.75, 0.9):
            print(f"  p{int(q*100)}: {np.quantile(ratios, q)*100:.3f}%")
        print(f"  中位 {np.median(ratios)*100:.3f}%（<0.1% → 固定 impact 0.10 属过度惩罚）")
    # spread 覆盖检查：H5 候选价 3-25 元 → 单边 spread 0.03%~0.33%，
    print("  spread 评估：价 3-25 元 tick 0.01 → 单边 spread 0.04%~0.33%；"
          "slippage 0.1% 对低价票覆盖不足，对中高价票充足")


def main():
    md = load_md()
    fund = load_fundamental_panel(md.dates)
    fin_orig = FinPIT()
    listed = load_listed_days(md.dates)
    idx = BarStore().load_benchmark("000852")
    idx_close = pd.Series(idx["close"].values, index=pd.to_datetime(idx["date"]))
    bt = load_config()["backtest"]

    picks = part_a(md, fund, fin_orig, listed, idx_close, bt)
    part_b(md, fund, listed, idx_close, bt, fin_orig)
    part_c(md, fund, listed, idx_close, bt, fin_orig)
    part_d(md, bt, fund, listed, idx_close, fin_orig)


if __name__ == "__main__":
    main()

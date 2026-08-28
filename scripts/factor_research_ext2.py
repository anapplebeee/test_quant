"""扩展因子研究 Round 2（2026-08-28）：流动性/风险/量价背离/行业维度新因子。

全部由本地 bar 数据 + 行业簇映射派生，无外部网络依赖：
  amihud20       非流动性（Amihud），预期正 IC（流动性溢价）
  max_ret20      彩票偏好 MAX 因子，预期负 IC（彩票效应折价）
  overnight20    隔夜跳空均值（散户情绪/隔夜信息），A股隔夜收益长期为负
  intraday20     日内收益均值（隔夜-日内拆分的另一端）
  beta60         市场贝塔（等权市场代理），预期负 IC（低贝塔异象）
  idio_vol60     特质波动率，预期负 IC（低风险异象）
  corr_pv20      量价相关性（放量上涨=出货嫌疑），预期负 IC
  res_mom120     残差动量（beta 中性化后 6 个月动量）
  ind_mom20      行业簇动量（stat_industry 映射）
  rel_ind_mom20  个股相对行业动量（行业内反转）
  vwap_pos20     收盘价相对 20 日 VWAP 位置（追高嫌疑），预期负 IC

用法：uv run python scripts/factor_research_ext2.py [--sample monthly|weekly] [--horizon 5]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.backtest.engine import MarketData
from quart.config import PROJECT_ROOT, load_config
from quart.data.store import BarStore
from scripts.factor_research import every_nth, monthly_ends

console = Console()


def _rolling_corr(x: pd.DataFrame, y: pd.DataFrame, window: int) -> pd.DataFrame:
    """列独立的滚动相关：cov = E[xy] - E[x]E[y]（避免逐列 rolling.cov 慢路径）。"""
    exy = (x * y).rolling(window).mean()
    ex, ey = x.rolling(window).mean(), y.rolling(window).mean()
    sx = (x * x).rolling(window).mean() - ex * ex
    sy = (y * y).rolling(window).mean() - ey * ey
    cov = exy - ex * ey
    den = np.sqrt(sx.clip(lower=0)) * np.sqrt(sy.clip(lower=0))
    return (cov / den.replace(0, np.nan)).astype("float64")


def build_r2_factors(md: MarketData) -> dict[str, pd.DataFrame]:
    c = md.close_val
    o = md.opens
    v = md.volumes
    a = md.amounts.ffill()
    ret1 = c.pct_change(fill_method=None)

    mkt = ret1.mean(axis=1)  # 等权市场代理

    amihud20 = (ret1.abs() / a.replace(0, np.nan)).rolling(20).mean() * 1e9
    max_ret20 = ret1.rolling(20).max()
    overnight20 = (o / c.shift(1).replace(0, np.nan) - 1.0).rolling(20).mean()
    intraday20 = (c / o.replace(0, np.nan) - 1.0).rolling(20).mean()

    # beta60 / idio_vol60：滚动矩估计
    w = 60
    mkt_b = pd.concat([mkt] * c.shape[1], axis=1)
    mkt_b.columns = c.columns
    ex, ey = ret1.rolling(w).mean(), mkt_b.rolling(w).mean()
    exy = (ret1 * mkt_b).rolling(w).mean()
    var_m = ((mkt_b * mkt_b).rolling(w).mean() - ey * ey).clip(lower=0)
    beta60 = ((exy - ex * ey) / var_m.replace(0, np.nan)).astype("float64")
    resid = ret1 - beta60 * mkt_b
    idio_vol60 = resid.rolling(w).std()

    corr_pv20 = _rolling_corr(ret1, np.log1p(v.replace(0, np.nan)), 20)

    mom120 = c / c.shift(120) - 1.0
    mkt_mom = mkt_b / mkt_b.shift(120) - 1.0
    res_mom120 = (mom120 - beta60 * mkt_mom).astype("float64")

    # 行业簇因子
    ind_mom20 = None
    rel_ind_mom20 = None
    ind_path = PROJECT_ROOT / "data" / "universe" / "stat_industry.parquet"
    if ind_path.exists():
        imap = pd.read_parquet(ind_path).set_index("symbol")["cluster"]
        ret20 = c / c.shift(20) - 1.0
        cols_in_map = [s for s in ret20.columns if s in imap.index]
        groups = pd.Series([imap.get(s, "NA") for s in ret20.columns], index=ret20.columns)
        ind_ret = ret20.T.groupby(groups).mean().T  # dates × industries
        # 按每个股票所属行业广播行业收益：dates × symbols
        ind_mom20 = ind_ret.reindex(columns=groups.values)
        ind_mom20.columns = ret20.columns
        ind_mom20 = ind_mom20.astype("float64")
        rel_ind_mom20 = (ret20 - ind_mom20).astype("float64")

    vwap20 = a.rolling(20).sum() / v.replace(0, np.nan).rolling(20).sum()
    vwap_pos20 = (c - vwap20) / vwap20.replace(0, np.nan)

    factors = {
        "amihud20": amihud20,
        "max_ret20": max_ret20,
        "overnight20": overnight20,
        "intraday20": intraday20,
        "beta60": beta60,
        "idio_vol60": idio_vol60,
        "corr_pv20": corr_pv20,
        "res_mom120": res_mom120,
        "vwap_pos20": vwap_pos20,
    }
    if ind_mom20 is not None:
        factors["ind_mom20"] = ind_mom20
        factors["rel_ind_mom20"] = rel_ind_mom20
    return {k: val.astype("float64") for k, val in factors.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="monthly", choices=["monthly", "weekly"])
    parser.add_argument("--horizon", type=int, default=5)
    args = parser.parse_args()
    HORIZON = args.horizon

    cfg = load_config()
    store = BarStore()
    bars = store.load(include_index=False)
    md = MarketData.from_bars(bars)

    factors = build_r2_factors(md)

    label = md.close_val.shift(-(HORIZON + 1)) / md.close_val.shift(-1) - 1.0
    amed = md.amounts.ffill().rolling(20).mean()
    eligible_base = amed > 20_000_000

    bench_close = store.load_benchmark(cfg["benchmark"]).set_index("date")["close"].reindex(md.dates).ffill()
    bench_fwd = bench_close.shift(-(HORIZON + 1)) / bench_close.shift(-1) - 1.0

    sampler = monthly_ends if args.sample == "monthly" else every_nth(5)
    ends = sampler(md.dates)
    console.print(f"eval window: {md.dates[ends[0]].date()} ~ {md.dates[ends[-1]].date()} | {len(ends)} points")

    rows = {}
    for name, fw in factors.items():
        ics, spreads = [], []
        for i in ends:
            elig = eligible_base.iloc[i].fillna(False)
            joined = pd.DataFrame({"f": fw.iloc[i], "y": label.iloc[i]}).loc[elig].dropna()
            if len(joined) < 100:
                continue
            fx, fy = joined["f"], joined["y"]
            ics.append(float(fx.corr(fy, method="spearman")))
            q_hi, q_lo = fx.quantile(0.9), fx.quantile(0.1)
            hi_y = fy[fx >= q_hi].clip(-0.5, 2.0).mean()
            lo_y = fy[fx <= q_lo].clip(-0.5, 2.0).mean()
            spread = hi_y - lo_y
            br = bench_fwd.iloc[i]
            spreads.append(float(spread - br) if not np.isnan(br) else float(spread))
        if not ics:
            continue
        s = pd.Series(ics)
        half = max(len(s) // 2, 1)
        rows[name] = {
            "ic": s.mean(), "icir": s.mean() / s.std() if s.std() else np.nan,
            "pos%": (s > 0).mean(),
            "early_half_ic": s.iloc[:half].mean(), "late_half_ic": s.iloc[half:].mean(),
            "ls_bp": float(np.nanmean(spreads)) * 10000, "n": len(s),
        }

    summary = pd.DataFrame(rows).T.sort_values("icir", key=lambda x: x.abs(), ascending=False)
    table = Table(title=f"R2 新因子研究 fwd{HORIZON}d RankIC")
    for col in ["factor", "IC", "ICIR", "正率", "前半段", "后半段", "多空bp", "n"]:
        table.add_column(col, justify="right")
    for name, r in summary.iterrows():
        table.add_row(
            str(name), f"{r['ic']:+.4f}", f"{r['icir']:+.2f}", f"{r['pos%']:.0%}",
            f"{r['early_half_ic']:+.4f}", f"{r['late_half_ic']:+.4f}",
            f"{r['ls_bp']:+.0f}", str(int(r['n'])),
        )
    console.print(table)
    out = PROJECT_ROOT / "reports" / "factor_research_ext2.csv"
    summary.to_csv(out)
    console.print(f"saved: {out}")


if __name__ == "__main__":
    main()

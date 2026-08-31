"""财报因子 + 涨停事件因子挖掘（RankIC 检验）。

对应调研文档（docs/A股市场因子与策略调研报告_2026-08-31.md）第八章：
- 8.1 财报因子（P0：ROE 稳定性/盈利动量/盈利惊喜代理，用现有 factors.py 字段）
- 8.2.2 涨停事件因子（涨停密度情绪择时/涨停次日收益，用 is_limit_up + 日线）

目的：把调研结论落地为可运行的因子 IC 检验，输出与 factor_research.py 一致的
RankIC/ICIR/多空价差，供判断"哪个新因子值得接入策略"。

用法：
    uv run python scripts/mine_factors.py --start 2022-01-01
    uv run python scripts/mine_factors.py --factor-group financial  # 只看财报
    uv run python scripts/mine_factors.py --factor-group limit_up   # 只看涨停
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.config import data_root, load_config
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.execution.constraints import is_limit_up, price_limit_pct
from quart.research.factor_audit import rank_correlation

console = Console()

HORIZON = 5  # 前视收益天数


# ---------------------------------------------------------------- 财报因子


def build_financial_factors(financials: pd.DataFrame, closes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """用财务快照构建财报因子宽表（date × symbol）。

    只需现有 factors.py 字段（roe/profit_yoy/rev_yoy），不依赖新数据源。
    报告期数据做披露时滞（120 天）后前向填充，防前视。

    返回 {factor_name: date×symbol DataFrame}：
      - roe_stab     : ROE 滚动标准差取负（越稳定越好）
      - profit_accel : 净利润增速的相邻差（加速=好）
      - surprise     : 净利润增速 / |营收增速| 代理盈利惊喜
    """
    fin = financials.copy()
    fin["date"] = pd.to_datetime(fin["date"], errors="coerce")
    fin = fin.dropna(subset=["date"]).sort_values(["symbol", "date"])
    for col in ("roe", "profit_yoy", "rev_yoy"):
        if col in fin:
            fin[col] = pd.to_numeric(fin[col], errors="coerce")
    # 披露时滞：报告期 + 120 天才可用（防前视）
    fin["usable_at"] = fin["date"] + pd.Timedelta(days=120)

    idx = pd.DatetimeIndex(closes.index)
    left = pd.DataFrame({"datetime": idx}).sort_values("datetime")

    out: dict[str, list[pd.Series]] = {"roe_stab": [], "profit_accel": [], "surprise": []}
    for sym, g in fin.groupby("symbol"):
        g = g.sort_values("usable_at")
        # 逐符号计算滚动指标
        g["roe_stab"] = -g["roe"].rolling(8, min_periods=4).std()   # ROE 稳定性（负=更稳）
        g["profit_accel"] = g["profit_yoy"].diff()                  # 盈利动量（加速）
        # 盈利惊喜代理：净利增速显著高于营收增速 = 超预期（利润弹性 > 收入弹性）
        g["surprise"] = np.where(
            g["rev_yoy"].abs() > 1e-9,
            g["profit_yoy"] - g["rev_yoy"],
            np.nan,
        )
        m = pd.merge_asof(
            left,
            g[["usable_at", "roe_stab", "profit_accel", "surprise"]]
            .rename(columns={"usable_at": "datetime"}),
            on="datetime",
            direction="backward",
        ).set_index("datetime")
        for f in out:
            if m[f].notna().any():
                out[f].append(m[f].rename(sym).astype("float32"))

    return {f: pd.concat(cols, axis=1) for f, cols in out.items() if cols}


# ---------------------------------------------------------------- 涨停事件因子


def build_limit_up_factors(md: MarketData) -> dict[str, pd.DataFrame]:
    """用日线 + 涨跌停判断构建涨停事件因子。

    涨停判定：当日收盘 == 涨停价（基于前收，见 is_limit_up）。

    - limit_up_density : 全市场涨停家数（情绪周期，时序标量 → 扩成截面用于统一验证）
    - limit_up_next    : 昨日涨停股的次日收益（情绪延续，截面）
    """
    closes = md.close_val
    prev = closes.shift(1)
    # 涨停标记矩阵（date × symbol）
    limit_matrix = pd.DataFrame(
        np.zeros(closes.shape, dtype=bool), index=closes.index, columns=closes.columns
    )
    for sym in closes.columns:
        limit_matrix[sym] = [
            is_limit_up(c, p, sym)
            for c, p in zip(closes[sym], prev[sym])
        ]
    limit_matrix = limit_matrix.fillna(False)

    # 涨停密度（每日涨停家数）
    density = limit_matrix.sum(axis=1).astype(float)
    # 密度均值平滑（情绪周期）
    density_smooth = density.rolling(10, min_periods=3).mean()

    # 涨停次日收益：今日收益 × 昨日是否涨停
    ret1 = closes.pct_change(fill_method=None)
    limit_up_next = (ret1 * limit_matrix.shift(1).astype(float)).where(
        limit_matrix.shift(1)
    )

    # 把时序密度扩成截面（每列都取当日密度值），与财报因子同格式便于统一验证
    density_cs = pd.DataFrame(
        np.tile(density.values[:, None], (1, len(closes.columns))),
        index=closes.index, columns=closes.columns,
    )
    density_smooth_cs = pd.DataFrame(
        np.tile(density_smooth.values[:, None], (1, len(closes.columns))),
        index=closes.index, columns=closes.columns,
    )
    return {
        "limit_up_density": density_cs.astype("float32"),
        "limit_up_density_smooth": density_smooth_cs.astype("float32"),
        "limit_up_next": limit_up_next.astype("float32"),
    }


# ---------------------------------------------------------------- IC 检验


def evaluate_factors(
    factors: dict[str, pd.DataFrame],
    md: MarketData,
    starts: list[int],
) -> pd.DataFrame:
    """对每个因子做 RankIC 检验，返回汇总表（与 factor_research 同口径）。"""
    label = md.opens.shift(-(HORIZON + 1)) / md.opens.shift(-1).replace(0, np.nan) - 1.0
    amed = md.amounts.rolling(20).mean()
    eligible_base = amed > 20_000_000

    rows: dict[str, dict] = {}
    for name, fw in factors.items():
        ics, spreads = [], []
        for i in starts:
            elig = eligible_base.iloc[i].fillna(False)
            joined = pd.DataFrame({"f": fw.iloc[i], "y": label.iloc[i]}).loc[elig].dropna()
            if len(joined) < 300:
                continue
            fx, fy = joined["f"], joined["y"]
            ics.append(rank_correlation(fx, fy))
            q_hi, q_lo = fx.quantile(0.9), fx.quantile(0.1)
            hi_y = fy[fx >= q_hi].clip(-0.5, 2.0).mean()
            lo_y = fy[fx <= q_lo].clip(-0.5, 2.0).mean()
            spreads.append(float(hi_y - lo_y))
        if not ics:
            continue
        s = pd.Series(ics)
        half = max(len(s) // 2, 1)
        rows[name] = {
            "ic": s.mean(),
            "icir": s.mean() / s.std() if s.std() else np.nan,
            "pos%": (s > 0).mean(),
            "early_half_ic": s.iloc[:half].mean(),
            "late_half_ic": s.iloc[half:].mean(),
            "ls_bp": float(np.nanmean(spreads)) * 10000,
            "n": len(s),
        }
    df = pd.DataFrame(rows).T
    if df.empty:
        return df
    return df.sort_values("icir", key=lambda x: x.abs(), ascending=False)


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="财报 + 涨停事件因子挖掘")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--factor-group", default="all", choices=["all", "financial", "limit_up"])
    args = parser.parse_args()

    store = BarStore()
    bars = store.load(start=args.start, end=args.end)
    bench = store.load_benchmark(cfg["benchmark"])
    if bars.empty:
        raise SystemExit("本地数据为空，请先运行 scripts/update_data.py")

    dc = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=dc.get("exclude_star", True),
        exclude_chinext=dc.get("exclude_chinext", True),
        exclude_st=dc.get("exclude_st", True),
        min_list_days=int(dc.get("min_list_days", 0)),
    )
    md = MarketData.from_bars(bars, benchmark=bench)
    # 采样：每 5 个交易日评估一次（月频太稀，财报因子用周频更稳）
    starts = list(range(100, len(md.dates) - HORIZON, 5))
    console.print(f"eval window: {md.dates[starts[0]].date()} ~ {md.dates[starts[-1]].date()} | {len(starts)} points")

    all_factors: dict[str, pd.DataFrame] = {}

    if args.factor_group in ("all", "financial"):
        fin_path = data_root() / "factors" / "financials.parquet"
        if fin_path.exists():
            fin = pd.read_parquet(fin_path)
            fin_factors = build_financial_factors(fin, md.close_val)
            all_factors.update(fin_factors)
            console.print(f"财报因子: {list(fin_factors)}")
        else:
            console.print("[yellow]未找到财务数据，跳过财报因子（运行 scripts/fetch_financial_factors.py）[/yellow]")

    if args.factor_group in ("all", "limit_up"):
        lu_factors = build_limit_up_factors(md)
        all_factors.update(lu_factors)
        console.print(f"涨停事件因子: {list(lu_factors)}")

    if not all_factors:
        raise SystemExit("无可评估因子")

    result = evaluate_factors(all_factors, md, starts)
    table = Table(title=f"新因子挖掘 fwd{HORIZON}d RankIC（2020-2026）")
    for col in ["factor", "IC", "ICIR", "正率", "前半", "后半", "多空bp", "n"]:
        table.add_column(col, justify="right")
    for name, r in result.iterrows():
        table.add_row(
            name,
            f"{r['ic']:.4f}",
            f"{r['icir']:.2f}",
            f"{r['pos%']:.0%}",
            f"{r['early_half_ic']:.4f}",
            f"{r['late_half_ic']:.4f}",
            f"{r['ls_bp']:.0f}",
            str(int(r['n'])),
        )
    console.print(table)

    # 落盘
    out_dir = Path("reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    result.to_csv(out_dir / f"mine_factors_{stamp}.csv", encoding="utf-8-sig")
    console.print(f"[green]saved: reports/mine_factors_{stamp}.csv[/green]")


if __name__ == "__main__":
    main()

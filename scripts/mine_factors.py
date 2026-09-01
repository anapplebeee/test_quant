"""财报因子 + 涨停事件因子挖掘（RankIC 检验）。

对应调研文档（docs/A股市场因子与策略调研报告_2026-08-31.md）第八章：
- 财报因子：ROE 稳定性/盈利加速/利润弹性代理；
- 个股事件：涨停次数、接近涨停次数、放量追板拥挤；
- 市场时序：涨跌停广度；
- 可选事件文件：公告/新闻情绪与龙虎榜净买入。

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
from quart.research.event_factors import (
    dragon_tiger_panels,
    event_sentiment_panels,
    limit_event_panels,
    market_limit_sentiment,
)
from quart.research.factor_audit import rank_correlation
from quart.research.value_growth import pit_panels

console = Console()

HORIZON = 5  # 前视收益天数


# ---------------------------------------------------------------- 财报因子


def build_financial_factors(financials: pd.DataFrame, closes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """用财务快照构建财报因子宽表（date × symbol）。

    只需现有 factors.py 字段（roe/profit_yoy/rev_yoy），不依赖新数据源。
    真实披露时间优先；缺失时以报告期 +120 天保守兜底后前向填充。

    返回 {factor_name: date×symbol DataFrame}：
      - roe_stability: ROE 滚动标准差取负（越稳定越好）
      - profit_accel : 净利润增速的相邻差（加速=好）
      - earnings_surprise_proxy: 净利润增速 - 营收增速（利润弹性代理）
    """
    return pit_panels(
        financials,
        closes,
        factors=("roe_stability", "profit_accel", "earnings_surprise_proxy"),
    )


# ---------------------------------------------------------------- 涨停事件因子


def build_limit_up_factors(md: MarketData) -> dict[str, pd.DataFrame]:
    """用日线 + 涨跌停判断构建涨停事件因子。

    涨停判定：基于历史 RuleBook、前收和交易所价格取整规则。

    - limit_hit_count20_neg       : 近 20 日涨停次数（负向）
    - near_limit_count20_neg      : 近 20 日接近涨停次数（负向）
    - speculative_crowding20_neg  : 接近板位 × 相对成交额拥挤度（负向）

    市场涨停家数是时序信号，另由 ``market_limit_sentiment`` 评估，不能
    复制成截面常数后计算 RankIC。
    """
    return limit_event_panels(md)


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
        fw = fw.copy()
        fw.columns = [str(symbol).replace(".0", "").zfill(6) for symbol in fw.columns]
        fw = fw.reindex(index=md.dates, columns=md.symbols)
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


def evaluate_market_signals(
    signals: pd.DataFrame,
    md: MarketData,
    *,
    horizon: int = HORIZON,
) -> pd.DataFrame:
    """评估市场情绪时序对未来全市场等权收益的预测力。

    与横截面 RankIC 分开：每个交易日只有一个市场涨停密度，正确检验是
    时间序列相关和高/低分位的未来市场收益差。
    """
    market_forward = (
        md.opens.shift(-(horizon + 1)) / md.opens.shift(-1).replace(0, np.nan) - 1.0
    ).mean(axis=1)
    rows = []
    for name in ("limit_up_breadth", "limit_down_breadth", "limit_net_breadth", "limit_heat_z"):
        if name not in signals:
            continue
        joined = pd.concat(
            [signals[name].rename("signal"), market_forward.rename("forward")], axis=1
        ).dropna()
        if len(joined) < 30 or joined["signal"].nunique() < 3:
            continue
        split = max(1, len(joined) // 2)
        ranks = joined["signal"].rank(pct=True)
        spread = joined.loc[ranks >= 0.8, "forward"].mean() - joined.loc[
            ranks <= 0.2, "forward"
        ].mean()
        rows.append(
            {
                "signal": name,
                "rank_ic": rank_correlation(joined["signal"], joined["forward"]),
                "early_rank_ic": rank_correlation(
                    joined["signal"].iloc[:split], joined["forward"].iloc[:split]
                ),
                "late_rank_ic": rank_correlation(
                    joined["signal"].iloc[split:], joined["forward"].iloc[split:]
                ),
                "q80_minus_q20_bp": float(spread) * 10_000,
                "n": len(joined),
            }
        )
    return pd.DataFrame(rows).set_index("signal") if rows else pd.DataFrame()


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="财报 + 涨停事件因子挖掘")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--factor-group",
        default="all",
        choices=["all", "financial", "limit_up", "news", "dragon_tiger"],
    )
    parser.add_argument("--news-events", default="data/events/news.parquet")
    parser.add_argument("--dragon-tiger-events", default="data/events/dragon_tiger.parquet")
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
    if not starts:
        raise SystemExit("可评估交易日不足")
    console.print(f"eval window: {md.dates[starts[0]].date()} ~ {md.dates[starts[-1]].date()} | {len(starts)} points")

    all_factors: dict[str, pd.DataFrame] = {}
    market_signals = pd.DataFrame()

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
        market_signals = market_limit_sentiment(md)

    if args.factor_group in ("all", "news"):
        news_path = Path(args.news_events)
        if news_path.exists():
            news = pd.read_parquet(news_path)
            news_factors = event_sentiment_panels(news, md.dates, md.symbols)
            all_factors.update(news_factors)
            console.print(f"公告/新闻因子: {list(news_factors)}")
        elif args.factor_group == "news":
            console.print(f"[yellow]缺少 {news_path}，无法研究新闻因子[/yellow]")

    if args.factor_group in ("all", "dragon_tiger"):
        dragon_path = Path(args.dragon_tiger_events)
        if dragon_path.exists():
            dragon = pd.read_parquet(dragon_path)
            dragon_factors = dragon_tiger_panels(dragon, md.dates, md.symbols)
            all_factors.update(dragon_factors)
            console.print(f"龙虎榜因子: {list(dragon_factors)}")
        elif args.factor_group == "dragon_tiger":
            console.print(f"[yellow]缺少 {dragon_path}，无法研究龙虎榜因子[/yellow]")

    if not all_factors:
        raise SystemExit("无可评估因子")

    result = evaluate_factors(all_factors, md, starts)
    if result.empty:
        console.print("[yellow]横截面样本不足或因子无有效变化，未产生 RankIC 结果[/yellow]")
    table = Table(
        title=(
            f"新因子挖掘 fwd{HORIZON}d RankIC"
            f"（{md.dates[starts[0]].date()}~{md.dates[starts[-1]].date()}）"
        )
    )
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

    timing_result = evaluate_market_signals(market_signals, md) if not market_signals.empty else pd.DataFrame()
    if not timing_result.empty:
        timing_table = Table(title=f"涨跌停市场情绪时序检验 fwd{HORIZON}d")
        for col in ["signal", "RankIC", "前半", "后半", "Q80-Q20bp", "n"]:
            timing_table.add_column(col, justify="right")
        for name, row in timing_result.iterrows():
            timing_table.add_row(
                name,
                f"{row['rank_ic']:.4f}",
                f"{row['early_rank_ic']:.4f}",
                f"{row['late_rank_ic']:.4f}",
                f"{row['q80_minus_q20_bp']:.0f}",
                str(int(row["n"])),
            )
        console.print(timing_table)

    # 落盘
    out_dir = Path("reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    result.to_csv(out_dir / f"mine_factors_{stamp}.csv", encoding="utf-8-sig")
    if not timing_result.empty:
        timing_result.to_csv(
            out_dir / f"mine_market_sentiment_{stamp}.csv", encoding="utf-8-sig"
        )
    console.print(f"[green]saved: reports/mine_factors_{stamp}.csv[/green]")


if __name__ == "__main__":
    main()

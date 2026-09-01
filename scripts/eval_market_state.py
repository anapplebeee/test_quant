"""RESEARCH-003 方向一评估：市场状态 × 动态因子路由。

用法:
    uv run python scripts/eval_market_state.py

输出:
- 状态分布（全期 / 学习段 2020-2022 / 诊断段 2023+，分别计数）
- 五个代表因子（财报稳定、涨停拥挤、盈利加速、龙虎榜净买衰减、公告情绪衰减）
  在 global / risk_on / transition / risk_off 下的 RankIC 分层表
- OOS 提示：学习段内的分层差异是"在样本内检视"，2023+ 仅允许观察一次。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger
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
from quart.research.market_state import TRANSITION, RISK_OFF, RISK_ON, market_state_vector, state_conditional_ic
from scripts.mine_factors import build_financial_factors

console = Console()

HORIZON = 5
SPLIT = pd.Timestamp("2023-01-01")  # 学习段 / 诊断段分界


def main() -> None:
    cfg = load_config()
    store = BarStore()
    bars = store.load(start="2019-06-01")
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

    # ---- 状态向量（复用 §5.2 市场时序信号） ----
    signals = market_limit_sentiment(md)
    signals["amount"] = md.amounts.sum(axis=1)
    states = market_state_vector(signals, bench_close=md.benchmark_close)
    console.print("\n[bold]状态分布[/bold]")
    for s in (RISK_ON, TRANSITION, RISK_OFF):
        seg = states.loc[states["state"] == s]
        n_learn = (seg.index < SPLIT).sum()
        n_diag = (seg.index >= SPLIT).sum()
        console.print(f"  {s:12s} 全期 {len(seg):5d} | 学习段 {n_learn:4d} | 诊断段 {n_diag:4d}")

    # ---- 因子面板 ----
    starts = list(range(100, len(md.dates) - HORIZON, 5))
    factors: dict[str, pd.DataFrame] = {}
    fin_path = data_root() / "factors" / "financials.parquet"
    if fin_path.exists():
        fin = pd.read_parquet(fin_path)
        factors.update(build_financial_factors(fin, md.close_val))
    factors.update(limit_event_panels(md))
    dragon_path = Path(data_root()) / "events" / "dragon_tiger.parquet"
    if dragon_path.exists():
        dragon = pd.read_parquet(dragon_path)
        factors.update(
            {
                k: v
                for k, v in dragon_tiger_panels(dragon, md.dates, md.symbols).items()
                if k == "dragon_tiger_net_buy_decay"
            }
        )
    news_path = Path(data_root()) / "events" / "news.parquet"
    if news_path.exists():
        news = pd.read_parquet(news_path)
        factors.update(
            {
                k: v
                for k, v in event_sentiment_panels(news, md.dates, md.symbols).items()
                if k == "event_sentiment_decay"
            }
        )

    names = ["roe_stability", "speculative_crowding20_neg", "profit_accel",
             "dragon_tiger_net_buy_decay", "event_sentiment_decay"]
    selected = {k: factors[k] for k in names if k in factors}
    if not selected:
        raise SystemExit("无可用因子面板")

    table = Table(
        title=f"状态条件 RankIC（fwd{HORIZON}d，2020-06~2026-08，同 mine_factors 口径）"
    )
    for col in ["factor", "global_ic", "risk_on_ic", "risk_on_n",
                "transition_ic", "risk_off_ic", "risk_off_n", "gap(on-off)"]:
        table.add_column(col, justify="right")

    result = state_conditional_ic(selected, md, states, starts, horizon=HORIZON)
    for name, r in result.iterrows():
        table.add_row(
            name,
            f"{r['global_ic']:.4f}",
            f"{r['risk_on_ic']:.4f}" if r["risk_on_n"] else "-",
            f"{int(r['risk_on_n'])}",
            f"{r['transition_ic']:.4f}" if r["transition_n"] else "-",
            f"{r['risk_off_ic']:.4f}" if r["risk_off_n"] else "-",
            f"{int(r['risk_off_n'])}",
            f"{r['ic_gap']:+.4f}" if not np.isnan(r.get("ic_gap", np.nan)) else "-",
        )
    console.print(table)
    console.print(
        "\n判读：gap = risk_on_ic − risk_off_ic，|gap| 越大越支持'该因子仅在特定"
        "市场状态下有效'的路由前提。学习段分层差异是样本内检视；诊断段统计仅"
        "允许观察一次，不得据此回改状态规则。"
    )


if __name__ == "__main__":
    logger.remove()
    main()
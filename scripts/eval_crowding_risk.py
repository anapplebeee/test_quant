"""RESEARCH-003 方向四评估：拥挤度风险预警层验证。

用法:
    uv run python scripts/eval_crowding_risk.py

输出:
- 拥挤分位面板覆盖与触发事件数（分年度）
- 预警后 5/20 日超额收益（相对当日全市场等权）：触发组 vs 未触发组，
  以及"坏拥挤"（gap>0.2）vs"好拥挤"（gap<-0.2）子样本
- 自适应阈值标定效果（触发占比应接近 10%）
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
from quart.research.crowding_risk import (
    bad_crowding_gap,
    crowding_indicators,
    crowding_trigger,
    fundamental_view_panel,
)

console = Console()

FORWARD = (5, 20)


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

    console.print("\n[bold]拥挤度指标[/bold]")
    ind = crowding_indicators(md)
    pct = ind["crowding_pctile_60d"]
    console.print(
        f"  拥挤分位面板 {pct.shape[1]} 只 | 非空值占比 "
        f"{pct.notna().to_numpy().mean():.1%}"
    )

    fin_path = data_root() / "factors" / "financials.parquet"
    if not fin_path.exists():
        raise SystemExit("缺少财务数据，无基本面维度")
    fin = pd.read_parquet(fin_path)
    console.print("  构建 PIT 盈利截面分位…")
    fund_all = fundamental_view_panel(fin, md.close_val)
    # 基本面面板列是全市场股票，对齐到当前可交易截面
    fund = fund_all.reindex(index=pct.index, columns=pct.columns)
    gap = bad_crowding_gap(pct, fund)

    console.print("  计算触发事件（自适应阈值 + 加速度为正 + 首次突破）…")
    trig = crowding_trigger(pct)
    tri = trig.sum(axis=1)
    total_events = int(tri.sum())
    console.print(f"  触发事件总数 {total_events} | 平均每日 {tri.mean():.2f} 只 "
                  f"（占当日可交易约 {100.0 * tri.mean() / trig.shape[1]:.1f}%）")
    console.print("\n[bold]触发事件年度分布[/bold]")
    t = Table(show_header=True)
    for col in ["年份", "事件数", "股票数"]:
        t.add_column(col, justify="right")
    for year, g in trig.groupby(trig.index.year):
        ev = int(g.to_numpy().sum())
        n_stocks = int((g > 0).to_numpy().sum())
        t.add_row(str(year), str(ev), str(n_stocks))
    console.print(t)

    # ---- 预警后超额收益（相对当日全市场等权） ----
    opens = md.opens.astype("float64")
    close = md.close_val.astype("float64")
    labels = {}
    for h in FORWARD:
        fwd = opens.shift(-(h + 1)) / opens.shift(-1).replace(0, np.nan) - 1.0
        mkt = fwd.mean(axis=1)
        labels[h] = fwd.sub(mkt, axis=0)

    rows = []
    tri_mask = (tri > 0).to_numpy()[:, None]  # (T,1) 显式按行广播，避免 pandas 对齐歧义
    gap_arr = gap.to_numpy()
    for h in FORWARD:
        lab = labels[h]
        leg = lab.notna().to_numpy()
        lab_arr = lab.to_numpy()
        triggered_ex = np.where(leg & tri_mask, lab_arr, np.nan)
        not_trig_ex = np.where(leg & ~tri_mask, lab_arr, np.nan)
        bad = np.where(leg & tri_mask & (gap_arr > 0.2), lab_arr, np.nan)
        good = np.where(leg & tri_mask & (gap_arr < -0.2), lab_arr, np.nan)

        def _mean(arr: np.ndarray) -> float:
            finite = arr[np.isfinite(arr)]
            return float(finite.mean() * 1e4) if finite.size else np.nan

        rows.append(
            {
                "forward": h,
                "trigger_n": int(np.isfinite(triggered_ex).sum()),
                "trigger_ex_bp": _mean(triggered_ex),
                "notrig_ex_bp": _mean(not_trig_ex),
                "bad_n": int(np.isfinite(bad).sum()),
                "bad_ex_bp": _mean(bad),
                "good_n": int(np.isfinite(good).sum()),
                "good_ex_bp": _mean(good),
            }
        )
    res = pd.DataFrame(rows)
    rt = Table(title="预警后超额收益（相对全市场等权，bp；负值 = 预警有价值）")
    for col in ["forward", "trigger_n", "trigger_ex_bp", "notrig_ex_bp",
                "bad_n", "bad_ex_bp", "good_n", "good_ex_bp"]:
        rt.add_column(col, justify="right")
    for _, r in res.iterrows():
        rt.add_row(
            str(r["forward"]), str(r["trigger_n"]), f"{r['trigger_ex_bp']:.1f}",
            f"{r['notrig_ex_bp']:.1f}", str(r["bad_n"]), f"{r['bad_ex_bp']:.1f}",
            str(r["good_n"]), f"{r['good_ex_bp']:.1f}",
        )
    console.print(rt)
    console.print(
        "\n判读：trigger_ex_bp 显著为负 => 拥挤预警预示回调（预警价值）；"
        "bad（拥挤>基本面）应比 good 更负；2023+ 段仅诊断一次。"
    )


if __name__ == "__main__":
    logger.remove()
    main()
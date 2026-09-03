"""ht_risk_grid.py — ML分数版龙头回测的风控网格(②) + WFA参数一致性诊断(③)。

② 在 ML 分数选龙头回测上加止损(硬止损/移动止损) + 降集中度(max_pos_weight，
   同时可拓宽 n_leaders)，跑参数网格对比 收益/回撤/Sharpe，定位能收敛回撤的稳健配置。

③ WFA 式参数一致性诊断：把样本外区间切成多个相邻滚动子窗口，对同一组参数在
   各子窗口分别回测，看 收益/回撤/Sharpe/换手 是否随窗口移动而漂移（防过拟合到
   单一窗口）；同时对热板块 Top1 与龙头数等"决策自由度"参数做扰动，看指标对
   参数是否"稳健"（在邻域内变化平缓）而非"尖峰依赖"。

数据只需加载一次；对每个配置逐日重放模拟（run() 内部轻量），网格 ~数十个配置
在最坏情况下也是可接受耗时。
"""
from __future__ import annotations

import argparse
import itertools
import json

import numpy as np
import pandas as pd

from quart.config import load_config
from quart.data.store import BarStore
from quart.research.ht_backtest import run, summarize
from quart.research.ht_universe import build_pools


def load_sector_map() -> pd.Series:
    df = pd.read_parquet("data/universe/stat_industry.parquet")
    return df.set_index("symbol")["cluster"].astype(str)


def _metric_grid_row(cfg: dict, res: pd.DataFrame) -> dict:
    s = summarize(res)
    # 换手/覆盖口径：在市场中(持仓>0)的日占比
    n_days = len(res)
    in_mkt = int((res["holdings"].astype(str) != "[]").sum())
    return {
        **cfg,
        "total_ret": round(s.get("total_ret", np.nan), 4),
        "cagr": round(s.get("cagr", np.nan), 4),
        "max_dd": round(s.get("max_drawdown", np.nan), 4),
        "sharpe": round(s.get("sharpe", np.nan), 3),
        "days": s.get("days", np.nan),
        "in_mkt_share": round(in_mkt / max(n_days, 1), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load-start", default="2024-01-01")
    ap.add_argument("--sim-start", default="2025-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--capital", type=float, default=30_000.0)
    ap.add_argument("--scores", default="reports/ht_ml_scores.csv")
    args = ap.parse_args()

    store = BarStore()
    bars = store.load(start=args.load_start, end=args.end)
    tr, pos, st = build_pools(bars, capital=args.capital)
    sector = load_sector_map()
    print("[risk_grid] pool:", st)
    score = pd.read_csv(args.scores)
    score["date"] = pd.to_datetime(score["date"])
    print(f"[risk_grid] bars {bars.shape} score {score.shape}")

    # ---------- ② 风控网格 ----------
    # 基准：现有 ML 配置 (ME, hot_top1, 2 leaders)
    grid = []
    for nl, sl, trs, mpw in itertools.product(
        [2, 3],                       # n_leaders 集中度
        [None, 0.10, 0.15],           # 硬止损
        [None, 0.12, 0.20],           # 移动止损
        [None, 0.35],                 # 单票市值上限（降集中度）
    ):
        if sl is None and trs is None and mpw is None and nl == 2:
            continue  # 避免重复基准，稍后单独跑
        grid.append(dict(n_leaders=nl, stop_loss=sl, trail_stop=trs, max_pos_weight=mpw))
    # 明确基准行
    grid.append(dict(n_leaders=2, stop_loss=None, trail_stop=None, max_pos_weight=None))

    rows = []
    for cfg in grid:
        res = run(bars, pos, sector, score=score, capital=args.capital,
                  freq="ME", hot_rank=1, start=args.sim_start,
                  stop_loss=cfg["stop_loss"], trail_stop=cfg["trail_stop"],
                  max_pos_weight=cfg["max_pos_weight"], n_leaders=cfg["n_leaders"])
        rows.append(_metric_grid_row(cfg, res))
        print(f"[grid] {cfg} -> {rows[-1]}")
    g = pd.DataFrame(rows).sort_values("sharpe", ascending=False, na_position="last")
    g.to_csv("reports/ht_risk_grid.csv", index=False)
    print("\n===== ② 风控网格（按 Sharpe 降序） =====")
    pd.set_option("display.width", 200)
    print(g.to_string(index=False))

    # ---------- ③ WFA 参数一致性 ----------
    # 3 个相邻样本外子窗口覆盖全区间
    starts = ["2025-02-01", "2025-07-01", "2025-12-01"]
    ends = ["2025-08-31", "2026-01-31", "2026-09-01"]
    # 决策自由度参数扰动：hot_rank(取热板块第1/第2) × n_leaders × freq
    perturb = list(itertools.product([1, 2], [2, 3], ["ME", "QE"]))
    wfa = []
    for sr, en in zip(starts, ends):
        # 该子窗口上的"代表性"配置：基准 ML (top1, 2 leaders, ME)
        cfg = dict(hot_rank=1, n_leaders=2, freq="ME",
                   stop_loss=None, trail_stop=None, max_pos_weight=None)
        res = run(bars, pos, sector, score=score, capital=args.capital,
                  freq=cfg["freq"], hot_rank=cfg["hot_rank"], start=sr, end=en)
        wfa.append({"window": f"{sr}~{en}", "cfg": "baseline-ML", **_metric_grid_row(cfg, res)})
    wdf = pd.DataFrame(wfa)
    print("\n===== ③a 跨样本外子窗口的指标漂移(同一基准配置) =====")
    print(wdf[["window", "cfg", "total_ret", "max_dd", "sharpe"]].to_string(index=False))
    # 在"最后一个(最近)子窗口"做参数扰动稳健性：指标对 hot_rank/n_leaders/freq 是否敏感
    last_sr, last_en = starts[-1], ends[-1]
    perturb_rows = []
    for hr, nl, fq in perturb:
        res = run(bars, pos, sector, score=score, capital=args.capital,
                  freq=fq, hot_rank=hr, start=last_sr, end=last_en,
                  n_leaders=nl)
        c = dict(hot_rank=hr, n_leaders=nl, freq=fq,
                 stop_loss=None, trail_stop=None, max_pos_weight=None)
        perturb_rows.append(_metric_grid_row(c, res))
    pv = pd.DataFrame(perturb_rows).sort_values("sharpe", ascending=False, na_position="last")
    pv.to_csv("reports/ht_wfa_perturb.csv", index=False)
    print(f"\n===== ③b 最近子窗口 {last_sr}~{last_en} 参数扰动稳健性(按 Sharpe 降序) =====")
    print(pv[["hot_rank", "n_leaders", "freq", "total_ret", "max_dd", "sharpe"]].to_string(index=False))

    with open("reports/ht_risk_grid_meta.json", "w", encoding="utf-8") as f:
        json.dump({"sim_start": args.sim_start, "end": args.end, "capital": args.capital},
                  f, ensure_ascii=False, indent=2)
    print("\n[risk_grid] saved reports/ht_risk_grid.csv, reports/ht_wfa_perturb.csv")


if __name__ == "__main__":
    main()

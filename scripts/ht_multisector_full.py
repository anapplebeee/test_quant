"""ht_multisector_full.py — 全周期(2025-01~2026-09)对比 单板块 vs 多板块 龙头选股 的最终底数。"""
import pandas as pd
from quart.data.store import BarStore
from quart.research.ht_backtest import run, summarize
from quart.research.ht_universe import build_pools


def load_sector_map():
    df = pd.read_parquet("data/universe/stat_industry.parquet")
    return df.set_index("symbol")["cluster"].astype(str)


def main():
    store = BarStore()
    bars = store.load(start="2024-01-01", end="2026-09-01")
    tr, pos, st = build_pools(bars, capital=30000.0)
    sector = load_sector_map()
    score = pd.read_csv("reports/ht_ml_scores.csv")
    score["date"] = pd.to_datetime(score["date"])
    rows = []
    for name, hot_k in [("single_k1", 1), ("multi_k2", 2), ("multi_k3", 3)]:
        res = run(bars, pos, sector, score=score, capital=30000.0,
                  freq="ME", hot_rank=1, hot_k=hot_k, per_sector=1,
                  n_leaders=3, stop_loss=0.10, start="2025-01-01", end="2026-09-01")
        s = summarize(res)
        rows.append({"mode": name, "hot_k": hot_k, "total_ret": round(s.get("total_ret", float("nan")), 4),
                     "max_dd": round(s.get("max_drawdown", float("nan")), 4),
                     "sharpe": round(s.get("sharpe", float("nan")), 3)})
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 150)
    print("=== 全周期 2025-01~2026-09, 3票+10%硬止损 ===")
    print(df.to_string(index=False))
    # 基准
    b = store.load_benchmark("000852")
    b = b[(b["date"] >= "2025-01-01") & (b["date"] <= "2026-09-01")].sort_values("date")
    print(f"CSI1000 同期: {b['close'].iloc[-1]/b['close'].iloc[0]-1:+.2%}")


if __name__ == "__main__":
    main()

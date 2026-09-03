"""ht_multisector_wfa.py — 跨样本外子窗口对比 单一板块 vs 多板块 龙头选股(关键验证)。
动机: 实验③ 显示单板块策略在最近窗口(2025-12~2026-09)巨亏-45.7%而基准+4.3%。
多板块分散能否在此坏窗口降低尾部损失？收益/回撤整体tradeoff如何？
"""
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

    windows = [("2025-02-01", "2025-08-31"), ("2025-07-01", "2026-01-31"), ("2025-12-01", "2026-09-01")]
    # 配置: 单板块(k1,3票,10%硬止损) vs 多板块(k3,3票,10%硬止损)
    rows = []
    for sr, en in windows:
        for name, hot_k in [("single_k1", 1), ("multi_k3", 3), ("multi_k2", 2)]:
            res = run(bars, pos, sector, score=score, capital=30000.0,
                      freq="ME", hot_rank=1, hot_k=hot_k, per_sector=1,
                      n_leaders=3, stop_loss=0.10, start=sr, end=en)
            s = summarize(res)
            rows.append({"window": f"{sr[:7]}~{en[:7]}", "mode": name, "hot_k": hot_k,
                         "total_ret": round(s.get("total_ret", float("nan")), 4),
                         "max_dd": round(s.get("max_drawdown", float("nan")), 4),
                         "sharpe": round(s.get("sharpe", float("nan")), 3)})
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 150)
    print("=== 单一板块 vs 多板块 跨子窗口(3票, 10%硬止损) ===")
    print(df.to_string(index=False))
    df.to_csv("reports/ht_multisector_wfa.csv", index=False)


if __name__ == "__main__":
    main()

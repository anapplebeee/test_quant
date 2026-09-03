"""重建 list_dates.parquet（基于 BarStore 各股真实首根 bar 日期）。

原因：data/universe/list_dates.parquet 原是坏的占位缓存（仅 60 行、全 2024-01-01），
导致次新过滤把所有主板股都当成"上市<120天"剔除，训练池骤缩。
数据边界：BarStore 全市场自 2024 起，无法区分"2024真上市"与"数据2024才有"，故
first_date 实为"数据首见日"。次新过滤用此近似；真正严格识别需外部上市日数据源。
"""
from __future__ import annotations

import pandas as pd

from quart.config import data_root
from quart.data.store import BarStore


def main() -> None:
    store = BarStore()
    rows = []
    for s in store.symbols():
        fd = store.first_date(s)
        if fd is not None:
            rows.append({"symbol": s, "first_date": pd.Timestamp(fd)})
    df = pd.DataFrame(rows)
    out = data_root() / "universe" / "list_dates.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    MB = ("000", "001", "002", "003", "600", "601", "603", "605")
    mb = df[df["symbol"].str.startswith(MB)]
    print(f"rebuilt list_dates: {len(df)} symbols total, {len(mb)} mainboard")
    print("mainboard first_date by month (tail):")
    print(mb["first_date"].dt.to_period("M").value_counts().sort_index().tail(6).to_string())


if __name__ == "__main__":
    main()

"""共享工具函数"""
from __future__ import annotations

import os
import pandas as pd


def load_stock_names() -> dict[str, str]:
    """获取股票代码-名称映射，优先读缓存"""
    cache_path = os.path.join("data", "stock_names.parquet")
    if os.path.exists(cache_path):
        df = pd.read_parquet(cache_path)
        return dict(zip(df["code"], df["name"]))
    # 缓存不存在则用 akshare 拉取
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        df.to_parquet(cache_path, index=False)
        return dict(zip(df["code"], df["name"]))
    except Exception:
        return {}

"""数据 API - 数据总览相关"""
from __future__ import annotations

import os

import pandas as pd


def get_stock_stats() -> dict:
    """获取股票统计数据"""
    data_root = "data"
    daily_dir = os.path.join(data_root, "daily")
    universe_dir = os.path.join(data_root, "universe")
    index_dir = os.path.join(data_root, "index")
    scores_path = os.path.join(data_root, "scores", "preds.csv")
    
    stats = {
        "stock_count": 0,
        "universe_count": 0,
        "index_count": 0,
        "last_score_date": "N/A",
    }
    
    # 股票数量
    try:
        if os.path.exists(daily_dir):
            stats["stock_count"] = len([f for f in os.listdir(daily_dir) if f.endswith(".parquet")])
    except Exception:
        pass
    
    # 股票池快照
    try:
        if os.path.exists(universe_dir):
            stats["universe_count"] = len([f for f in os.listdir(universe_dir) if f.endswith(".parquet")])
    except Exception:
        pass
    
    # 指数数量
    try:
        if os.path.exists(index_dir):
            stats["index_count"] = len([f for f in os.listdir(index_dir) if f.endswith(".parquet")])
    except Exception:
        pass
    
    # 最新分数日期
    try:
        if os.path.exists(scores_path):
            scores_df = pd.read_csv(scores_path, usecols=["datetime"])
            stats["last_score_date"] = str(scores_df["datetime"].max())[:10]
    except Exception:
        pass
    
    return stats


def get_universe() -> pd.DataFrame:
    """获取最新股票池"""
    universe_dir = os.path.join("data", "universe")
    
    try:
        if os.path.exists(universe_dir):
            files = [f for f in os.listdir(universe_dir) if f.endswith(".parquet")]
            if files:
                latest = sorted(files)[-1]
                df = pd.read_parquet(os.path.join(universe_dir, latest))
                
                # 尝试获取股票名称
                try:
                    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    from common import load_stock_names
                    stock_names = load_stock_names()
                    df["名称"] = df["symbol"].map(stock_names).fillna("-")
                except Exception:
                    pass
                
                return df[["symbol", "名称"]].head(50) if "名称" in df.columns else df.head(50)
    except Exception:
        pass
    
    return pd.DataFrame(columns=["symbol", "名称"])


def get_sample_data() -> pd.DataFrame | None:
    """获取样本数据（平安银行）"""
    sample_file = "data/daily/000001.parquet"
    
    try:
        if os.path.exists(sample_file):
            df = pd.read_parquet(sample_file)
            if "date" in df.columns:
                return df
    except Exception:
        pass
    
    return None

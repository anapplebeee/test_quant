"""回测 API - 回测相关"""
from __future__ import annotations

import glob
import json
import os
import sys

import pandas as pd
from loguru import logger


def _warn(where: str, exc: Exception) -> None:
    logger.warning("backtest_api[{}] degraded: {}", where, exc)


def get_backtest_list() -> list[str]:
    """获取回测列表"""
    reports_dir = "reports"
    summary_files = sorted(glob.glob(os.path.join(reports_dir, "summary_*.json")))
    
    result = []
    for f in summary_files:
        name = os.path.basename(f).replace("summary_", "").replace(".json", "")
        result.append(name)
    
    return result


def get_backtest_summary(name: str) -> dict | None:
    """获取回测摘要"""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from common import safe_path, valid_name

    if not valid_name(name):
        return None
    path = safe_path("reports", f"summary_{name}.json")
    if path is None:
        return None

    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        _warn("get_backtest_summary", e)

    return None


def get_equity_curve(name: str) -> pd.DataFrame | None:
    """获取净值曲线"""
    reports_dir = "reports"
    # 优先查找 sweep_equity 文件，其次查找 equity 文件
    equity_files = sorted(glob.glob(os.path.join(reports_dir, f"sweep_equity_{name}*.csv")))
    if not equity_files:
        equity_files = sorted(glob.glob(os.path.join(reports_dir, f"equity_{name}*.csv")))
    
    try:
        if equity_files:
            return pd.read_csv(equity_files[-1], parse_dates=["date"])
    except Exception as e:
        _warn("get_equity_curve", e)
    
    return None


def get_trades(name: str) -> pd.DataFrame | None:
    """获取交易记录"""
    reports_dir = "reports"
    trade_files = sorted(glob.glob(os.path.join(reports_dir, f"trades_{name}*.csv")))
    
    try:
        if trade_files:
            df = pd.read_csv(trade_files[-1], parse_dates=["date"])
            df = df.sort_values("date", ascending=False)
            
            # 添加股票名称
            try:
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                from common import load_stock_names
                stock_names = load_stock_names()
                df["名称"] = df["symbol"].apply(lambda x: f"{int(x):06d}").map(stock_names).fillna("-")
            except Exception as e:
                _warn("trades_names", e)
                df["名称"] = "-"
            
            # 重命名列
            df = df.rename(columns={"date": "交易日期", "symbol": "代码", "side": "方向",
                                   "shares": "数量", "price": "价格", "amount": "金额", "fee": "手续费"})
            
            return df[["交易日期", "代码", "名称", "方向", "数量", "价格", "金额", "手续费"]]
    except Exception as e:
        _warn("get_trades", e)
    
    return None


def get_cost_breakdown(name: str) -> dict | None:
    """交易成本分解：手续费 / 滑点成本 / 双边换手 / 成本占初始资金比。

    引擎（quart/backtest/engine.py Fees）已含佣金、印花税、过户费、滑点与冲击成本，
    但结果页此前不可见——本函数从 trades 反算各项成本，供回测中心展示。
    """
    reports_dir = "reports"
    trade_files = sorted(glob.glob(os.path.join(reports_dir, f"trades_{name}*.csv")))
    if not trade_files:
        return None
    try:
        df = pd.read_csv(trade_files[-1], parse_dates=["date"], dtype={"symbol": str})
    except Exception as e:
        _warn("get_cost_breakdown", e)
        return None
    if df.empty:
        return None

    total_fee = float(df["fee"].sum())
    turnover_2way = float(df["amount"].sum())
    n_trades = int(len(df))
    n_buy = int((df["side"] == "BUY").sum())
    n_sell = int((df["side"] == "SELL").sum())

    # 滑点成本：成交价 vs 当日开盘价的偏离（买入为正偏离、卖出为负偏离）
    opens_cache: dict[str, pd.DataFrame] = {}
    slip_cost = 0.0
    matched = 0
    for sym, grp in df.groupby("symbol"):
        code = str(sym).zfill(6)
        if code not in opens_cache:
            try:
                bar = pd.read_parquet(os.path.join("data", "daily", f"{code}.parquet"))
                bar["date"] = pd.to_datetime(bar["date"])
                opens_cache[code] = bar.set_index("date")["open"]
            except Exception:
                opens_cache[code] = None
        op = opens_cache[code]
        if op is None:
            continue
        for _, row in grp.iterrows():
            o = op.get(row["date"])
            if o is None or pd.isna(o) or o <= 0:
                continue
            slip_cost += abs(row["price"] - o) * row["shares"]
            matched += 1

    total_cost = total_fee + slip_cost
    # 初始资金：优先取 summary 里的区间首值（缺失时退回 100 万默认）
    init_cash = 1_000_000.0
    try:
        s = get_backtest_summary(name) or {}
        if s.get("initial_cash"):
            init_cash = float(s["initial_cash"])
    except Exception:
        pass

    return {
        "total_fee": total_fee,
        "slip_cost": float(slip_cost),
        "total_cost": float(total_cost),
        "cost_pct_init": float(total_cost / init_cash) if init_cash else 0.0,
        "turnover_2way": turnover_2way,
        "turnover_x": float(turnover_2way / init_cash) if init_cash else 0.0,
        "n_trades": n_trades,
        "n_buy": n_buy,
        "n_sell": n_sell,
        "slip_matched": matched,
    }


def get_sweep_results(name: str) -> pd.DataFrame | None:
    """获取参数扫描结果"""
    reports_dir = "reports"
    sweep_files = sorted(glob.glob(os.path.join(reports_dir, f"sweep_{name}*.csv")))
    
    try:
        if sweep_files:
            return pd.read_csv(sweep_files[-1])
    except Exception as e:
        _warn("get_sweep_results", e)
    
    return None

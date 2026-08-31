"""数据 API - 数据总览相关。

路径统一走 `quart.data.store.BarStore`（分区/旧布局自动识别），
不再直读 `daily_dir()/*.parquet`——存储迁移为 year=YYYY 分区布局后，
旧 per-symbol 直读会静默返回空（2026-08-31 架构检视修复）。
"""
from __future__ import annotations

import json

import pandas as pd

from common import degraded, index_dir, universe_dir
from quart.data.index_catalog import BOARDS


def _last_update_status() -> dict:
    from quart.config import data_root

    path = data_root() / "meta" / "last_data_update.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        degraded("last_data_update", exc)
        return {}


def _count_parquet(directory) -> int:
    if not directory.exists():
        return 0
    return sum(1 for f in directory.glob("*.parquet"))


def _count_partitioned(directory) -> int:
    """兼容分区布局（year=YYYY/*.parquet）与旧平铺布局。"""
    if not directory.exists():
        return 0
    flat = directory.glob("*.parquet")
    partitioned = directory.glob("year=*/*.parquet")
    return sum(1 for _ in flat) + sum(1 for _ in partitioned)


def _bar_store():
    from quart.data.store import BarStore

    return BarStore()


def get_index_coverage() -> pd.DataFrame:
    """指数覆盖明细：按板块分类（上证/深证/创业板/中证/科创/沪深300）。

    2026-08-31 优化：此前只显示"指数数量"总数，无法区分指数归属板块。
    每个指数检查本地文件是否存在 + 最新交易日。
    """
    from quart.data.index_catalog import BOARDS, INDEX_CATALOG

    store = _bar_store()
    codes = [item["code"] for item in INDEX_CATALOG]
    latest_by_symbol: dict[str, str] = {}
    try:
        bars = store.load(symbols=[f"IDX{code}" for code in codes], include_index=True)
        if not bars.empty:
            bars["_date"] = pd.to_datetime(bars["date"])
            for symbol, group in bars.groupby("symbol"):
                latest_by_symbol[str(symbol).removeprefix("IDX")] = (
                    group["_date"].max().date().isoformat()
                )
    except Exception:
        pass

    rows = []
    for item in INDEX_CATALOG:
        latest = latest_by_symbol.get(item["code"])
        rows.append({
            "板块": item["board"],
            "代码": item["code"],
            "名称": item["name"],
            "状态": "✅ 已覆盖" if latest else "⬜ 未拉取",
            "最新交易日": latest or "-",
        })
    frame = pd.DataFrame(rows)
    order = {board: i for i, board in enumerate(BOARDS)}
    if not frame.empty:
        frame["_order"] = frame["板块"].map(order).fillna(99)
        frame = frame.sort_values("_order").drop(columns="_order")
    return frame


def get_stock_stats() -> dict:
    """获取股票统计数据（BarStore 双布局兼容）。

    口径说明（2026-08-31 修复）：`index_count` 是**已覆盖指数个数**（唯一代码数，
    与 stock_count 的"唯一代码数"口径一致），不再是按年分区的指数文件数——
    后者会随时间/指数历史跨度膨胀（上证指数 1990 年起 37 个年份文件），易被误读。
    """
    scores_path = _scores_path()

    stats = {
        "stock_count": 0,
        "latest_date": "N/A",
        "latest_symbols": 0,
        "latest_coverage": 0.0,
        "freshness_days": None,
        "universe_count": 0,
        "index_count": 0,
        "index_file_count": 0,
        "index_boards": {},
        "last_score_date": "N/A",
        "last_update": {},
    }

    try:
        coverage = _bar_store().coverage_summary()
        stats["stock_count"] = coverage["symbols"]
        stats["latest_date"] = coverage["latest_date"] or "N/A"
        stats["latest_symbols"] = coverage["latest_symbols"]
        stats["latest_coverage"] = coverage["latest_coverage"]
        stats["freshness_days"] = coverage["freshness_days"]
    except Exception as e:
        degraded("stock_count", e)

    try:
        stats["universe_count"] = _count_parquet(universe_dir())
    except Exception as e:
        degraded("universe_count", e)

    try:
        stats["index_file_count"] = _count_partitioned(index_dir())
    except Exception as e:
        degraded("index_file_count", e)

    try:
        coverage = get_index_coverage()
        if not coverage.empty:
            covered = coverage[coverage["状态"].str.startswith("✅")]
            stats["index_count"] = len(covered)
            stats["index_boards"] = {
                board: int((covered["板块"] == board).sum())
                for board in BOARDS
            }
    except Exception as e:
        degraded("index_count", e)

    try:
        if scores_path.exists():
            scores_df = pd.read_csv(scores_path, usecols=["datetime"])
            stats["last_score_date"] = str(scores_df["datetime"].max())[:10]
    except Exception as e:
        degraded("last_score_date", e)

    stats["last_update"] = _last_update_status()

    return stats


def get_universe(limit: int = 50) -> pd.DataFrame:
    """获取最新股票池"""
    try:
        files = sorted(universe_dir().glob("*.parquet"))
        if not files:
            return pd.DataFrame(columns=["symbol", "名称"])
        df = pd.read_parquet(files[-1])

        try:
            from common import load_stock_names

            stock_names = load_stock_names()
            df["名称"] = df["symbol"].map(stock_names).fillna("-")
        except Exception as e:
            degraded("universe_names", e)

        return df[["symbol", "名称"]].head(limit) if "名称" in df.columns else df.head(limit)
    except Exception as e:
        degraded("get_universe", e)

    return pd.DataFrame(columns=["symbol", "名称"])


def _read_daily(symbol: str) -> pd.DataFrame | None:
    """个股全史日线（分区/旧布局自动识别）。"""
    try:
        bars = _bar_store().load(symbols=[str(symbol).zfill(6)])
        return bars if not bars.empty else None
    except Exception as e:
        degraded("get_stock_data", e)
        return None


def get_sample_data() -> pd.DataFrame | None:
    """获取样本数据（平安银行）"""
    return _read_daily("000001")


def get_stock_list() -> list[str]:
    """获取所有股票代码列表（双布局）"""
    try:
        return _bar_store().symbols()
    except Exception as e:
        degraded("get_stock_list", e)
        return []


def get_stock_data(symbol: str) -> pd.DataFrame | None:
    """获取指定股票的日线数据"""
    return _read_daily(symbol)


def _scores_path():
    from common import data_dir

    return data_dir() / "scores" / "preds.csv"


# ---------------- UI-001：前端直读整改统一入口 ----------------


def get_freshness() -> int | None:
    """最新 bar 距今天数（None = 无数据/检测失败）。

    UI-001 DR-03：前端不再直接实例化 BarStore。
    """
    try:
        return _bar_store().freshness_days()
    except Exception as exc:
        degraded("get_freshness", exc)
        return None


def get_next_trade_date(as_of) -> str | None:
    """下一交易日（ISO 日期）。UI-001 DR-03：前端不直调交易日历仓储。"""
    try:
        from quart.manual_trading.repository import next_trade_date

        return next_trade_date(as_of)
    except Exception as exc:
        degraded("get_next_trade_date", exc)
        return None


def get_stock_names() -> dict[str, str]:
    """股票代码 → 名称映射。

    UI-001 DR-04/DR-06：统一入口，前端不直接读 common 缓存 parquet。
    """
    try:
        from common import load_stock_names

        return load_stock_names()
    except Exception as exc:
        degraded("get_stock_names", exc)
        return {}


def get_latest_ml_scores(limit: int = 50) -> pd.DataFrame | None:
    """最新 ML 预测分数（按时间倒序取前 N）。

    UI-001 DR-02：前端不直读 data/scores/preds.csv。
    """
    path = _scores_path()
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
        if "datetime" in df.columns:
            df = df.sort_values("datetime", ascending=False)
        return df.head(limit) if len(df) > limit else df
    except Exception as exc:
        degraded("get_latest_ml_scores", exc)
        return None

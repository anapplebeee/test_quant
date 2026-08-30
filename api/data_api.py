"""数据 API - 数据总览相关。

路径统一走 `quart.data.store.BarStore`（分区/旧布局自动识别），
不再直读 `daily_dir()/*.parquet`——存储迁移为 year=YYYY 分区布局后，
旧 per-symbol 直读会静默返回空（2026-08-31 架构检视修复）。
"""
from __future__ import annotations

import pandas as pd

from common import degraded, index_dir, universe_dir

from quart.data.index_catalog import BOARDS


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
    from quart.data.store import BarStore

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
        "universe_count": 0,
        "index_count": 0,
        "index_file_count": 0,
        "index_boards": {},
        "last_score_date": "N/A",
    }

    try:
        stats["stock_count"] = len(_bar_store().symbols())
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
            stats["index_count"] = int(len(covered))
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

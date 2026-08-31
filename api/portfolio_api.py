"""组合风险与归因 API。

前端只负责渲染；账本、行情、制品和行业映射均在 API 层统一读取，避免页面
直接依赖 SQLite/Parquet/ArtifactStore 的物理布局。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import degraded, reports_dir, universe_dir
from quart.data.artifacts import STATUS_OK, ArtifactStore

EXPOSURE_COLUMNS = ["因子", "组合暴露", "持仓等权", "主动暴露"]


def current_holdings() -> tuple[dict[str, int], float]:
    """读取当前手工交易账本持仓和现金。"""
    try:
        from api.manual_trading_api import manual_settings, repository

        _, account_name = manual_settings()
        state = repository().account_state(account_name)
        if state is not None:
            return state.total_positions, float(state.cash_total)
    except Exception as exc:
        degraded("portfolio_api[current_holdings]", exc)
    return {}, 0.0


def holding_price_frame(positions: dict[str, int]) -> pd.DataFrame:
    """返回持仓代码、数量、最新价和市值。"""
    if not positions:
        return pd.DataFrame(columns=["code", "shares", "price", "value"])
    try:
        from api.manual_trading_api import latest_prices

        prices = latest_prices(list(positions))
        return pd.DataFrame([
            {
                "code": symbol,
                "shares": shares,
                "price": float(prices.get(symbol, 0.0)),
                "value": shares * float(prices.get(symbol, 0.0)),
            }
            for symbol, shares in positions.items()
        ])
    except Exception as exc:
        degraded("portfolio_api[holding_price_frame]", exc)
        return pd.DataFrame(columns=["code", "shares", "price", "value"])


def holding_bars(symbols: list[str]) -> pd.DataFrame:
    """通过 BarStore 统一读取持仓行情。"""
    if not symbols:
        return pd.DataFrame()
    try:
        from quart.data.store import BarStore

        return BarStore().load(symbols=[str(symbol).zfill(6) for symbol in symbols])
    except Exception as exc:
        degraded("portfolio_api[holding_bars]", exc)
        return pd.DataFrame()


def portfolio_factor_exposure() -> pd.DataFrame:
    """计算当前组合相对持仓等权基准的可解释行情因子暴露。"""
    positions, _ = current_holdings()
    if not positions:
        return pd.DataFrame(columns=EXPOSURE_COLUMNS)
    bars = holding_bars(list(positions))
    if bars.empty:
        return pd.DataFrame(columns=EXPOSURE_COLUMNS)

    rows = []
    for symbol, shares in positions.items():
        group = bars[bars["symbol"].astype(str) == str(symbol)].sort_values("date")
        if len(group) < 25:
            continue
        close = group["close"].astype(float)
        returns = close.pct_change(fill_method=None)
        rows.append({
            "symbol": str(symbol),
            "shares": shares,
            "mom60": float((1.0 + returns.tail(60)).prod() - 1.0),
            "vol20": float(returns.tail(20).std()),
            "size": float(close.iloc[-1] * shares),
        })
    if not rows:
        return pd.DataFrame(columns=EXPOSURE_COLUMNS)

    frame = pd.DataFrame(rows)
    output = []
    for factor, name in (
        ("mom60", "动量(mom60)"),
        ("vol20", "波动率(vol20)"),
        ("size", "规模(持仓市值)"),
    ):
        values = frame[factor]
        if values.std(ddof=1) < 1e-12 or frame["size"].sum() <= 0:
            continue
        weights = frame["size"] / frame["size"].sum()
        portfolio = float((values * weights).sum())
        equal_weight = float(values.mean())
        output.append({
            "因子": name,
            "组合暴露": round(portfolio, 4),
            "持仓等权": round(equal_weight, 4),
            "主动暴露": round(portfolio - equal_weight, 4),
        })

    for name in ("反转(rev5)", "价值(EP)", "流动性"):
        output.append({
            "因子": name,
            "组合暴露": np.nan,
            "持仓等权": np.nan,
            "主动暴露": np.nan,
        })
    return pd.DataFrame(output, columns=EXPOSURE_COLUMNS)


def _latest_artifact_table(task_prefix: str, name: str) -> pd.DataFrame | None:
    try:
        store = ArtifactStore()
        for manifest in store.list_runs(status=STATUS_OK):
            if not manifest.task.startswith(task_prefix):
                continue
            table = store.read(manifest.run_id, name)
            if table is not None and not table.empty:
                return table
    except Exception as exc:
        degraded(f"portfolio_api[artifact:{task_prefix}:{name}]", exc)
    return None


def _industry_map() -> pd.Series | None:
    for filename in ("sw_industry.parquet", "stat_industry.parquet"):
        path = universe_dir() / filename
        if not path.exists():
            continue
        try:
            frame = pd.read_parquet(path)
            industry_column = "ind1" if "ind1" in frame.columns else "cluster"
            symbols = frame["symbol"].astype(str).str.zfill(6)
            return pd.Series(frame[industry_column].values, index=symbols).groupby(level=0).last()
        except Exception as exc:
            degraded("portfolio_api[industry_map]", exc)
    return None


def latest_industry_trade_summary() -> pd.DataFrame:
    """读取最新回测交易并按行业汇总买卖金额。"""
    trades = _latest_artifact_table("backtest_", "trades")
    if trades is None:
        files = sorted(reports_dir().glob("trades_*.csv"))
        if files:
            try:
                trades = pd.read_csv(files[-1])
            except Exception as exc:
                degraded("portfolio_api[trades_fallback]", exc)
    industries = _industry_map()
    if trades is None or trades.empty or industries is None:
        return pd.DataFrame(columns=["行业", "买入", "卖出"])

    frame = trades.copy()
    frame["symbol"] = frame["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    frame["industry"] = frame["symbol"].map(industries).fillna("未知")
    buy = frame[frame["side"] == "BUY"].groupby("industry")["amount"].sum()
    sell = frame[frame["side"] == "SELL"].groupby("industry")["amount"].sum()
    result = pd.DataFrame({"买入": buy, "卖出": sell}).fillna(0).reset_index()
    return result.rename(columns={"industry": "行业"})


def latest_monthly_returns() -> pd.DataFrame:
    """读取最新净值曲线并返回年份×月份的复利收益百分比矩阵。"""
    equity = _latest_artifact_table("backtest_", "equity")
    if equity is None or "date" not in equity.columns:
        equity = _latest_artifact_table("sweep_", "equity_curves")
    if equity is None or "date" not in equity.columns:
        files = sorted([
            *reports_dir().glob("equity_*.csv"),
            *reports_dir().glob("sweep_equity_*.csv"),
        ])
        if files:
            try:
                equity = pd.read_csv(files[-1])
            except Exception as exc:
                degraded("portfolio_api[equity_fallback]", exc)
    if equity is None or equity.empty or "date" not in equity.columns:
        return pd.DataFrame()

    value_columns = [column for column in equity.columns if column != "date"]
    if not value_columns:
        return pd.DataFrame()
    value_column = "equity" if "equity" in value_columns else value_columns[0]
    frame = equity[["date", value_column]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame[value_column] = pd.to_numeric(frame[value_column], errors="coerce")
    frame = frame.dropna().sort_values("date")
    frame["ret"] = frame[value_column].pct_change(fill_method=None)
    frame["year"] = frame["date"].dt.year
    frame["month"] = frame["date"].dt.month
    return (
        frame.groupby(["year", "month"])["ret"]
        .apply(lambda returns: (1.0 + returns.dropna()).prod() - 1.0)
        .unstack()
        .mul(100)
        .round(1)
    )

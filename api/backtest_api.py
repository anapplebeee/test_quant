"""回测 API - 回测产物读取。

路径一律走 `common.reports_dir()` / `common.daily_dir()`，不再硬编码
"reports" / "data"——此前改 settings.yaml 的 data.root 会让核心库照新路径写、
API 层读空并静默返回空。
"""
from __future__ import annotations

import json

import pandas as pd

from common import degraded, reports_dir, safe_path, valid_name


def _warn(where: str, exc: BaseException) -> None:
    degraded(f"backtest_api[{where}]", exc)


def _latest(pattern: str):
    """按文件名升序取最新一个（文件名含时间戳，升序=时间升序）。"""
    files = sorted(reports_dir().glob(pattern))
    return files[-1] if files else None


def get_backtest_list() -> list[str]:
    """获取回测列表"""
    return sorted(
        p.name[len("summary_"):-len(".json")]
        for p in reports_dir().glob("summary_*.json")
    )


def get_backtest_summary(name: str) -> dict | None:
    """获取回测摘要"""
    if not valid_name(name):
        return None
    path = safe_path(reports_dir(), f"summary_{name}.json")
    if path is None or not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _warn("get_backtest_summary", e)
    return None


def get_equity_curve(name: str) -> pd.DataFrame | None:
    """获取净值曲线"""
    if not valid_name(name):
        return None
    # 优先查找 sweep_equity 文件，其次查找 equity 文件
    path = _latest(f"sweep_equity_{name}*.csv") or _latest(f"equity_{name}*.csv")
    if path is None:
        return None
    try:
        return pd.read_csv(path, parse_dates=["date"])
    except Exception as e:
        _warn("get_equity_curve", e)
    return None


def get_trades(name: str) -> pd.DataFrame | None:
    """获取交易记录"""
    if not valid_name(name):
        return None
    path = _latest(f"trades_{name}*.csv")
    if path is None:
        return None
    try:
        df = pd.read_csv(path, parse_dates=["date"])
        df = df.sort_values("date", ascending=False)

        try:
            from common import load_stock_names

            stock_names = load_stock_names()
            df["名称"] = df["symbol"].apply(lambda x: f"{int(x):06d}").map(stock_names).fillna("-")
        except Exception as e:
            _warn("trades_names", e)
            df["名称"] = "-"

        df = df.rename(columns={"date": "交易日期", "symbol": "代码", "side": "方向",
                                "shares": "数量", "price": "价格", "amount": "金额", "fee": "手续费"})
        return df[["交易日期", "代码", "名称", "方向", "数量", "价格", "金额", "手续费"]]
    except Exception as e:
        _warn("get_trades", e)
    return None


def get_cost_breakdown(name: str) -> dict | None:
    """交易成本分解：手续费 / 滑点成本 / 双边换手 / 成本占初始资金比。

    引擎（quart/execution/fees.py Fees）已含佣金、印花税、过户费、滑点与冲击成本，
    但结果页此前不可见——本函数从 trades 反算各项成本，供回测中心展示。
    """
    if not valid_name(name):
        return None
    path = _latest(f"trades_{name}*.csv")
    if path is None:
        return None
    try:
        df = pd.read_csv(path, parse_dates=["date"], dtype={"symbol": str})
    except Exception as e:
        _warn("get_cost_breakdown", e)
        return None
    if df.empty:
        return None

    total_fee = float(df["fee"].sum())
    turnover_2way = float(df["amount"].sum())

    # 滑点成本：成交价 vs 当日开盘价的偏离（买入为正偏离、卖出为负偏离）
    from quart.data.store import BarStore

    opens_cache: dict[str, pd.Series | None] = {}
    slip_cost = 0.0
    matched = 0
    try:
        bars = BarStore().load(symbols=[str(s).zfill(6) for s in df["symbol"].unique()])
        if not bars.empty:
            bars["date"] = pd.to_datetime(bars["date"])
            for code, grp0 in bars.groupby("symbol"):
                opens_cache[str(code)] = grp0.set_index("date")["open"]
    except Exception:
        pass
    for sym, grp in df.groupby("symbol"):
        code = str(sym).zfill(6)
        op = opens_cache.get(code)
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
        "n_trades": int(len(df)),
        "n_buy": int((df["side"] == "BUY").sum()),
        "n_sell": int((df["side"] == "SELL").sum()),
        "slip_matched": matched,
    }


def get_sweep_results(name: str) -> pd.DataFrame | None:
    """获取参数扫描结果"""
    if not valid_name(name):
        return None
    path = _latest(f"sweep_{name}*.csv")
    if path is None:
        return None
    try:
        return pd.read_csv(path)
    except Exception as e:
        _warn("get_sweep_results", e)
    return None


# ---------------- 区间窗口（近半年/近一年）----------------

_BENCH_CACHE: pd.Series | None = None


def _benchmark_series() -> pd.Series:
    """基准收盘序列（模块级缓存；失败时返回空序列）。"""
    global _BENCH_CACHE
    if _BENCH_CACHE is None:
        try:
            from quart.config import load_config
            from quart.data.store import BarStore

            cfg = load_config()
            b = BarStore().load_benchmark(cfg["benchmark"]).copy()
            b["date"] = pd.to_datetime(b["date"])
            _BENCH_CACHE = b.set_index("date")["close"].astype(float).sort_index()
        except Exception as e:
            _warn("benchmark_series", e)
            _BENCH_CACHE = pd.Series(dtype=float)
    return _BENCH_CACHE


def get_window_stats(name: str) -> dict | None:
    """所选回测的近半年/近一年区间指标（策略 + 基准同期）。

    旧摘要 json 缺少窗口键，此函数从 equity csv 实时补算，与新摘要
    （metrics.summarize 已内置 last_6m/last_1y）同一套口径。
    """
    from quart.backtest.metrics import WINDOWS, max_drawdown, window_stats

    eq_df = get_equity_curve(name)
    if eq_df is None or eq_df.empty or "equity" not in eq_df.columns:
        return None
    try:
        eq = pd.Series(
            eq_df["equity"].astype(float).values,
            index=pd.to_datetime(eq_df["date"]),
        ).dropna()
    except Exception as e:
        _warn("get_window_stats", e)
        return None
    if len(eq) < 2:
        return None

    bench = _benchmark_series()
    out: dict = {}
    for label, days in WINDOWS:
        ws = window_stats(eq, days)
        entry = {
            "days": ws["days"],
            "return": ws["return"],
            "mdd": ws["mdd"],
            "ann_vol": ws["ann_vol"],
            "sharpe": ws["sharpe"],
        }
        # 基准取与策略窗口相同的日历区间，保证同期可比。
        # 用 max(0, ...) 而非负索引：数据量少于窗口时 eq.index[-(days+1)]
        # 会 IndexError（window_stats 内部用 iloc 切片会自动 clamp 到 0，
        # 两者行为必须一致，否则短回测一打开网页就崩）。
        if not bench.empty and ws["return"] is not None and ws["days"] > 0:
            start = eq.index[max(0, len(eq) - (days + 1))]
            end = eq.index[-1]
            bwin = bench[(bench.index >= start) & (bench.index <= end)].dropna()
            if len(bwin) >= 2:
                entry["bench_return"] = float(bwin.iloc[-1] / bwin.iloc[0] - 1.0)
                entry["bench_mdd"] = max_drawdown(bwin)[0]
        out[label] = entry
    return out

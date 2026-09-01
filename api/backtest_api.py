"""回测 API - 回测产物读取。

路径一律走 `common.reports_dir()` / `common.daily_dir()`，不再硬编码
"reports" / "data"——此前改 settings.yaml 的 data.root 会让核心库照新路径写、
API 层读空并静默返回空。
"""
from __future__ import annotations

import json
from contextlib import suppress

import numpy as np
import pandas as pd

# 策略中文名：单一数据源 → frontend/pages 同源；新增策略只需改 REGISTRY + STRATEGY_META。
from api.strategy_api import STRATEGY_META
from common import degraded, reports_dir, safe_path, valid_name
from quart.data.artifacts import ArtifactStore
from quart.strategy.parameters import build_factor_receipt


def _warn(where: str, exc: BaseException) -> None:
    degraded(f"backtest_api[{where}]", exc)


def _latest(pattern: str):
    """按文件名升序取最新一个（文件名含时间戳，升序=时间升序）。"""
    files = sorted(reports_dir().glob(pattern))
    return files[-1] if files else None


def get_backtest_list(limit: int | None = None) -> list[str]:
    """获取回测列表（默认全传；limit>0 时仅返回最近 N 条，用于下拉控件收敛选项）"""
    names = sorted(
        p.name[len("summary_"):-len(".json")]
        for p in reports_dir().glob("summary_*.json")
    )
    if limit and limit > 0:
        names = names[-limit:]
    return names


def scan_summaries(limit: int | None = None) -> pd.DataFrame:
    """扫描全部回测摘要，返回关键指标表格（业界数据卡片/表格模式）。

    列：
      name        — 内部标识（文件名，隐藏不展示，仅作加载 key）
      strategy    — 策略中文名（来自 STRATEGY_META，找不到则回退到英文 key）
      label       — 人类可读标签，如"低波·行业内z · 20260831 10:33"
      run_date    — 日期字符串 YYYY-MM-DD
      run_dt      — 文件时间戳解析出的 datetime（用于可靠排序）
      start / end — 回测区间端点
      CAGR / 夏普 / 最大回撤 / 波动 / 卡玛

    按 run_dt 降序（最新在前），排序完全来自文件名时间戳，不依赖字符串顺序。
    """
    import re

    rows: list[dict] = []
    names = get_backtest_list(limit=limit)
    for name in names:
        p = reports_dir() / f"summary_{name}.json"
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            continue
        # 解析文件名里的策略与时间：summary_<strategy>_<YYYYMMDD>_<HHMMSS>
        m = re.match(r"^(.+)_(20\d{6})_(\d{6})$", name)
        strategy_key = m.group(1) if m else name
        cdate = m.group(2) if m else ""
        ctime = m.group(3) if m else ""
        run_date = f"{cdate[:4]}-{cdate[4:6]}-{cdate[6:8]}" if len(cdate) == 8 else ""
        run_dt = None
        with suppress(Exception):
            run_dt = pd.to_datetime(f"{cdate}{ ctime}", format="%Y%m%d%H%M%S")
        # 策略中文名：来自 STRATEGY_META 的 label 字段；找不到则回退英文 key
        strategy_zh = STRATEGY_META.get(strategy_key, {}).get("label", strategy_key)
        label = f"{strategy_zh} · {cdate} {ctime[:2]}:{ctime[2:4]}" if cdate else name
        rows.append({
            "name": name,
            "strategy": strategy_zh,
            "label": label,
            "run_date": run_date,
            "run_dt": run_dt,
            "区间": f"{s.get('start','?')}" if s.get('end') is None else f"{s.get('start','?')} ~ {s.get('end','?')}",
            "start": s.get("start"),
            "end": s.get("end"),
            "CAGR": s.get("cagr"),
            "夏普": s.get("sharpe"),
            "最大回撤": s.get("max_drawdown"),
            "波动": s.get("annual_vol"),
            "卡玛": s.get("calmar"),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("run_dt", ascending=False, na_position="last").reset_index(drop=True)
    return df


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


def get_benchmark_comparison(name: str) -> pd.DataFrame | None:
    """返回归一化策略、配置基准与超额净值，兼容未固化基准的旧回测。

    新旧回测都以策略首个有效净值日归一到 1。基准仅在真实覆盖日期内参与，
    不向回测开始日前反向填充，避免制造虚假的同期对照。
    """
    frame = get_equity_curve(name)
    if frame is None or frame.empty or "date" not in frame.columns:
        return None
    value_columns = [c for c in frame.columns if c != "date"]
    if not value_columns:
        return None
    value_column = "equity" if "equity" in value_columns else value_columns[0]
    try:
        strategy = pd.Series(
            pd.to_numeric(frame[value_column], errors="coerce").to_numpy(),
            index=pd.to_datetime(frame["date"], errors="coerce"),
            name="strategy",
        ).dropna().sort_index()
    except Exception as exc:
        _warn("benchmark_comparison_strategy", exc)
        return None
    if strategy.empty or float(strategy.iloc[0]) <= 0:
        return None

    benchmark = _benchmark_series().reindex(strategy.index).ffill()
    first_valid = benchmark.first_valid_index()
    if first_valid is None:
        return None
    benchmark = benchmark.loc[first_valid:]
    strategy = strategy.reindex(benchmark.index).dropna()
    benchmark = benchmark.reindex(strategy.index).dropna()
    strategy = strategy.reindex(benchmark.index)
    if len(strategy) < 2 or float(benchmark.iloc[0]) <= 0:
        return None

    strategy_nav = strategy / float(strategy.iloc[0])
    benchmark_nav = benchmark / float(benchmark.iloc[0])
    result = pd.DataFrame({
        "date": strategy_nav.index,
        "strategy_nav": strategy_nav.to_numpy(),
        "benchmark_nav": benchmark_nav.to_numpy(),
    })
    result["excess_nav"] = result["strategy_nav"] / result["benchmark_nav"]
    return result.reset_index(drop=True)


def get_performance_diagnostics(name: str) -> dict | None:
    """从已落盘净值补算页面诊断指标，不要求重新回测。"""
    comparison = get_benchmark_comparison(name)
    if comparison is None or len(comparison) < 3:
        return None

    from quart.backtest.metrics import TRADING_DAYS

    strategy = pd.Series(
        comparison["strategy_nav"].astype(float).to_numpy(),
        index=pd.to_datetime(comparison["date"]),
    )
    benchmark = pd.Series(
        comparison["benchmark_nav"].astype(float).to_numpy(), index=strategy.index
    )
    returns = strategy.pct_change(fill_method=None).dropna()
    benchmark_returns = benchmark.pct_change(fill_method=None).reindex(returns.index)
    excess = (returns - benchmark_returns).dropna()
    if returns.empty:
        return None

    downside = returns.clip(upper=0.0)
    downside_deviation = float(np.sqrt(np.mean(np.square(downside))) * np.sqrt(TRADING_DAYS))
    sortino = None
    if downside_deviation > 1e-12:
        sortino = float(returns.mean() * TRADING_DAYS / downside_deviation)

    tracking_error = None
    information_ratio = None
    if len(excess) > 1 and float(excess.std(ddof=1)) > 1e-12:
        tracking_error = float(excess.std(ddof=1) * np.sqrt(TRADING_DAYS))
        information_ratio = float(excess.mean() / excess.std(ddof=1) * np.sqrt(TRADING_DAYS))

    drawdown = strategy / strategy.cummax() - 1.0
    underwater = drawdown < -1e-12
    groups = (~underwater).cumsum()
    max_drawdown_duration = int(underwater.groupby(groups).sum().max()) if underwater.any() else 0
    current_drawdown_duration = 0
    if bool(underwater.iloc[-1]):
        current_drawdown_duration = int(underwater.groupby(groups).sum().iloc[-1])

    trough = drawdown.idxmin()
    peak = strategy.loc[:trough].idxmax()
    peak_value = float(strategy.loc[peak])
    recovered = strategy.loc[trough:][strategy.loc[trough:] >= peak_value]
    recovery_date = recovered.index[0] if not recovered.empty else None
    peak_pos = int(strategy.index.get_loc(peak))
    recovery_days = (
        int(strategy.index.get_loc(recovery_date)) - peak_pos if recovery_date is not None else None
    )

    q05 = float(returns.quantile(0.05))
    tail = returns[returns <= q05]
    return {
        "sortino": sortino,
        "downside_vol": downside_deviation,
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
        "skew": float(returns.skew()) if len(returns) > 2 else None,
        "kurtosis": float(returns.kurt()) if len(returns) > 3 else None,
        "cvar_95": float(tail.mean()) if not tail.empty else None,
        "worst_day": float(returns.min()),
        "max_drawdown_duration": max_drawdown_duration,
        "current_drawdown_duration": current_drawdown_duration,
        "drawdown_peak_date": str(peak.date()),
        "drawdown_trough_date": str(trough.date()),
        "drawdown_recovery_date": str(recovery_date.date()) if recovery_date is not None else None,
        "drawdown_recovery_days": recovery_days,
    }


def get_execution_assumptions(name: str | None = None) -> dict:
    """返回回测执行口径；新 run 优先读固化值，旧 run 回退当前配置并明确标记。"""
    from quart.config import load_config

    cfg = load_config()
    summary = get_backtest_summary(name) if name else None
    persisted = (summary or {}).get("execution")
    execution = dict(persisted or {})
    bcfg = cfg.get("backtest", {})
    dcfg = cfg.get("data", {})
    execution.setdefault("price_model", "T+1 次日开盘价 + 不利方向滑点")
    execution.setdefault("commission_rate", bcfg.get("commission_rate", 0.0))
    execution.setdefault("commission_min", bcfg.get("commission_min", 0.0))
    execution.setdefault("stamp_tax_rate", bcfg.get("stamp_tax_rate", 0.0))
    execution.setdefault("transfer_fee_rate", bcfg.get("transfer_fee_rate", 0.0))
    execution.setdefault("slippage_rate", bcfg.get("slippage_rate", 0.0))
    execution.setdefault("impact_coef", bcfg.get("impact_coef", 0.0))
    execution.setdefault("impact_model", "base + coef × sqrt(min(order/ADV5, 1))")
    execution.setdefault("price_adjust", dcfg.get("adjust", "unknown"))
    execution.setdefault("lot_size", 100)
    execution.setdefault("limit_rule", "按交易日与板块涨跌停规则拒单")
    execution.setdefault("suspension_rule", "无开盘行情/不可交易时拒单，持仓继续估值")
    execution["source"] = "run" if persisted else "current_config_fallback"
    return execution


def get_factor_execution_receipt(name: str) -> dict | None:
    """读取本次回测固化的因子回执，兼容旧回测的制品参数推断。"""
    if not valid_name(name):
        return None
    summary = get_backtest_summary(name) or {}
    persisted = summary.get("factor_receipt")
    if isinstance(persisted, dict):
        return persisted

    import re

    match = re.match(r"^(.+)_(20\d{6})_(\d{6})$", name)
    strategy = str(summary.get("strategy") or (match.group(1) if match else ""))
    if not strategy:
        return None

    try:
        prefix = f"backtest_{name}_"
        manifests = [
            manifest
            for manifest in ArtifactStore().list_runs(task=f"backtest_{strategy}")
            if manifest.run_id.startswith(prefix)
        ]
        if manifests:
            manifest = manifests[-1]
            return build_factor_receipt(
                strategy,
                manifest.params,
                source="artifact_params_inferred",
            )
        return build_factor_receipt(
            strategy,
            source="current_config_fallback",
        )
    except (KeyError, TypeError, ValueError) as exc:
        _warn("factor_execution_receipt", exc)
        return None


def get_trades(name: str) -> pd.DataFrame | None:
    """获取交易记录"""
    if not valid_name(name):
        return None
    path = _latest(f"trades_{name}*.csv")
    if path is None:
        return None
    try:
        df = pd.read_csv(path, parse_dates=["date"], dtype={"symbol": str})
        df = df.sort_values("date", ascending=False)
        df["symbol"] = (
            df["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        )

        try:
            from common import load_stock_names

            stock_names = load_stock_names()
            df["名称"] = df["symbol"].map(stock_names).fillna("-")
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

    slip_cost = 0.0
    matched = 0
    try:
        syms = [str(s).zfill(6) for s in df["symbol"].unique()]
        min_s = pd.Timestamp(df["date"].min()).strftime("%Y-%m-%d")
        max_s = pd.Timestamp(df["date"].max()).strftime("%Y-%m-%d")
        # 仅取成交涉及个股在成交区间内的 open 列：duckdb 列投影 + symbol IN 下推 +
        # 日期谓词裁剪，替代原 load(symbols=全部成交股) 加载全市场全历史（启动主耗时）。
        bars = BarStore()._query_partitioned(
            start=min_s, end=max_s, include_index=False,
            columns=["date", "symbol", "open"], symbols=syms,
        )
        if not bars.empty:
            bars = bars.copy()
            bars["symbol"] = bars["symbol"].astype(str).str.zfill(6)
            bars["date"] = pd.to_datetime(bars["date"])
            bars = bars[["symbol", "date", "open"]]

            tmp = df.copy()
            tmp["code"] = tmp["symbol"].astype(str).str.zfill(6)
            tmp["date"] = pd.to_datetime(tmp["date"])
            # 向量化关联：逐笔 (symbol,date) 取开盘价算滑点，替代原 Python 双层循环。
            merged = tmp.merge(
                bars, left_on=["code", "date"], right_on=["symbol", "date"], how="left",
            )
            openv = merged["open"]
            # 与原逻辑一致：开盘价缺失 / 非正 的成交不参与滑点累计
            valid = openv.notna() & (openv > 0)
            slip_cost = float(((merged["price"] - openv).abs() * merged["shares"])[valid].sum())
            matched = int(valid.sum())
    except Exception:
        pass

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
        "n_trades": len(df),
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

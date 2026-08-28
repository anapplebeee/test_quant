from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import FLAT, MarketData
from quart.config import PROJECT_ROOT, load_config
from quart.data.store import BarStore
from quart.data.store import drop_incomplete_today
from quart.data.universe import filter_for_simulation
from quart.notify.dingtalk import send_markdown
from quart.risk.rules import check_holdings_risk, validate_weights
from quart.strategy import build_strategy

console = Console()


@dataclass
class OrderPlan:
    symbol: str
    action: str
    shares: int
    ref_price: float
    weight: float = field(default=0.0)


def load_holdings(path: Path | None = None) -> tuple[float, dict[str, int]]:
    p = path or (PROJECT_ROOT / "state" / "holdings.json")
    if not p.exists():
        return 0.0, {}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    cash = float(data.get("cash", 0.0))
    positions = {str(k): int(v) for k, v in data.get("positions", {}).items()}
    return cash, positions


def generate_orders(
    weights: dict[str, float],
    latest_close: pd.Series,
    cash: float,
    positions: dict[str, int],
    force_flat: bool = False,
    warnings: list[str] | None = None,
) -> tuple[list[OrderPlan], float]:
    equity = cash + sum(
        sh * latest_close[sym]
        for sym, sh in positions.items()
        if sym in latest_close.index and not pd.isna(latest_close[sym])
    )
    orders: list[OrderPlan] = []
    if force_flat:
        for sym, held in sorted(positions.items()):
            if held <= 0:
                continue
            price = latest_close.get(sym)
            if pd.isna(price):
                continue
            orders.append(OrderPlan(sym, "SELL", held, round(float(price), 2), 0.0))
        return orders, equity

    # 先卖后买：卖出回款计入可用资金（否则可能给出资金买不起的组合）
    sell_proceeds = 0.0
    for sym, held in sorted(positions.items()):
        price = latest_close.get(sym)
        if pd.isna(price):
            continue
        w = weights.get(sym, 0.0)
        sell = 0
        if w <= 0:
            sell = held
        else:
            delta = w * equity - held * price
            if delta < 0:
                lots = int(abs(delta) // (price * 100))
                sell = min(held, lots * 100)
        if sell > 0:
            orders.append(OrderPlan(sym, "SELL", sell, round(float(price), 2), w))
            sell_proceeds += sell * price

    budget = max(cash + sell_proceeds, 0.0)
    buys: list[OrderPlan] = []
    for sym, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        if w <= 0:
            continue
        price = latest_close.get(sym)
        if pd.isna(price):
            continue
        delta = w * equity - positions.get(sym, 0) * price
        if delta <= 0:
            continue
        if budget <= 0:
            if warnings is not None:
                warnings.append(f"{sym}: 可用资金不足，买入计划被裁剪")
            continue
        affordable = min(delta, budget)
        lots = int(affordable // (price * 100))
        if lots >= 1:
            buys.append(OrderPlan(sym, "BUY", lots * 100, round(float(price), 2), w))
            budget -= lots * 100 * price
        else:
            if warnings is not None:
                warnings.append(f"{sym}: 可用资金不足一手，买入计划被裁剪")
    orders.extend(buys)
    return orders, equity


def render_report(date: pd.Timestamp, strategy_name: str, orders: list[OrderPlan], equity: float, warnings: list[str]) -> str:
    lines = [
        f"# Quart 每日信号 {date.date()}",
        "",
        f"- 策略: **{strategy_name}**",
        f"- 账户估值: **{equity:,.0f} CNY**",
        "- 执行方式: 次日开盘价附近委托，请人工确认后下单",
        "",
    ]
    if warnings:
        lines.append("## 风控提示\n")
        lines += [f"- ⚠️ {w}" for w in warnings]
        lines.append("")
    if orders:
        table = Table(title="交易计划")
        table.add_column("方向")
        table.add_column("代码")
        table.add_column("股数", justify="right")
        table.add_column("参考价", justify="right")
        table.add_column("目标权重", justify="right")
        console_table_rows = []
        for o in sorted(orders, key=lambda x: (x.action != "SELL", -x.weight)):
            color = "[red]SELL[/red]" if o.action == "SELL" else "[green]BUY[/green]"
            table.add_row(color, o.symbol, f"{o.shares}", f"{o.ref_price:.2f}", f"{o.weight:.1%}")
            console_table_rows.append(f"| {o.action} | {o.symbol} | {o.shares} | {o.ref_price:.2f} | {o.weight:.1%} |")
        console.print(table)
        md_lines = ["| 方向 | 代码 | 股数 | 参考价 | 目标权重 |", "|---|---|---|---|---|"] + console_table_rows
        lines.append("## 交易计划\n")
        lines += md_lines
    else:
        lines.append("今日无调仓信号。")
        console.print("[yellow]今日无调仓信号[/yellow]")
    lines += ["", "> 信号由模型自动生成，仅供研究参考，不构成投资建议。"]
    return "\n".join(lines)


def run_daily(strategy_name: str | None = None, push: bool = True, report_dir: Path | None = None) -> str:
    cfg = load_config()
    strategy_name = strategy_name or cfg["strategy"]["name"]
    store = BarStore()
    stale = store.freshness_days()
    if stale is None:
        raise RuntimeError("本地数据为空，请先运行 scripts/update_data.py")
    if stale > 5:
        raise RuntimeError(f"数据已过期 {stale} 天，信号不可信。请先运行 scripts/update_data.py")
    if stale > 2:
        console.print(f"[yellow]警告: 数据落后 {stale} 天[/yellow]")

    bars = store.load(include_index=False)
    # 盘中手动触发时剔除当日未收盘 partial bar，防止信号基于未收盘价（与 updater 同口径）
    bars = drop_incomplete_today(bars)
    bench = drop_incomplete_today(store.load_benchmark(cfg["benchmark"]))
    if bars.empty or bench.empty:
        raise RuntimeError("本地数据为空，请先运行 scripts/update_data.py")

    data_cfg = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )

    md = MarketData.from_bars(bars, benchmark=bench)
    strategy_params = {k: v for k, v in cfg["strategy"].items() if k != "name"}
    strategy = build_strategy(strategy_name, **strategy_params)
    strategy.prepare(md)
    i = len(md.dates) - 1
    raw_weights = strategy.target_weights(i)
    force_flat = FLAT in raw_weights
    raw_weights = {} if force_flat else dict(raw_weights)

    risk_cfg = cfg["risk"]
    cash, positions = load_holdings()
    last_close = md.closes.iloc[i]
    equity = cash + sum(
        sh * last_close[sym]
        for sym, sh in positions.items()
        if sym in last_close.index and not pd.isna(last_close[sym])
    )
    weights, violations = validate_weights(
        raw_weights,
        last_close,
        equity=equity,
        max_position_pct=float(risk_cfg["max_position_pct"]),
    )
    warnings = list(violations)
    orders, equity = generate_orders(weights, last_close, cash, positions, force_flat=force_flat, warnings=warnings)
    if force_flat:
        warnings.append("策略发出择时清仓(FLAT)信号：建议全部卖出")
    warnings += check_holdings_risk(
        positions, last_close, equity, float(risk_cfg["max_position_pct"])
    )

    date = md.dates[i]
    report = render_report(date, strategy_name, orders, equity, warnings)

    out_dir = report_dir or (PROJECT_ROOT / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"signal_{date.strftime('%Y%m%d')}.md"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(report)
    console.print(f"[green]报告已保存: {out_file}[/green]")
    if push:
        send_markdown(f"Quart 每日信号 {date.date()}", report)
    return report

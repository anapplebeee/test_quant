from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.config import PROJECT_ROOT, load_config
from quart.data.market import MarketData
from quart.data.quality_gate import require_quality_gate
from quart.data.store import BarStore, drop_incomplete_today
from quart.data.universe import filter_for_simulation
from quart.execution import (
    BUY,
    FLAT,
    ExecutionContext,
    Fees,
    LiveExecutionModel,
    OrderPlan,
)
from quart.execution import generate_orders as build_rebalance_plan
from quart.execution.constraints import A_SHARE_LOT
from quart.manual_trading import PlannedOrderInput, TradingRepository, next_trade_date
from quart.notify.dingtalk import send_markdown
from quart.risk.daily_loss import DailyLossAssessment, DailyLossGuard
from quart.risk.engine import RiskLimits, RiskState, limits_from_config
from quart.risk.rules import check_holdings_risk, validate_weights
from quart.risk.store import RiskRepository
from quart.strategy import build_strategy

console = Console()


def _apply_daily_loss_guard(
    account_name: str,
    trade_date: str | pd.Timestamp,
    current_equity: float,
    limits: RiskLimits,
    warnings: list[str],
    *,
    repository: RiskRepository | None = None,
) -> tuple[RiskState, DailyLossAssessment | None]:
    """日损检查是每日信号的 fail-closed 前置条件。

    日初基线未知时会显式记录并提示；无法读写风险账本或权益无法估值时按
    ``HALTED`` 处理，避免在风控盲区继续生成新风险订单。
    """
    try:
        assessment = DailyLossGuard(limits, repository or RiskRepository()).evaluate(
            account_name,
            trade_date,
            current_equity,
        )
    except Exception as exc:
        warnings.append(f"日损风险检查失败，按 HALTED 处理（fail-closed）: {exc}")
        return RiskState.HALTED, None

    if not assessment.mark.baseline_available:
        warnings.append(
            "日损基线已初始化：首次接入尚无可审计日初权益，"
            "本日不触发日损；下一交易日将使用本日终权益作为基线"
        )
    elif assessment.triggered:
        warnings.append(f"日损熔断：{assessment.reason}，风险状态已转为 HALTED")
    return assessment.state_after, assessment


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
    fees: Fees | None = None,
    prev_close: pd.Series | None = None,
    sellable_positions: dict[str, int] | None = None,
    trade_date: str | pd.Timestamp | None = None,
) -> tuple[list[OrderPlan], float]:
    """生成次日委托计划。

    与回测共用 `quart.execution.generate_orders`——撮合/整手/资金约束只有
    一份实现。差异仅来自 `LiveExecutionModel`：
      * 参考价用最新收盘（回测用次日开盘）
      * 不做滑点预测（回测中滑点是假设，实盘中它是成交结果）
      * 涨跌停只提示不拒单（次日可能开板）

    Parameters
    ----------
    prev_close:
        **前一交易日**收盘价，用于涨跌停判断与持仓成本估值。
        必须传：若误传当日收盘，涨跌停检测永远不触发
        （今收 == 今收，不可能触及昨收算出的涨跌停价）。
    """
    fees = fees or Fees.from_config()
    targets = {FLAT: 1.0} if force_flat else weights
    prev_close = latest_close if prev_close is None else prev_close
    equity = cash + sum(
        sh * latest_close[sym]
        for sym, sh in positions.items()
        if sym in latest_close.index and not pd.isna(latest_close[sym])
    )

    from quart.execution.rule_resolver import ExecutionRuleResolver

    # 每日信号使用当前已发布主数据；缺失时仍由 RuleBook 处理板块/日期规则，
    # 但不会虚构上市日龄或证券状态。
    rule_resolver = ExecutionRuleResolver(autoload_security_master=True)
    model = LiveExecutionModel(fees, rule_resolver=rule_resolver)
    ctx = ExecutionContext(
        date=pd.Timestamp(trade_date or pd.Timestamp.today()).normalize(),
        targets=targets,
        equity=equity,
        cash=cash,
        positions=positions,
        sellable_positions=sellable_positions,
        mark_prices=latest_close,
        exec_prices=latest_close,
        prev_closes=prev_close,
        fees=fees,
        lot_size=A_SHARE_LOT,
        rule_resolver=rule_resolver,
        # 实盘不留现金垫：委托计划要如实反映目标仓位，由人判断是否留余地
        cash_buffer=1.0,
    )
    plan = build_rebalance_plan(ctx, model)

    if warnings is not None:
        warnings.extend(plan.notes)
        warnings.extend(model.warnings)
        warnings.extend(_unfilled_warnings(targets, plan))
    return plan.orders, equity


def _unfilled_warnings(targets: dict[str, float], plan) -> list[str]:
    """目标里有、但计划里没有的标的必须显式说明，不能静默丢弃。

    资金不足、停牌、涨跌停都会导致"想要但没买到"。若不加提示，
    使用者看到的是一份干干净净的委托单，无从得知策略实际想买什么。
    """
    filled = {o.symbol for o in plan.orders}
    out = []
    for sym in sorted(targets):
        if sym in filled:
            continue
        reason = next((s.blocked_reason for s in plan.skipped if s.symbol == sym), None)
        out.append(f"{sym}: 目标未成交 ({reason or '资金不足或整手约束'})")
    return out


def render_report(
    date: pd.Timestamp,
    strategy_name: str,
    orders: list[OrderPlan],
    equity: float,
    warnings: list[str],
    plan_id: str | None = None,
    intended_trade_date: str | None = None,
) -> str:
    lines = [
        f"# Quart 每日信号 {date.date()}",
        "",
        f"- 策略: **{strategy_name}**",
        f"- 账户估值: **{equity:,.0f} CNY**",
        "- 执行方式: 次日开盘价附近委托，请人工确认后下单",
    ]
    if plan_id:
        lines.append(f"- 交易计划: **{plan_id}** (状态 DRAFT)")
    if intended_trade_date:
        lines.append(f"- 计划交易日: **{intended_trade_date}**")
    lines.append("")
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
        md_lines = [
            "| 方向 | 代码 | 股数 | 参考价 | 目标权重 |",
            "|---|---|---|---|---|",
            *console_table_rows,
        ]
        lines.append("## 交易计划\n")
        lines += md_lines
    else:
        lines.append("今日无调仓信号。")
        console.print("[yellow]今日无调仓信号[/yellow]")
    lines += ["", "> 信号由模型自动生成，仅供研究参考，不构成投资建议。"]
    return "\n".join(lines)


def run_daily(
    strategy_name: str | None = None,
    push: bool = True,
    report_dir: Path | None = None,
    intended_trade_date: str | None = None,
) -> str:
    cfg = load_config()
    strategy_name = strategy_name or cfg["strategy"]["name"]
    live_allowlist = set(cfg.get("strategy", {}).get("live_allowlist") or [])
    if live_allowlist and strategy_name not in live_allowlist:
        raise ValueError(
            f"策略 {strategy_name!r} 未进入实盘信号白名单；"
            f"允许策略: {sorted(live_allowlist)}。请先完成样本外与模拟盘验收。"
        )
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
    # 每日信号会生成待执行交易计划，质量失败必须 fail-closed，而不是只打印扫描报告。
    require_quality_gate(
        bars,
        bench,
        as_of=pd.Timestamp.now().normalize(),
    )

    md = MarketData.from_bars(bars, benchmark=bench)
    strategy = build_strategy(strategy_name)
    strategy.prepare(md)
    i = len(md.dates) - 1
    from quart.market_rules.rule_book import load_rule_book_version

    rule_book_version = load_rule_book_version()

    manual_cfg = cfg.get("manual_trading", {})
    manual_enabled = bool(manual_cfg.get("enabled", True))
    account_name = str(manual_cfg.get("account_name", "manual"))
    db_path = Path(manual_cfg.get("database", PROJECT_ROOT / "state" / "trading.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    repository = TradingRepository(db_path) if manual_enabled else None
    account_state = None
    if repository is not None:
        repository.initialize_schema()
        legacy_path = PROJECT_ROOT / "state" / "holdings.json"
        if (
            bool(manual_cfg.get("auto_migrate_holdings", True))
            and legacy_path.exists()
            and not repository.has_snapshot(account_name)
        ):
            repository.initialize_from_holdings_json(
                legacy_path,
                as_of=str(md.dates[-1].date()),
                account_name=account_name,
            )
        account_state = repository.account_state(account_name, str(md.dates[-1].date()))
    if account_state is not None:
        cash = account_state.cash_available_to_trade
        cash_total = account_state.cash_total
        positions = account_state.total_positions
        sellable_positions = account_state.sellable_positions
    else:
        cash, positions = load_holdings()
        cash_total = cash
        sellable_positions = positions
    strategy.sync_positions(positions)
    raw_weights = strategy.target_weights(i)
    force_flat = FLAT in raw_weights
    raw_weights = {} if force_flat else dict(raw_weights)
    last_close = md.closes.iloc[i]
    equity = cash + sum(
        sh * last_close[sym]
        for sym, sh in positions.items()
        if sym in last_close.index and not pd.isna(last_close[sym])
    )
    # RISK-001：正式信号必须经过 Risk Engine（限额与状态都不可绕过）
    limits = limits_from_config(cfg)
    warnings: list[str] = []
    # 前一交易日收盘：涨跌停判断必须基于它，不能用当日收盘
    # （今收 vs 今收算出的涨跌停价永远不触发）
    prev_close = md.closes.iloc[i - 1] if i > 0 else last_close
    date = md.dates[i]
    trade_date = intended_trade_date or next_trade_date(str(date.date()))
    risk_equity = cash_total + sum(
        sh * last_close[sym]
        for sym, sh in positions.items()
        if sym in last_close.index and not pd.isna(last_close[sym])
    )
    risk_state, daily_loss = _apply_daily_loss_guard(
        account_name,
        date,
        risk_equity,
        limits,
        warnings,
    )
    if risk_state in (RiskState.HALTED, RiskState.RECOVERY):
        weights: dict[str, float] = {}
        orders: list[OrderPlan] = []
        warnings.append(
            f"风险状态 {risk_state.value}：停止生成新订单（撤单与查询不受影响）"
        )
    else:
        weights, violations = validate_weights(
            raw_weights,
            last_close,
            equity=equity,
            max_position_pct=limits.max_position_pct,
        )
        warnings += violations
        orders, equity = generate_orders(
            weights, last_close, cash, positions,
            force_flat=force_flat,
            warnings=warnings,
            prev_close=prev_close,
            sellable_positions=sellable_positions,
            trade_date=trade_date,
        )
        if risk_state is RiskState.REDUCING:
            buys = [o for o in orders if o.side == BUY]
            if buys:
                warnings.append(
                    f"风险状态 REDUCING：剔除 {len(buys)} 笔买入，只保留降风险方向"
                )
                orders = [o for o in orders if o.side != BUY]
    if force_flat:
        warnings.append("策略发出择时清仓(FLAT)信号：建议全部卖出")
    warnings += check_holdings_risk(
        positions, last_close, equity, limits.max_position_pct
    )

    plan_id = None
    if repository is not None:
        account_id = account_state.account_id if account_state is not None else repository.get_or_create_account(account_name)
        if account_state is None:
            warnings.append("手动交易账户尚未完成初始化/对账; 计划基于兼容持仓生成")
        plan_id = repository.create_trade_plan(
            account_id=account_id,
            account_snapshot_id=account_state.snapshot_id if account_state is not None else None,
            strategy_name=strategy_name,
            signal_date=str(date.date()),
            intended_trade_date=trade_date,
            orders=[
                PlannedOrderInput(
                    symbol=order.symbol,
                    side=order.side,
                    strategy_quantity=order.shares,
                    reference_price=order.ref_price,
                    target_weight=order.weight,
                    estimated_fee=order.fee,
                    deferred_quantity=order.deferred_shares,
                )
                for order in orders
            ],
            notes="平台生成, 等待用户在券商端手动确认和执行",
        )
    report = render_report(
        date,
        strategy_name,
        orders,
        equity,
        warnings,
        plan_id=plan_id,
        intended_trade_date=trade_date,
    )

    out_dir = report_dir or (PROJECT_ROOT / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"signal_{date.strftime('%Y%m%d')}.md"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(report)
    console.print(f"[green]报告已保存: {out_file}[/green]")

    # 制品：信号是唯一会真正产生人工下单动作的产出，
    # 必须能回答"这份委托是基于哪天的行情、哪套参数生成的"
    try:
        from quart.data.artifacts import ArtifactStore

        run = ArtifactStore().create_run(
            f"signal_{strategy_name}",
            params={
                "strategy": strategy_name,
                "signal_date": str(date.date()),
                "data_stale_days": stale,
                "n_orders": len(orders),
                "warnings": list(warnings),
                "plan_id": plan_id,
                "intended_trade_date": trade_date,
                "rule_book_version": rule_book_version,
                "risk_state": risk_state.value,
                "daily_loss": daily_loss.to_dict() if daily_loss else None,
            },
        )
        run.put_text("report", report)
        run.put_table("orders", pd.DataFrame([{
            "symbol": o.symbol, "side": o.side, "shares": o.shares,
            "ref_price": o.ref_price, "weight": o.weight,
        } for o in orders]))
        run.put_table("weights", pd.DataFrame(
            [{"symbol": s, "weight": w} for s, w in sorted(weights.items())]
        ))
        run.add_metrics(
            equity=float(equity),
            risk_equity=float(risk_equity),
            daily_loss_pct=(daily_loss.mark.daily_loss_pct if daily_loss else None),
            n_warnings=len(warnings),
        )
        manifest = run.finish()
        if plan_id and repository is not None:
            repository.attach_source_run(plan_id, manifest.run_id)
        console.print(f"[green]制品目录: artifacts/{manifest.run_id}/[/green]")
    except Exception as exc:
        # 制品写失败不应阻断信号推送——推送是主路径
        console.print(f"[yellow]制品写入失败（不影响信号）: {exc}[/yellow]")

    if push:
        send_markdown(f"Quart 每日信号 {date.date()}", report)
    return report

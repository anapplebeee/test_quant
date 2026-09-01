"""执行容量与账户规模压力测试（EXEC-002B）。"""
from __future__ import annotations

from collections.abc import Callable, Iterable

import pandas as pd

from quart.backtest.engine import BacktestEngine
from quart.backtest.metrics import summarize
from quart.data.market import MarketData
from quart.execution.fees import Fees
from quart.execution.price_scenarios import PRICE_MODES
from quart.strategy.base import BaseStrategy


def run_execution_stress(
    md: MarketData,
    strategy_factory: Callable[[], BaseStrategy],
    *,
    fees: Fees,
    initial_cash_values: Iterable[float],
    cost_multipliers: Iterable[float] = (1.0, 2.0),
    execution_price_modes: Iterable[str] = PRICE_MODES,
    risk_pipeline_factory: Callable[[], object] | None = None,
    security_master=None,
    max_adv_participation: float | None = None,
) -> pd.DataFrame:
    """对资本、成本和成交场景做笛卡尔压力测试。

    每格创建新策略、新回测引擎和新风控收集器，杜绝策略状态、待执行意图或
    风控记录跨压力格泄漏。返回表按输入数值排序，适合直接存进 Artifact/CSV。
    """
    cash_values = _positive_grid(initial_cash_values, "initial_cash_values")
    costs = _nonnegative_grid(cost_multipliers, "cost_multipliers")
    modes = _price_modes(execution_price_modes)
    rows: list[dict] = []
    for cash in cash_values:
        for multiplier in costs:
            for mode in modes:
                engine = BacktestEngine(
                    md,
                    strategy_factory(),
                    fees=fees.scaled(multiplier),
                    initial_cash=cash,
                    risk_pipeline=(risk_pipeline_factory() if risk_pipeline_factory is not None else None),
                    security_master=security_master,
                    max_adv_participation=max_adv_participation,
                    execution_price_mode=mode,
                )
                result = engine.run_result()
                metrics = summarize(result.equity)
                trades = result.trades
                rows.append({
                    "initial_cash": cash,
                    "cost_multiplier": multiplier,
                    "execution_price_mode": result.execution_price_mode,
                    "cagr": metrics.get("cagr"),
                    "sharpe": metrics.get("sharpe"),
                    "max_drawdown": metrics.get("max_drawdown"),
                    "total_return": metrics.get("total_return"),
                    "n_trades": len(trades),
                    "trade_notional": float(trades["amount"].sum()) if not trades.empty else 0.0,
                    "n_deferred_orders": len(result.deferred_orders),
                    "execution_price_fallbacks": result.execution_price_fallbacks,
                    "ending_equity": float(result.equity.iloc[-1]),
                })
    return pd.DataFrame(rows).sort_values(
        ["initial_cash", "cost_multiplier", "execution_price_mode"]
    ).reset_index(drop=True)


def _positive_grid(values: Iterable[float], name: str) -> list[float]:
    parsed = sorted({float(value) for value in values})
    if not parsed or any(value <= 0 for value in parsed):
        raise ValueError(f"{name} 必须包含至少一个正数")
    return parsed


def _nonnegative_grid(values: Iterable[float], name: str) -> list[float]:
    parsed = sorted({float(value) for value in values})
    if not parsed or any(value < 0 for value in parsed):
        raise ValueError(f"{name} 必须包含至少一个非负数")
    return parsed


def _price_modes(values: Iterable[str]) -> list[str]:
    parsed = sorted({str(value).strip().lower() for value in values})
    invalid = sorted(set(parsed) - set(PRICE_MODES))
    if not parsed or invalid:
        raise ValueError(f"execution_price_modes 仅支持 {list(PRICE_MODES)}，收到 {invalid or parsed}")
    return parsed


__all__ = ["run_execution_stress"]

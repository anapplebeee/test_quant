from __future__ import annotations

import pandas as pd


def validate_weights(
    weights: dict[str, float],
    latest_close: pd.Series,
    equity: float,
    max_position_pct: float,
) -> tuple[dict[str, float], list[str]]:
    violations: list[str] = []
    clean: dict[str, float] = {}
    total = 0.0
    for sym, w in sorted(weights.items()):
        if w <= 0:
            continue
        if sym not in latest_close.index or pd.isna(latest_close[sym]):
            violations.append(f"{sym}: 无最新价格，剔除")
            continue
        if w > max_position_pct:
            violations.append(f"{sym}: 权重 {w:.1%} 超过单票上限 {max_position_pct:.1%}，已截断")
            w = max_position_pct
        clean[sym] = w
        total += w
    if total > 1.0:
        scale = 1.0 / total
        clean = {s: w * scale for s, w in clean.items()}
        violations.append(f"总权重 {total:.1%} 超限，已等比缩放至 100%")
    return clean, violations


def check_holdings_risk(
    positions: dict[str, int],
    latest_close: pd.Series,
    equity: float,
    max_position_pct: float,
) -> list[str]:
    warnings: list[str] = []
    for sym, shares in positions.items():
        value = shares * latest_close.get(sym, float("nan"))
        if pd.isna(value):
            warnings.append(f"{sym}: 持仓无最新价格，无法估值")
            continue
        pct = value / equity if equity > 0 else 0
        if pct > max_position_pct:
            warnings.append(f"{sym}: 当前持仓占比 {pct:.1%} 超过上限 {max_position_pct:.1%}")
    return warnings


def make_weight_validator(max_position_pct: float, collect: list[str] | None = None):
    """构造回测可用的风控钩子（签名匹配 BacktestEngine.risk_pipeline）。

    风控此前**只在实盘路径生效**——回测跑出的组合可以违反单票上限，
    实盘才被截断，导致回测组合 ≠ 实盘组合。用本函数造出的钩子注入
    `BacktestEngine(risk_pipeline=...)`，两条路径就受同一约束。

    Parameters
    ----------
    max_position_pct:
        单票权重上限（建议用 `risk.max_position_pct` 配置值，
        而非 `strategy.max_weight_pct`——后者是策略内部的分散度参数）。
    collect:
        可选列表，用于收集每条违规记录（供回测报告汇总）。

    Example
    -------
    >>> from quart.config import load_config
    >>> from quart.risk.rules import make_weight_validator
    >>> cfg = load_config()
    >>> violations: list[str] = []
    >>> engine = BacktestEngine(
    ...     md, strategy,
    ...     risk_pipeline=make_weight_validator(
    ...         cfg["risk"]["max_position_pct"], collect=violations),
    ... )
    """
    def _validator(targets: dict[str, float], prices: pd.Series, equity: float) -> dict[str, float]:
        clean, violations = validate_weights(targets, prices, equity, max_position_pct)
        if collect is not None:
            collect.extend(violations)
        return clean

    return _validator

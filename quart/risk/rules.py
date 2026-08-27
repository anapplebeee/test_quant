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

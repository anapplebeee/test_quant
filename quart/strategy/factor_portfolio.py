"""因子分数直通独立 Portfolio Constructor 的策略。

``FactorPortfolioStrategy`` 的职责止于构建 PIT 横截面 alpha；持仓权重、单票
上限、现金比例和风险目标均委托给 ``quart.portfolio``。这条链路是因子研究
从候选分数走向可回测组合的最小正式实现，禁止退回策略内部等权 Top-K。
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.portfolio import (
    PortfolioConstraints,
    PortfolioConstructionInput,
    PortfolioConstructionResult,
    PortfolioConstructor,
)
from quart.research.factor_audit import FACTOR_SPECS, FactorInputs
from quart.strategy.base import BaseStrategy
from quart.strategy.filters import apply_liquidity


class FactorPortfolioStrategy(BaseStrategy):
    """把一组技术/事件/基本面因子合成为 alpha，再交给 Constructor。

    ``factor_names`` 使用逗号分隔的 ``FACTOR_SPECS`` 名称。每个因子在每个交易
    日做横截面 z-score，再等权合成；任一被请求因子在运行期无数据即明确失败，
    防止研究配置悄悄少了一个因子还继续出结果。
    """

    name = "factor_portfolio"
    required_history_days = 61

    PARAMS_SCHEMA = {
        "factor_names": (str, "vol20_neg,amp20_neg,lottery20_neg", "逗号分隔的研究因子名"),
        "top_k": (int, 10, "候选因子分数前 K 名"),
        "rebalance_days": (int, 20, "调仓周期（交易日）"),
        "max_weight_pct": (float, 0.10, "Constructor 单票硬上限"),
        "min_cash_weight": (float, 0.0, "Constructor 最低现金比例"),
        "min_avg_amount": ((int, float, type(None)), None, "流动性门槛"),
        "liquidity_days": (int, 20, "流动性回看窗口"),
        "min_price": ((int, float, type(None)), None, "最低价过滤"),
        "risk_aversion": (float, 0.0, "协方差风险惩罚（>0 启用60日样本协方差）"),
        "turnover_penalty": (float, 0.0, "目标函数换手成本惩罚"),
        "transaction_cost_bps": (float, 0.0, "目标函数预估双边成本（bps）"),
    }

    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        p = self.params
        self.factor_names = _parse_factor_names(str(p.get("factor_names", "")))
        self.top_k = int(p.get("top_k", 10))
        self.rebalance_days = int(p.get("rebalance_days", 20))
        self.max_weight = float(p.get("max_weight_pct", 0.10))
        self.min_cash_weight = float(p.get("min_cash_weight", 0.0))
        self.min_avg_amount = p.get("min_avg_amount")
        self.liquidity_days = int(p.get("liquidity_days", 20))
        self.min_price = p.get("min_price")
        self.risk_aversion = float(p.get("risk_aversion", 0.0))
        self.turnover_penalty = float(p.get("turnover_penalty", 0.0))
        self.transaction_cost_bps = float(p.get("transaction_cost_bps", 0.0))
        _validate_params(self)

        inputs = FactorInputs(md)
        panels: dict[str, pd.DataFrame] = {}
        for name in self.factor_names:
            panel = inputs.compute(name)
            if panel is None:
                raise RuntimeError(
                    f"factor_portfolio 请求的因子 {name!r} 在当前 PIT 数据中不可用；"
                    "请补齐数据或从配置中移除该因子"
                )
            panels[name] = panel.reindex(index=md.dates, columns=md.symbols)
        self.factor_panels = panels
        self.alpha_panel = _combine_panels(panels)
        self.returns = md.close_val.pct_change(fill_method=None)
        self._next_rebalance = 0
        self.last_construction: PortfolioConstructionResult | None = None

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._require_md()
        if i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days

        scores = self.alpha_panel.iloc[i].dropna()
        tradable = md.volumes.iloc[i]
        scores = scores.loc[scores.index.intersection(tradable[tradable.fillna(0) > 0].index)]
        scores = apply_liquidity(
            scores, md, i, self.min_avg_amount, self.liquidity_days, self.min_price,
        )
        if scores.empty:
            return {}

        candidates = scores.nlargest(self.top_k)
        covariance = self._covariance_for(i, candidates.index)
        request = PortfolioConstructionInput(
            alphas=candidates,
            # 当前权重、ADV 和不可交易持仓由后续组合运行时上下文接入；本策略
            # 只交付 alpha → 目标权重，不得自行等权。
            current_weights={},
            equity=1.0,
            tradable=candidates.index,
            covariance=covariance,
            risk_aversion=self.risk_aversion,
            turnover_penalty=self.turnover_penalty,
            transaction_cost_bps=self.transaction_cost_bps,
            reason=f"factor_portfolio:{','.join(self.factor_names)}",
        )
        constraints = PortfolioConstraints(
            max_weight=self.max_weight,
            min_cash_weight=self.min_cash_weight,
        )
        self.last_construction = PortfolioConstructor().construct(request, constraints)
        return {
            symbol: float(weight)
            for symbol, weight in self.last_construction.target_weights.items()
        }

    def construction_receipt(self) -> dict | None:
        """供回测 Artifact/前端展示的最新 Constructor 回执。"""
        result = self.last_construction
        if result is None:
            return None
        return {
            "strategy": self.name,
            "reason": result.reason,
            "selected_symbols": list(result.selected_symbols),
            "target_weights": {key: float(value) for key, value in result.target_weights.items()},
            "cash_weight": result.cash_weight,
            "expected_alpha": result.expected_alpha,
            "expected_variance": result.expected_variance,
            "expected_turnover": result.expected_turnover,
            "expected_cost": result.expected_cost,
            "objective_value": result.objective_value,
            "constraint_usage": {
                key: {
                    "used": usage.used,
                    "limit": usage.limit,
                    "headroom": usage.headroom,
                    "detail": usage.detail,
                }
                for key, usage in result.constraint_usage.items()
            },
        }

    def state_dict(self) -> dict:
        return {"next_rebalance": int(self._next_rebalance)}

    def load_state_dict(self, state: Mapping | None) -> None:
        super().load_state_dict(state)
        if state and "next_rebalance" in state:
            self._next_rebalance = int(state["next_rebalance"])

    def _covariance_for(self, i: int, symbols: pd.Index) -> pd.DataFrame | None:
        if self.risk_aversion <= 0:
            return None
        history = self.returns.iloc[max(0, i - 59) : i + 1].loc[:, symbols].dropna(how="any")
        if len(history) < 20:
            raise RuntimeError("factor_portfolio 风险厌恶已启用，但可用协方差历史少于 20 日")
        covariance = history.cov().reindex(index=symbols, columns=symbols)
        if not np.isfinite(covariance.to_numpy()).all():
            raise RuntimeError("factor_portfolio 无法得到完整有限的协方差矩阵")
        return covariance


def _parse_factor_names(raw: str) -> tuple[str, ...]:
    names = tuple(name.strip() for name in raw.split(",") if name.strip())
    available = {spec.name for spec in FACTOR_SPECS}
    unknown = sorted(set(names) - available)
    if not names:
        raise ValueError("factor_names 不能为空")
    if unknown:
        raise ValueError(f"未知研究因子: {unknown}")
    return names


def _combine_panels(panels: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    standardized: list[pd.DataFrame] = []
    for panel in panels.values():
        mean = panel.mean(axis=1)
        std = panel.std(axis=1).replace(0, np.nan)
        standardized.append(panel.sub(mean, axis=0).div(std, axis=0))
    combined = sum(standardized) / len(standardized)
    return combined.replace([np.inf, -np.inf], np.nan).astype("float64")


def _validate_params(strategy: FactorPortfolioStrategy) -> None:
    if strategy.top_k < 1:
        raise ValueError("top_k 必须为正整数")
    if strategy.rebalance_days < 1:
        raise ValueError("rebalance_days 必须为正整数")
    if not 0 < strategy.max_weight <= 1:
        raise ValueError("max_weight_pct 必须在 (0, 1]")
    if not 0 <= strategy.min_cash_weight < 1:
        raise ValueError("min_cash_weight 必须在 [0, 1)")
    if strategy.risk_aversion < 0 or strategy.turnover_penalty < 0:
        raise ValueError("risk_aversion 与 turnover_penalty 不能为负")
    if strategy.transaction_cost_bps < 0:
        raise ValueError("transaction_cost_bps 不能为负")


__all__ = ["FactorPortfolioStrategy"]

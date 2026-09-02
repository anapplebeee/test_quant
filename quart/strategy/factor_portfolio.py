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
    PortfolioConstructionContext,
    PortfolioConstructionInput,
    PortfolioConstructionResult,
    PortfolioConstructor,
)
from quart.research.factor_audit import FACTOR_SPECS, FactorInputs
from quart.risk.exposure import ExposureLimits, parse_style_bounds
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
        "max_turnover": ((float, type(None)), None, "Constructor 单次换手硬上限"),
        "industry_active_bound": ((float, type(None)), None, "相对基准的行业主动权重上限"),
        "market_cap_active_bound": ((float, type(None)), None, "相对基准的市值 log-z 主动暴露上限"),
        "style_active_bounds": (str, "", "风格主动暴露上限，如 momentum=0.15,size=0.2"),
        "turnover_penalty": (float, 0.0, "目标函数换手成本惩罚"),
        "transaction_cost_bps": (float, 0.0, "目标函数预估双边成本（bps）"),
        "rank_combine": (
            bool, False,
            "因子合成用横截面 rank 百分位等权而非 z-score 等权。拥挤度/反转类因子 "
            "大量并列且厚尾，z-score 会把并列段噪声放大导致选股抖动；RESEARCH-009 "
            "校准实证 rank 合成能复现 composite3 的 +15% 正 alpha（见 "
            "scripts/composite_backtest.py 注释）",
        ),
    }

    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        p = self.params
        self.factor_names = _parse_factor_names(
            str(p.get("factor_names", "vol20_neg,amp20_neg,lottery20_neg"))
        )
        self.top_k = int(p.get("top_k", 10))
        self.rebalance_days = int(p.get("rebalance_days", 20))
        self.max_weight = float(p.get("max_weight_pct", 0.10))
        self.min_cash_weight = float(p.get("min_cash_weight", 0.0))
        self.min_avg_amount = p.get("min_avg_amount")
        self.liquidity_days = int(p.get("liquidity_days", 20))
        self.min_price = p.get("min_price")
        self.risk_aversion = float(p.get("risk_aversion", 0.0))
        self.max_turnover = p.get("max_turnover")
        self.industry_active_bound = p.get("industry_active_bound")
        self.market_cap_active_bound = p.get("market_cap_active_bound")
        self.style_active_bounds = parse_style_bounds(p.get("style_active_bounds"))
        self.exposure_limits = ExposureLimits(
            industry_active_bounds=self.industry_active_bound,
            market_cap_active_bound=self.market_cap_active_bound,
            style_active_bounds=self.style_active_bounds,
        )
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
        self.rank_combine = bool(p.get("rank_combine", False))
        self.alpha_panel = _combine_ranks(panels) if self.rank_combine else _combine_panels(panels)
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
        # 防御：退市/停牌/无行情标的即使残留 alpha 面板值（如数据源写入
        # 退市股"幽灵行情"）也不得进入候选，避免 Constructor 对不可估值标的
        # 建仓或触发 Infeasible。
        prices = md.close_val.iloc[i].reindex(scores.index)
        scores = scores[prices.fillna(0) > 0]
        scores = apply_liquidity(
            scores, md, i, self.min_avg_amount, self.liquidity_days, self.min_price,
        )
        if scores.empty:
            return {}

        candidates = scores.nlargest(self.top_k)
        context = getattr(self, "_portfolio_context", None)
        if context is not None and not isinstance(context, PortfolioConstructionContext):
            raise TypeError("factor_portfolio 收到无效 PortfolioConstructionContext")
        tradable_symbols = candidates.index if context is None else context.tradable
        covariance = self._covariance_for(i, candidates.index)
        exposure_inputs = self._resolve_exposures(context, candidates.index)
        request = PortfolioConstructionInput(
            alphas=candidates,
            current_weights={} if context is None else context.current_weights,
            benchmark_weights=(
                {} if exposure_inputs is None else exposure_inputs.benchmark_weights
            ),
            equity=1.0 if context is None else context.equity,
            tradable=tradable_symbols,
            covariance=covariance,
            adv=None if context is None else context.adv,
            industries=None if exposure_inputs is None else exposure_inputs.industries,
            market_caps=None if exposure_inputs is None else exposure_inputs.market_caps,
            style_exposures=None if exposure_inputs is None else exposure_inputs.style_exposures,
            risk_aversion=self.risk_aversion,
            turnover_penalty=self.turnover_penalty,
            transaction_cost_bps=self.transaction_cost_bps,
            reason=f"factor_portfolio:{','.join(self.factor_names)}",
        )
        constraints = PortfolioConstraints(
            max_weight=self.max_weight,
            min_cash_weight=self.min_cash_weight,
            max_turnover=self.max_turnover,
            max_adv_participation=(
                None if context is None else context.max_adv_participation
            ),
            industry_active_bounds=self.exposure_limits.industry_active_bounds,
            market_cap_active_bound=self.exposure_limits.market_cap_active_bound,
            style_active_bounds=self.exposure_limits.style_active_bounds,
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

    def _resolve_exposures(self, context, candidates: pd.Index):
        if not self.exposure_limits.enabled:
            return None
        if context is None or context.exposure_snapshot is None:
            raise RuntimeError(
                "factor_portfolio 已启用行业/市值/风格暴露约束，但没有 PIT ExposureSnapshot"
            )
        symbols = candidates.union(context.current_weights.index)
        return context.exposure_snapshot.resolve(
            context.date, symbols, self.exposure_limits,
        )


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


def _combine_ranks(panels: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """横截面 rank 百分位（0~1）等权合成（RESEARCH-009 校准）。

    与 z-score 合成不同：rank 对厚尾与大量并列不敏感（拥挤度计数类因子如 20 日
    0 次涨停极多并列，z-score 会把并列段微小噪声放大导致选股在并列股间抖动、
    换手爆炸）。这与 ``scripts/composite_backtest.py`` 的
    ``cross_sectional_rank`` 口径一致——实证该口径能复现 composite3 的 +15% CAGR。
    """
    ranked: list[pd.DataFrame] = []
    for panel in panels.values():
        ranked.append(panel.rank(axis=1, pct=True, method="average"))
    combined = sum(ranked) / len(ranked)
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
    if strategy.max_turnover is not None and not 0 <= float(strategy.max_turnover) <= 1:
        raise ValueError("max_turnover 必须在 [0, 1]")
    if strategy.industry_active_bound is not None and float(strategy.industry_active_bound) < 0:
        raise ValueError("industry_active_bound 不能为负")
    if strategy.market_cap_active_bound is not None and float(strategy.market_cap_active_bound) < 0:
        raise ValueError("market_cap_active_bound 不能为负")
    if strategy.transaction_cost_bps < 0:
        raise ValueError("transaction_cost_bps 不能为负")


__all__ = ["FactorPortfolioStrategy"]

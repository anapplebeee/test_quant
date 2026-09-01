"""确定性的长多组合构建器（PORTFOLIO-001 第一版）。

本模块把策略 alpha 与最终权重彻底分开。它不是隐藏在某个策略中的
``Top-K 等权``，而是显式接收基准、当前权重、行业/风格/市值暴露、流动性
和风险限额，并返回可审计的构建结果。

第一版有意不引入外部凸优化器：使用按 alpha 排序的确定性贪心分配，再对
换手和 ADV 做严格裁剪。所有约束在输出前重新验证；若无法同时满足，抛出
``PortfolioInfeasibleError``，绝不静默回退为等权组合。

约束口径
--------
* 行业：相对基准行业权重的绝对主动偏离；
* 市值：相对基准的市值对数 z-score 主动暴露；
* 风格：相对基准的输入风格暴露主动暴露；
* 换手：``0.5 * sum(abs(target - current))``；
* ADV：单标的目标权重变化的名义金额不超过 ``ADV × participation``。
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

_EPS = 1e-10


class PortfolioInfeasibleError(ValueError):
    """组合约束不能同时满足。

    ``reasons`` 是面向 Artifact/前端的稳定、可枚举原因，调用方不能把该异常
    吞掉并改为策略内部的 Top-K 或等权结果。
    """

    def __init__(self, reasons: Collection[str]):
        self.reasons = tuple(str(reason) for reason in reasons)
        message = "组合构建不可行：" + "；".join(self.reasons)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PortfolioConstraints:
    """长多组合的硬约束。未提供的暴露数据绝不被悄悄忽略。"""

    max_weight: float
    min_cash_weight: float = 0.0
    max_turnover: float | None = None
    max_adv_participation: float | None = None
    industry_active_bounds: float | Mapping[str, float] | None = None
    market_cap_active_bound: float | None = None
    style_active_bounds: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 < float(self.max_weight) <= 1:
            raise ValueError("max_weight 必须在 (0, 1]")
        if not 0 <= float(self.min_cash_weight) < 1:
            raise ValueError("min_cash_weight 必须在 [0, 1)")
        if self.max_turnover is not None and not 0 <= float(self.max_turnover) <= 1:
            raise ValueError("max_turnover 必须在 [0, 1]")
        if self.max_adv_participation is not None and not 0 < float(self.max_adv_participation) <= 1:
            raise ValueError("max_adv_participation 必须在 (0, 1]")
        _validate_bound_spec("industry_active_bounds", self.industry_active_bounds)
        if self.market_cap_active_bound is not None and float(self.market_cap_active_bound) < 0:
            raise ValueError("market_cap_active_bound 不能为负")
        for factor, bound in self.style_active_bounds.items():
            if not str(factor).strip() or float(bound) < 0:
                raise ValueError("style_active_bounds 的因子名不能为空且上限不能为负")


@dataclass(frozen=True, slots=True)
class PortfolioConstructionInput:
    """Constructor 的统一输入合同。

    ``market_caps`` 为正的原始市值；Constructor 在可用股票全集上做 log-z
    标准化，因此市值主动暴露是可比较、确定性的。风格暴露的行是股票、列是
    风格因子，且应使用 PIT 数据。
    """

    alphas: pd.Series
    current_weights: Mapping[str, float] | pd.Series = field(default_factory=dict)
    benchmark_weights: Mapping[str, float] | pd.Series = field(default_factory=dict)
    equity: float = 1.0
    tradable: Collection[str] | None = None
    industries: Mapping[str, str] | pd.Series | None = None
    market_caps: Mapping[str, float] | pd.Series | None = None
    style_exposures: pd.DataFrame | None = None
    covariance: pd.DataFrame | None = None
    adv: Mapping[str, float] | pd.Series | None = None
    transaction_cost_bps: float = 0.0
    risk_aversion: float = 0.0
    turnover_penalty: float = 0.0
    reason: str = "alpha_score"


@dataclass(frozen=True, slots=True)
class ConstraintUsage:
    """一条已验证约束的使用量，供结果页和 Artifact 直接展示。"""

    rule_id: str
    used: float
    limit: float
    headroom: float
    detail: str


@dataclass(frozen=True, slots=True)
class PortfolioConstructionResult:
    """Constructor 的可审计输出。"""

    target_weights: pd.Series
    trade_weights: pd.Series
    cash_weight: float
    expected_alpha: float
    expected_variance: float | None
    expected_turnover: float
    expected_cost: float
    objective_value: float
    constraint_usage: Mapping[str, ConstraintUsage]
    selected_symbols: tuple[str, ...]
    reason: str


class PortfolioConstructor:
    """将 alpha 转为满足硬约束的目标权重。

    该类无运行态；相同输入始终产生相同的输出和约束审计。策略层只能把分数
    传进来，不能绕过本类自行做权重截断。
    """

    def construct(
        self,
        request: PortfolioConstructionInput,
        constraints: PortfolioConstraints,
    ) -> PortfolioConstructionResult:
        prepared = _prepare(request, constraints)
        universe = prepared["universe"]
        alphas = prepared["alphas"]
        current = prepared["current"]
        benchmark = prepared["benchmark"]
        tradable = prepared["tradable"]
        industries = prepared["industries"]
        market_cap_z = prepared["market_cap_z"]
        styles = prepared["styles"]
        covariance = prepared["covariance"]
        adv = prepared["adv"]

        frozen = universe.difference(tradable)
        target = pd.Series(0.0, index=universe, dtype="float64")
        target.loc[frozen] = current.loc[frozen]
        investable_cap = 1.0 - float(constraints.min_cash_weight)
        frozen_weight = float(target.sum())
        if frozen_weight > investable_cap + _EPS:
            raise PortfolioInfeasibleError([
                f"不可交易持仓 {frozen_weight:.2%} 超过可投资上限 {investable_cap:.2%}"
            ])
        # 冻结（停牌/无行情/退市整理）持仓豁免单票上限：它不可交易，无法主动
        # 减仓，上限约束对其无意义。直接 raise 会让"持仓中某股停牌且权重被动
        # 超限"的合法回测崩溃（RESEARCH 2026-09 实测 600803 停牌触发）。
        # 超限冻结持仓保留并在约束审计里标记 frozen.overweight，待解冻后自然
        # 回落目标权重。可交易持仓由下方贪心分配自然限制在 max_weight 内，
        # 无需在此额外检查。

        # 仅 alpha 有效且可交易的标的是新目标候选；当前但不再有 alpha 的可交易
        # 标的会自然降至零，完成策略分数与组合权重的分离。
        candidates = alphas.index.intersection(tradable)
        if covariance is not None:
            adjusted = alphas.loc[candidates] - float(request.risk_aversion) * pd.Series(
                np.diag(covariance.loc[candidates, candidates]), index=candidates,
            )
        else:
            adjusted = alphas.loc[candidates].copy()
        # 延续奖励只影响同分/相近 alpha 的排序，仍由显式目标函数审计其成本项。
        adjusted = adjusted + float(request.turnover_penalty) * current.loc[candidates]
        ranked = sorted(candidates, key=lambda symbol: (-float(adjusted.loc[symbol]), str(symbol)))

        for symbol in ranked:
            room = min(
                float(constraints.max_weight) - float(target.loc[symbol]),
                investable_cap - float(target.sum()),
            )
            if room <= _EPS:
                continue
            room = _max_increment(
                symbol=symbol,
                target=target,
                benchmark=benchmark,
                room=room,
                constraints=constraints,
                industries=industries,
                market_cap_z=market_cap_z,
                styles=styles,
            )
            if room > _EPS:
                target.loc[symbol] += room

        # 交易容量和换手限制只能让目标向当前组合收缩；两端均会在最终校验中
        # 验证，避免裁剪后无声地破坏行业或风格约束。
        delta = target - current
        if constraints.max_adv_participation is not None:
            capacity = adv * float(constraints.max_adv_participation) / float(request.equity)
            delta = delta.clip(lower=-capacity, upper=capacity)
            target = current + delta
        turnover = _turnover(delta)
        if constraints.max_turnover is not None and turnover > float(constraints.max_turnover) + _EPS:
            delta *= float(constraints.max_turnover) / turnover
            target = current + delta
            turnover = _turnover(delta)

        target = target.where(target.abs() > _EPS, 0.0)
        reasons = _validate_final(
            target=target,
            current=current,
            benchmark=benchmark,
            frozen=frozen,
            constraints=constraints,
            industries=industries,
            market_cap_z=market_cap_z,
            styles=styles,
            adv=adv,
            equity=float(request.equity),
        )
        if reasons:
            raise PortfolioInfeasibleError(reasons)

        trade_weights = target - current
        turnover = _turnover(trade_weights)
        expected_alpha = float((alphas.reindex(universe).fillna(0.0) * target).sum())
        expected_variance = _variance(target, covariance)
        expected_cost = turnover * float(request.equity) * float(request.transaction_cost_bps) / 10_000.0
        cost_fraction = expected_cost / float(request.equity)
        objective_value = (
            expected_alpha
            - float(request.risk_aversion) * (expected_variance or 0.0)
            - float(request.turnover_penalty) * cost_fraction
        )
        usage = _constraint_usage(
            target=target,
            current=current,
            benchmark=benchmark,
            frozen=frozen,
            constraints=constraints,
            industries=industries,
            market_cap_z=market_cap_z,
            styles=styles,
            adv=adv,
            equity=float(request.equity),
        )
        selected = tuple(sorted(target[target > _EPS].index, key=str))
        return PortfolioConstructionResult(
            target_weights=target[target > _EPS].sort_index(),
            trade_weights=trade_weights[trade_weights.abs() > _EPS].sort_index(),
            cash_weight=max(0.0, 1.0 - float(target.sum())),
            expected_alpha=expected_alpha,
            expected_variance=expected_variance,
            expected_turnover=turnover,
            expected_cost=expected_cost,
            objective_value=objective_value,
            constraint_usage=usage,
            selected_symbols=selected,
            reason=str(request.reason),
        )


def _prepare(request: PortfolioConstructionInput, constraints: PortfolioConstraints) -> dict[str, Any]:
    if not np.isfinite(float(request.equity)) or float(request.equity) <= 0:
        raise PortfolioInfeasibleError(["equity 必须为正且有限"])
    if float(request.transaction_cost_bps) < 0:
        raise PortfolioInfeasibleError(["transaction_cost_bps 不能为负"])
    if float(request.risk_aversion) < 0 or float(request.turnover_penalty) < 0:
        raise PortfolioInfeasibleError(["risk_aversion 与 turnover_penalty 不能为负"])

    alphas = _series(request.alphas, "alphas", allow_empty=False)
    if np.isinf(alphas).any():
        raise PortfolioInfeasibleError(["alphas 不能包含无穷值"])
    alphas = alphas.dropna()
    if alphas.empty:
        raise PortfolioInfeasibleError(["没有可用的有限 alpha 分数"])
    current = _series(request.current_weights, "current_weights")
    benchmark = _series(request.benchmark_weights, "benchmark_weights")
    universe = alphas.index.union(current.index).union(benchmark.index).sort_values()
    current = current.reindex(universe, fill_value=0.0)
    benchmark = benchmark.reindex(universe, fill_value=0.0)
    _validate_weight_vector("current_weights", current)
    _validate_weight_vector("benchmark_weights", benchmark)

    if request.tradable is None:
        tradable = universe
    else:
        tradable = pd.Index(sorted({str(symbol) for symbol in request.tradable}, key=str))
        tradable = tradable.intersection(universe)

    needs_industry = constraints.industry_active_bounds is not None
    industries = _categorical_series(request.industries, universe, "industries", required=needs_industry)
    needs_market_cap = constraints.market_cap_active_bound is not None
    market_cap_z = _market_cap_z(request.market_caps, universe, required=needs_market_cap)
    styles = _style_frame(request.style_exposures, universe, constraints.style_active_bounds)
    covariance = _covariance(request.covariance, universe, required=float(request.risk_aversion) > 0)
    if constraints.max_adv_participation is not None:
        adv = _adv_series(request.adv, universe, tradable)
    else:
        adv = pd.Series(np.inf, index=universe, dtype="float64")

    return {
        "universe": universe,
        "alphas": alphas.reindex(universe).fillna(0.0),
        "current": current,
        "benchmark": benchmark,
        "tradable": tradable,
        "industries": industries,
        "market_cap_z": market_cap_z,
        "styles": styles,
        "covariance": covariance,
        "adv": adv,
    }


def _series(value: Mapping[str, float] | pd.Series, name: str, *, allow_empty: bool = True) -> pd.Series:
    series = pd.Series(value, dtype="float64").copy()
    series.index = series.index.map(str)
    if series.index.has_duplicates:
        raise PortfolioInfeasibleError([f"{name} 含重复 symbol"])
    if not allow_empty and series.empty:
        raise PortfolioInfeasibleError([f"{name} 不能为空"])
    return series.sort_index()


def _validate_weight_vector(name: str, weights: pd.Series) -> None:
    if not np.isfinite(weights).all() or (weights < -_EPS).any():
        raise PortfolioInfeasibleError([f"{name} 必须是有限非负权重"])
    if float(weights.sum()) > 1.0 + _EPS:
        raise PortfolioInfeasibleError([f"{name} 总权重超过 100%"])


def _categorical_series(
    value: Mapping[str, str] | pd.Series | None,
    universe: pd.Index,
    name: str,
    *,
    required: bool,
) -> pd.Series | None:
    if value is None:
        if required:
            raise PortfolioInfeasibleError([f"启用行业约束必须提供 {name}"])
        return None
    series = pd.Series(value, dtype="object")
    series.index = series.index.map(str)
    series = series.reindex(universe)
    if required and (series.isna() | (series.astype(str).str.strip() == "")).any():
        missing = sorted(series[series.isna() | (series.astype(str).str.strip() == "")].index)
        raise PortfolioInfeasibleError([f"{name} 缺少分类: {missing}"])
    return series.astype(str)


def _market_cap_z(
    value: Mapping[str, float] | pd.Series | None,
    universe: pd.Index,
    *,
    required: bool,
) -> pd.Series | None:
    if value is None:
        if required:
            raise PortfolioInfeasibleError(["启用市值约束必须提供 market_caps"])
        return None
    caps = _series(value, "market_caps").reindex(universe)
    if required and ((~np.isfinite(caps)) | (caps <= 0)).any():
        missing = sorted(caps[(~np.isfinite(caps)) | (caps <= 0)].index)
        raise PortfolioInfeasibleError([f"market_caps 必须覆盖全部股票且为正: {missing}"])
    if caps.dropna().empty:
        return None
    logged = np.log(caps)
    std = float(logged.std(ddof=0))
    if not np.isfinite(std) or std <= _EPS:
        return pd.Series(0.0, index=universe, dtype="float64")
    return (logged - float(logged.mean())) / std


def _style_frame(
    value: pd.DataFrame | None,
    universe: pd.Index,
    bounds: Mapping[str, float],
) -> pd.DataFrame | None:
    if not bounds:
        return None
    if value is None:
        raise PortfolioInfeasibleError(["启用风格约束必须提供 style_exposures"])
    frame = value.copy()
    frame.index = frame.index.map(str)
    missing_factors = sorted(set(bounds) - set(frame.columns))
    if missing_factors:
        raise PortfolioInfeasibleError([f"style_exposures 缺少因子: {missing_factors}"])
    frame = frame.reindex(index=universe, columns=sorted(bounds)).astype("float64")
    if not np.isfinite(frame.to_numpy()).all():
        raise PortfolioInfeasibleError(["style_exposures 必须覆盖全部股票且为有限数值"])
    return frame


def _covariance(value: pd.DataFrame | None, universe: pd.Index, *, required: bool) -> pd.DataFrame | None:
    if value is None:
        if required:
            raise PortfolioInfeasibleError(["risk_aversion > 0 时必须提供 covariance"])
        return None
    frame = value.copy()
    frame.index = frame.index.map(str)
    frame.columns = frame.columns.map(str)
    frame = frame.reindex(index=universe, columns=universe).astype("float64")
    if not np.isfinite(frame.to_numpy()).all():
        raise PortfolioInfeasibleError(["covariance 必须覆盖全部股票且为有限数值"])
    if not np.allclose(frame.to_numpy(), frame.to_numpy().T, atol=1e-10):
        raise PortfolioInfeasibleError(["covariance 必须为对称矩阵"])
    if (np.diag(frame) < -_EPS).any():
        raise PortfolioInfeasibleError(["covariance 对角线不能为负"])
    return frame


def _adv_series(
    value: Mapping[str, float] | pd.Series | None,
    universe: pd.Index,
    tradable: pd.Index,
) -> pd.Series:
    if value is None:
        raise PortfolioInfeasibleError(["启用 ADV 约束必须提供 adv"])
    series = _series(value, "adv").reindex(universe)
    active = universe.intersection(tradable)
    missing = series.loc[active].isna()
    if missing.any():
        raise PortfolioInfeasibleError([f"adv 缺少可交易股票: {sorted(missing[missing].index)}"])
    invalid = series.notna() & ((~np.isfinite(series)) | (series < 0))
    if invalid.any():
        raise PortfolioInfeasibleError([f"adv 必须为有限非负数: {sorted(series[invalid].index)}"])
    # 非交易的指数成分只用于计算相对基准暴露；其 ADV 对当日交易无意义，可记为
    # 0 而不要求行情面板虚构一条流动性记录。
    return series.fillna(0.0)


def _validate_bound_spec(name: str, value: float | Mapping[str, float] | None) -> None:
    if value is None:
        return
    if isinstance(value, Mapping):
        if not value or any(float(bound) < 0 for bound in value.values()):
            raise ValueError(f"{name} 映射不能为空且上限不能为负")
        return
    if float(value) < 0:
        raise ValueError(f"{name} 不能为负")


def _bound(spec: float | Mapping[str, float] | None, key: str) -> float | None:
    if spec is None:
        return None
    if isinstance(spec, Mapping):
        if key in spec:
            return float(spec[key])
        if "__default__" in spec:
            return float(spec["__default__"])
        raise PortfolioInfeasibleError([f"行业 {key} 没有主动偏离上限"])
    return float(spec)


def _active_exposure(weights: pd.Series, benchmark: pd.Series, values: pd.Series) -> float:
    return float((weights * values).sum() - (benchmark * values).sum())


def _max_increment(
    *,
    symbol: str,
    target: pd.Series,
    benchmark: pd.Series,
    room: float,
    constraints: PortfolioConstraints,
    industries: pd.Series | None,
    market_cap_z: pd.Series | None,
    styles: pd.DataFrame | None,
) -> float:
    allowed = max(0.0, float(room))
    if constraints.industry_active_bounds is not None and industries is not None:
        group = str(industries.loc[symbol])
        bound = _bound(constraints.industry_active_bounds, group)
        assert bound is not None
        group_mask = industries == group
        active = float(target[group_mask].sum() - benchmark[group_mask].sum())
        allowed = min(allowed, max(0.0, float(bound) - active))
    if constraints.market_cap_active_bound is not None and market_cap_z is not None:
        allowed = min(
            allowed,
            _upper_increment_for_exposure(
                _active_exposure(target, benchmark, market_cap_z),
                float(market_cap_z.loc[symbol]),
                float(constraints.market_cap_active_bound),
            ),
        )
    if styles is not None:
        for factor, bound in constraints.style_active_bounds.items():
            values = styles[factor]
            allowed = min(
                allowed,
                _upper_increment_for_exposure(
                    _active_exposure(target, benchmark, values),
                    float(values.loc[symbol]),
                    float(bound),
                ),
            )
    return max(0.0, allowed)


def _upper_increment_for_exposure(active: float, loading: float, bound: float) -> float:
    """在不越过 ``[-bound, bound]`` 的前提下，单标的最多可加多少权重。"""
    if abs(loading) <= _EPS:
        return np.inf
    if loading > 0:
        return max(0.0, (bound - active) / loading)
    return max(0.0, (bound + active) / (-loading))


def _turnover(delta: pd.Series) -> float:
    return 0.5 * float(delta.abs().sum())


def _variance(weights: pd.Series, covariance: pd.DataFrame | None) -> float | None:
    if covariance is None:
        return None
    vector = weights.to_numpy(dtype="float64")
    return float(vector @ covariance.to_numpy(dtype="float64") @ vector)


def _validate_final(
    *,
    target: pd.Series,
    current: pd.Series,
    benchmark: pd.Series,
    frozen: pd.Index,
    constraints: PortfolioConstraints,
    industries: pd.Series | None,
    market_cap_z: pd.Series | None,
    styles: pd.DataFrame | None,
    adv: pd.Series,
    equity: float,
) -> list[str]:
    reasons: list[str] = []
    if not np.isfinite(target).all() or (target < -_EPS).any():
        reasons.append("输出含非有限或负权重")
    invested = float(target.sum())
    if invested > 1.0 - float(constraints.min_cash_weight) + _EPS:
        reasons.append("最低现金约束被违反")
    bad_weight = target[target > float(constraints.max_weight) + _EPS]
    bad_weight = bad_weight[~bad_weight.index.isin(frozen)]  # 冻结持仓豁免单票上限
    if not bad_weight.empty:
        reasons.append(f"单票上限被违反: {sorted(bad_weight.index)}")
    frozen_delta = (target.loc[frozen] - current.loc[frozen]).abs() if len(frozen) else pd.Series(dtype=float)
    if not frozen_delta.empty and float(frozen_delta.max()) > _EPS:
        reasons.append("不可交易持仓未被冻结")

    delta = target - current
    turnover = _turnover(delta)
    if constraints.max_turnover is not None and turnover > float(constraints.max_turnover) + _EPS:
        reasons.append("换手上限被违反")
    if constraints.max_adv_participation is not None:
        capacity = adv * float(constraints.max_adv_participation) / equity
        bad_adv = delta.abs() > capacity + _EPS
        if bad_adv.any():
            reasons.append(f"ADV 参与率上限被违反: {sorted(delta[bad_adv].index)}")

    if constraints.industry_active_bounds is not None and industries is not None:
        for group in sorted(industries.unique(), key=str):
            mask = industries == group
            active = float(target[mask].sum() - benchmark[mask].sum())
            bound = _bound(constraints.industry_active_bounds, str(group))
            assert bound is not None
            if abs(active) > float(bound) + _EPS:
                reasons.append(f"行业主动偏离超限: {group}={active:.2%}")
    if constraints.market_cap_active_bound is not None and market_cap_z is not None:
        active = _active_exposure(target, benchmark, market_cap_z)
        if abs(active) > float(constraints.market_cap_active_bound) + _EPS:
            reasons.append(f"市值主动暴露超限: {active:.4f}")
    if styles is not None:
        for factor, bound in constraints.style_active_bounds.items():
            active = _active_exposure(target, benchmark, styles[factor])
            if abs(active) > float(bound) + _EPS:
                reasons.append(f"风格主动暴露超限: {factor}={active:.4f}")
    return reasons


def _constraint_usage(
    *,
    target: pd.Series,
    current: pd.Series,
    benchmark: pd.Series,
    frozen: pd.Index,
    constraints: PortfolioConstraints,
    industries: pd.Series | None,
    market_cap_z: pd.Series | None,
    styles: pd.DataFrame | None,
    adv: pd.Series,
    equity: float,
) -> dict[str, ConstraintUsage]:
    usage: dict[str, ConstraintUsage] = {}
    invested = float(target.sum())
    cash = max(0.0, 1.0 - invested)
    usage["cash.minimum"] = ConstraintUsage(
        "cash.minimum", float(constraints.min_cash_weight), cash,
        cash - float(constraints.min_cash_weight), "现金权重 / 最低现金",
    )
    usage["gross.invested"] = ConstraintUsage(
        "gross.invested", invested, 1.0 - float(constraints.min_cash_weight),
        1.0 - float(constraints.min_cash_weight) - invested, "股票总权重 / 可投资上限",
    )
    for symbol, weight in target.items():
        usage[f"position.{symbol}"] = ConstraintUsage(
            f"position.{symbol}", float(weight), float(constraints.max_weight),
            float(constraints.max_weight) - float(weight), "单票权重 / 单票上限",
        )
    if constraints.max_turnover is not None:
        turnover = _turnover(target - current)
        usage["turnover"] = ConstraintUsage(
            "turnover", turnover, float(constraints.max_turnover),
            float(constraints.max_turnover) - turnover, "单日换手 / 上限",
        )
    if constraints.max_adv_participation is not None:
        for symbol, change in (target - current).items():
            used = abs(float(change)) * equity
            limit = float(adv.loc[symbol]) * float(constraints.max_adv_participation)
            usage[f"adv.{symbol}"] = ConstraintUsage(
                f"adv.{symbol}", used, limit, limit - used, "调仓名义额 / ADV 可成交额",
            )
    for symbol in frozen:
        usage[f"frozen.{symbol}"] = ConstraintUsage(
            f"frozen.{symbol}", abs(float(target.loc[symbol] - current.loc[symbol])), 0.0,
            0.0, "不可交易持仓目标变动（必须为零）",
        )
    if constraints.industry_active_bounds is not None and industries is not None:
        for group in sorted(industries.unique(), key=str):
            mask = industries == group
            active = float(target[mask].sum() - benchmark[mask].sum())
            limit_value = _bound(constraints.industry_active_bounds, str(group))
            assert limit_value is not None
            limit = float(limit_value)
            usage[f"industry.{group}"] = ConstraintUsage(
                f"industry.{group}", abs(active), limit, limit - abs(active),
                f"{group} 主动权重绝对值 / 上限",
            )
    if constraints.market_cap_active_bound is not None and market_cap_z is not None:
        active = _active_exposure(target, benchmark, market_cap_z)
        limit = float(constraints.market_cap_active_bound)
        usage["market_cap.active"] = ConstraintUsage(
            "market_cap.active", abs(active), limit, limit - abs(active),
            "市值 log-z 主动暴露绝对值 / 上限",
        )
    if styles is not None:
        for factor, bound in constraints.style_active_bounds.items():
            active = _active_exposure(target, benchmark, styles[factor])
            limit = float(bound)
            usage[f"style.{factor}"] = ConstraintUsage(
                f"style.{factor}", abs(active), limit, limit - abs(active),
                f"{factor} 主动暴露绝对值 / 上限",
            )
    return usage


__all__ = [
    "ConstraintUsage",
    "PortfolioConstraints",
    "PortfolioConstructionInput",
    "PortfolioConstructionResult",
    "PortfolioConstructor",
    "PortfolioInfeasibleError",
]

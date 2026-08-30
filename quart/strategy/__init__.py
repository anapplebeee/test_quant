from __future__ import annotations

from quart.config import load_config
from quart.strategy.base import BaseStrategy
from quart.strategy.dual_ma import DualMAStrategy
from quart.strategy.lowvol_composite import LowVolCompositeStrategy
from quart.strategy.ml_rank import MLRankStrategy
from quart.strategy.momentum import MomentumRotationStrategy

REGISTRY: dict[str, type[BaseStrategy]] = {
    "momentum_rotation": MomentumRotationStrategy,
    "dual_ma": DualMAStrategy,
    "ml_rank": MLRankStrategy,
    "lowvol_composite": LowVolCompositeStrategy,
    # 行业内 z-score 打分变体（R2 因子研究：rel_ind_mom20 ICIR 最稳）
    "lowvol_indz": LowVolCompositeStrategy,
}

#: 非策略参数的配置键，传入策略前必须剥离
_NON_PARAM_KEYS = {"name", "overrides", "live_allowlist"}


def resolve_params(name: str, params: dict) -> dict:
    """合并 config.strategy.overrides.<name> 的按策略参数覆盖。

    全局参数（config.strategy.*）对所有策略生效，但最优换手频率等因策略而异
    （实测 2026-08-28：lowvol rebalance 5→20 日使收益 -30.9%→+1.1%，
    而 momentum 对换手不敏感），故支持按策略覆盖，避免"一动全动"。

    优先级：显式传入 params > overrides.<name> > config.strategy.* 全局值。
    （显式参数必须赢——否则 sweep/CLI 指定的参数会被 yaml 静默劫持，
    2026-08-28 实测发生过：overrides 把 sweep 的 rebalance_days 强制改写。）
    """
    cfg = load_config()
    strategy_cfg = cfg.get("strategy") or {}
    global_params = {
        key: value
        for key, value in strategy_cfg.items()
        if key not in _NON_PARAM_KEYS
    }
    overrides = (strategy_cfg.get("overrides") or {}).get(name) or {}
    merged = dict(global_params)
    merged.update(overrides)
    merged.update(params)
    return merged


def _filter_to_schema(cls: type[BaseStrategy], params: dict) -> dict:
    """按 PARAMS_SCHEMA 过滤参数。

    未声明 schema 的策略：只剥离已知的非参数键（name/overrides）。
    已声明 schema 的策略：只保留 schema 键——这样配置里混入的任意杂项
    （如 overrides 子字典）都不会作为策略参数传入。
    """
    if not cls.PARAMS_SCHEMA:
        return {k: v for k, v in params.items() if k not in _NON_PARAM_KEYS}
    return {k: v for k, v in params.items() if k in cls.PARAMS_SCHEMA}


def build_strategy(name: str, **params) -> BaseStrategy:
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy '{name}', available: {sorted(REGISTRY)}")
    cls = REGISTRY[name]
    params = _filter_to_schema(cls, resolve_params(name, params))
    if name == "lowvol_indz":
        params.setdefault("industry_z", True)
    return cls(**params)


__all__ = ["REGISTRY", "BaseStrategy", "build_strategy", "resolve_params"]

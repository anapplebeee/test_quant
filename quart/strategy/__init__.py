from __future__ import annotations

from quart.backtest.engine import BaseStrategy
from quart.config import load_config
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
    overrides = ((cfg.get("strategy") or {}).get("overrides") or {}).get(name) or {}
    if not overrides:
        return dict(params)
    merged = dict(overrides)
    merged.update(params)
    return merged


def build_strategy(name: str, **params) -> BaseStrategy:
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy '{name}', available: {sorted(REGISTRY)}")
    params = resolve_params(name, params)
    if name == "lowvol_indz":
        params.setdefault("industry_z", True)
    return REGISTRY[name](**params)


__all__ = ["BaseStrategy", "build_strategy", "resolve_params", "REGISTRY"]

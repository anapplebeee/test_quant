from __future__ import annotations

from quart.backtest.engine import BaseStrategy
from quart.strategy.dual_ma import DualMAStrategy
from quart.strategy.lowvol_composite import LowVolCompositeStrategy
from quart.strategy.ml_rank import MLRankStrategy
from quart.strategy.momentum import MomentumRotationStrategy

REGISTRY: dict[str, type[BaseStrategy]] = {
    "momentum_rotation": MomentumRotationStrategy,
    "dual_ma": DualMAStrategy,
    "ml_rank": MLRankStrategy,
    "lowvol_composite": LowVolCompositeStrategy,
}


def build_strategy(name: str, **params) -> BaseStrategy:
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy '{name}', available: {sorted(REGISTRY)}")
    return REGISTRY[name](**params)


__all__ = ["BaseStrategy", "build_strategy", "REGISTRY"]

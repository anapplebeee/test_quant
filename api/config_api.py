"""配置 API：平台配置的只读快照（UI-001 DR-05）。

前端（参数词典页）不再直接 import `quart.config.load_config` /
`quart.strategy.build_strategy`，统一经此 API 层；只读，不提供写入口。
"""
from __future__ import annotations


def get_config_snapshot() -> dict:
    """当前生效配置快照。

    Returns
    -------
    {
        "strategy": {...}, "risk": {...}, "backtest": {...}, "manual_trading": {...},
        "effective": {"strategy_name", "top_k", "rebalance_days", "use_regime_filter"},
    }

    `effective` 为默认策略（`config.strategy.name`）解析后的生效参数，
    与回测/信号同源；解析失败时退回 config 原始值。
    """
    from quart.config import load_config
    from quart.strategy import build_strategy

    cfg = load_config()
    strategy_cfg = dict(cfg.get("strategy", {}))
    name = str(strategy_cfg.get("name", "lowvol_indz"))
    use_regime = bool(strategy_cfg.get("use_regime_filter", True))
    try:
        params = build_strategy(name).params
        effective = {
            "strategy_name": name,
            "top_k": int(params.get("top_k", strategy_cfg.get("top_k", 10))),
            "rebalance_days": int(params.get("rebalance_days", strategy_cfg.get("rebalance_days", 5))),
            "use_regime_filter": use_regime,
        }
    except Exception:
        effective = {
            "strategy_name": name,
            "top_k": int(strategy_cfg.get("top_k", 10)),
            "rebalance_days": int(strategy_cfg.get("rebalance_days", 5)),
            "use_regime_filter": use_regime,
        }
    return {
        "strategy": strategy_cfg,
        "risk": dict(cfg.get("risk", {})),
        "backtest": dict(cfg.get("backtest", {})),
        "manual_trading": dict(cfg.get("manual_trading", {})),
        "effective": effective,
    }


__all__ = ["get_config_snapshot"]

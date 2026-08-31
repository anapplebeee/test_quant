"""resolve_params 优先级回归：显式参数 > strategy.overrides.<name> > 全局默认。

背景（2026-08-28 事故）：overrides 曾用 merged.update(overrides) 让 yaml 静默劫持
sweep 显式指定的 rebalance_days，导致同一组合三种周期产出逐位相同的结果。
"""

from quart.strategy import build_strategy, resolve_params


def test_explicit_params_beat_overrides():
    # 显式参数必须赢过 strategy.overrides.lowvol_indz.rebalance_days=40
    s = build_strategy("lowvol_indz", top_k=5, rebalance_days=45)
    assert s.params["rebalance_days"] == 45


def test_override_applies_when_not_explicit():
    # settings.yaml: 全市场复验后采用 45 日调仓 / Top30 / 不叠加反转
    s = build_strategy("lowvol_indz")
    assert s.params.get("rebalance_days") == 45
    assert s.params.get("top_k") == 30
    assert s.params.get("rank_buffer") == 0.5
    assert s.params.get("industry_z") is True
    assert s.params.get("rev_weight") == 0.0


def test_strategy_override_beats_global_defaults():
    s = build_strategy("lowvol_indz")
    assert s.params["rebalance_days"] != 5
    assert s.params["top_k"] != 10


def test_resolve_params_merges_without_dropping_explicit():
    merged = resolve_params("lowvol_indz", {"rebalance_days": 10, "top_k": 7})
    assert merged["rebalance_days"] == 10
    assert merged["top_k"] == 7
    assert merged["industry_z"] is True

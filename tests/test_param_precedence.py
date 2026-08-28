"""resolve_params 优先级回归：显式参数 > strategy.overrides.<name> > 全局默认。

背景（2026-08-28 事故）：overrides 曾用 merged.update(overrides) 让 yaml 静默劫持
sweep 显式指定的 rebalance_days，导致同一组合三种周期产出逐位相同的结果。
"""

from quart.strategy import build_strategy, resolve_params


def test_explicit_params_beat_overrides():
    # settings.yaml: strategy.overrides.lowvol_indz.rebalance_days=20
    s = build_strategy("lowvol_indz", top_k=5, rebalance_days=45)
    assert s.params["rebalance_days"] == 45


def test_override_applies_when_not_explicit():
    s = build_strategy("lowvol_indz")
    assert s.params.get("rebalance_days") == 20
    assert s.params.get("industry_z") is True


def test_resolve_params_merges_without_dropping_explicit():
    merged = resolve_params("lowvol_indz", {"rebalance_days": 10, "top_k": 7})
    assert merged["rebalance_days"] == 10
    assert merged["top_k"] == 7
    assert merged["industry_z"] is True

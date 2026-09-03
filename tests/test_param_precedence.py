"""resolve_params 优先级回归：显式参数 > strategy.overrides.<name> > 全局默认。

背景（2026-08-28 事故）：overrides 曾用 merged.update(overrides) 让 yaml 静默劫持
sweep 显式指定的 rebalance_days，导致同一组合三种周期产出逐位相同的结果。
"""

from quart.strategy import build_strategy, resolve_params


def test_explicit_params_beat_overrides():
    # 显式参数必须赢过 strategy.overrides.lowvol_indz.rebalance_days=45
    s = build_strategy("lowvol_indz", top_k=5, rebalance_days=45)
    assert s.params["rebalance_days"] == 45


def test_override_applies_when_not_explicit():
    # settings.yaml: R011 门禁胜出配置（45 日调仓 / Top8 / 不叠加反转 /
    # R010 3 个正交因子权重各 0.3 / R4 打分择时），权威口径 CAGR 13.3% / MDD -20%
    s = build_strategy("lowvol_indz")
    assert s.params.get("rebalance_days") == 45
    assert s.params.get("top_k") == 8
    assert s.params.get("rank_buffer") == 0.5
    assert s.params.get("industry_z") is True
    assert s.params.get("rev_weight") == 0.0
    assert s.params.get("gap_fill_weight") == 0.3
    assert s.params.get("amount_concen_weight") == 0.3
    assert s.params.get("vol_asym_weight") == 0.3
    assert s.params.get("new_alpha_weight") is None  # 旧单权重已拆分为 3 个独立参数
    assert s.params.get("regime_mode") == "score"


def test_strategy_override_beats_global_defaults():
    s = build_strategy("lowvol_indz")
    assert s.params["rebalance_days"] != 5
    assert s.params["top_k"] != 10


def test_resolve_params_merges_without_dropping_explicit():
    merged = resolve_params("lowvol_indz", {"rebalance_days": 10, "top_k": 7})
    assert merged["rebalance_days"] == 10
    assert merged["top_k"] == 7
    assert merged["industry_z"] is True

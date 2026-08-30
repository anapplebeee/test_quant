"""配置结构校验测试。

配置错误必须在**启动瞬间**暴露，而不是等回测跑一小时后在某一行炸 KeyError。
"""
from __future__ import annotations

import pytest

from quart.config_schema import ConfigError, ensure_valid, validate_config

VALID = {
    "data": {"root": "./data", "adjust": "qfq", "sleep_seconds": 0.35},
    "universe": {"default_index": "000300"},
    "benchmark": "000300",
    "backtest": {
        "initial_cash": 1_000_000.0,
        "commission_rate": 0.00025,
        "commission_min": 5.0,
        "stamp_tax_rate": 0.0005,
        "transfer_fee_rate": 0.00001,
        "slippage_rate": 0.001,
    },
    "strategy": {"name": "lowvol_indz"},
    "risk": {"max_position_pct": 0.25},
}


def test_valid_config_passes():
    assert validate_config(VALID) == []


def test_missing_required_key_reported_with_path():
    cfg = {k: v for k, v in VALID.items() if k != "backtest"}
    problems = validate_config(cfg)
    assert any("backtest.initial_cash" in p for p in problems)
    assert any("缺失必填配置" in p for p in problems)


def test_type_error_reported():
    cfg = {"backtest": {"initial_cash": "一百万"}}
    problems = validate_config(cfg)
    assert any("initial_cash" in p and "类型错误" in p for p in problems)


def test_range_check_catches_nonsense_rates():
    cfg = dict(VALID, backtest=dict(VALID["backtest"], slippage_rate=1.5))
    problems = validate_config(cfg)
    assert any("slippage_rate" in p and "区间" in p for p in problems)


def test_bool_is_not_accepted_as_int():
    """YAML 里 `top_k: true` 会被解析成 bool，不能当 int 用。"""
    cfg = dict(VALID, strategy=dict(VALID["strategy"], top_k=True))
    problems = validate_config(cfg)
    assert any("top_k" in p for p in problems)


def test_invalid_adjust_value():
    cfg = {"data": {"adjust": "bogus"}}
    problems = validate_config(cfg)
    assert any("data.adjust" in p for p in problems)


def test_ensure_valid_raises_with_all_problems():
    with pytest.raises(ConfigError) as exc:
        ensure_valid({"data": {"root": 123}})
    msg = str(exc.value)
    # 一次汇总多个问题，而不是修一个跑一次
    assert msg.count("缺失必填配置") >= 2


def test_empty_config_is_rejected():
    with pytest.raises(ConfigError):
        ensure_valid({})

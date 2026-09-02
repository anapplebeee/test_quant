"""RiskBudgetOverlay 单元测试（微型合成数据，KB 级内存占用）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.execution.constraints import FLAT
from quart.strategy.risk_budget import (
    DRAWDOWN_LEVELS,
    RECOVERY_HYSTERESIS,
    RiskBudgetOverlay,
)


class _AlphaStub:
    """最小 alpha 桩：prepare 记录 md，target_weights 返回预设目标。"""

    name = "stub_alpha"
    required_history_days = 10

    def __init__(self, targets=None):
        self.targets = targets or {}
        self.params = {}
        self.prepared = False
        self.synced = None

    def prepare(self, md):
        self.prepared = True

    def target_weights(self, i):
        return dict(self.targets)

    def set_portfolio_context(self, context):
        self._ctx = context

    def sync_positions(self, positions):
        self.synced = positions

    def serialize_state(self):
        return {"stub": True}

    def load_state_dict(self, state):
        pass


class _Ctx:
    def __init__(self, date, equity, weights=None):
        self.date = date
        self.equity = equity
        self.current_weights = weights if weights is not None else pd.Series(dtype=float)


class _MDStub:
    """market_state 不可得的空壳 md（overlay 需容忍并降级为不启用该维度）。"""

    dates = pd.bdate_range("2024-01-02", periods=10)
    amounts = None
    volumes = None
    benchmark_close = None


def _make_overlay(state_on=False):
    alpha = _AlphaStub(targets={"S1": 0.6, "S2": 0.4})
    return RiskBudgetOverlay(alpha, enable_state=state_on), alpha


def test_prepare_degrades_when_market_state_unavailable():
    ov, alpha = _make_overlay()
    md = _MDStub()
    ov.prepare(md)
    assert alpha.prepared
    assert ov._state_exposure is None  # 状态不可得 → 该维度不启用（不清仓）


def test_drawdown_level_machine_with_hysteresis_and_cooldown():
    ov, _ = _make_overlay()
    ov.prepare(_MDStub())
    dates = pd.bdate_range("2024-01-02", periods=10)
    # 峰值 100 → 回撤 9%：触发档1（0.08→0.6），进入冷却
    ov.set_portfolio_context(_Ctx(dates[0], 100.0))
    assert ov._dd_level == 0
    ov.set_portfolio_context(_Ctx(dates[1], 91.0))
    assert ov._dd_level == 1 and ov._cooldown_until_date == dates[1] + pd.Timedelta(days=5)
    # 冷却期内即使回撤收窄到滞后带以下也不降档
    ov.set_portfolio_context(_Ctx(dates[2], 96.5))  # dd=3.5% <= 8%-4%
    assert ov._dd_level == 1
    # 冷却期后回撤进一步收窄 → 降档归零
    ov.set_portfolio_context(_Ctx(dates[8], 97.5))
    assert ov._dd_level == 0


def test_daily_exposure_scales_targets_on_rebalance_day():
    ov, alpha = _make_overlay()
    ov.prepare(_MDStub())
    dates = pd.bdate_range("2024-01-02", periods=3)
    # 回撤 12% → 档2（0.12→0.4）
    ov.set_portfolio_context(_Ctx(dates[0], 100.0))
    ov.set_portfolio_context(_Ctx(dates[1], 88.0))
    assert ov._dd_level == 2
    out = ov.target_weights(2)
    assert out == pytest.approx({"S1": 0.6 * 0.4, "S2": 0.4 * 0.4})  # 调仓日：alpha 目标缩放


def test_non_rebalance_day_scales_current_holdings():
    alpha = _AlphaStub(targets={})  # alpha 非调仓日返回 {}（保持持仓）
    ov = RiskBudgetOverlay(alpha, enable_state=False)
    ov.prepare(_MDStub())
    dates = pd.bdate_range("2024-01-02", periods=3)
    ov.set_portfolio_context(_Ctx(dates[0], 100.0))
    ov.set_portfolio_context(_Ctx(dates[1], 88.0))
    ov._portfolio_context = _Ctx(
        dates[1], 88.0, weights=pd.Series({"S1": 0.5, "S2": 0.3})
    )
    out = ov.target_weights(2)
    assert out == pytest.approx({"S1": 0.5 * 0.4, "S2": 0.3 * 0.4})  # 对现有持仓缩放


def test_extreme_drawdown_goes_flat():
    alpha = _AlphaStub(targets={})
    ov = RiskBudgetOverlay(alpha, enable_state=False)
    ov.prepare(_MDStub())
    dates = pd.bdate_range("2024-01-02", periods=2)
    ov.set_portfolio_context(_Ctx(dates[0], 100.0))
    ov.set_portfolio_context(_Ctx(dates[1], 82.0))  # dd=18% → 档3 exposure=0.2
    assert ov._dd_level == 3
    assert DRAWDOWN_LEVELS[-1] == (0.16, 0.20)
    # alpha 清仓意图直接放行
    alpha.targets = {FLAT: 1.0}
    assert ov.target_weights(1) == {FLAT: 1.0}


def test_state_serialization_roundtrip():
    ov, _ = _make_overlay()
    ov.prepare(_MDStub())
    dates = pd.bdate_range("2024-01-02", periods=2)
    ov.set_portfolio_context(_Ctx(dates[0], 100.0))
    ov.set_portfolio_context(_Ctx(dates[1], 91.0))
    state = ov.serialize_state()
    ov2, _ = _make_overlay()
    ov2.load_state_dict(state)
    assert ov2._dd_level == 1
    assert ov2._equity_peak == 100.0
    assert list(ov2._equity_history)[-1] == 91.0


def test_params_schema_rejects_unknown():
    with pytest.raises(TypeError):  # BaseStrategy.validate_params 拒绝未知参数
        RiskBudgetOverlay(_AlphaStub(), not_a_param=1)

"""RISK-001：强制 Risk Engine、风险状态机与持久化测试。

验收要点（docs/DEVELOPMENT_COORDINATION.md §12）：
- 回测/信号/paper 三条路径使用同一限额语义（一致性测试）；
- 风险状态机合法/非法迁移与重启后持久性；
- 决策记录携带限额版本、规则结果与原因，且按幂等键去重。
"""
from __future__ import annotations

import pandas as pd
import pytest

from quart.domain.enums import RiskDecisionStatus
from quart.domain.orders import OrderIntent
from quart.domain.time import market_datetime
from quart.infrastructure.db import Database
from quart.infrastructure.job import JobRepository
from quart.market_rules.rule_book import default_rule_book
from quart.risk.engine import (
    PortfolioSnapshot,
    RiskEngine,
    RiskLimits,
    RiskState,
    evaluate_weights,
    limits_from_config,
    require_risk_engine,
)
from quart.risk.store import RiskRepository

TRADE_TIME = market_datetime("2024-05-10", "09:30")
LIMITS = RiskLimits(max_position_pct=0.25)
EQUITY = 1_000_000.0


def _snapshot(
    *,
    account_id: str = "acc1",
    positions: dict | None = None,
    prev_close: dict | None = None,
    equity: float = EQUITY,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account_id=account_id,
        business_time=TRADE_TIME,
        equity=equity,
        cash=equity,
        positions=positions or {},
        prev_close=prev_close or {},
    )


def _buy(symbol: str = "600000", qty: int = 1000, limit_price=None, account_id="acc1"):
    return OrderIntent.create(
        account_id=account_id,
        environment="paper",
        symbol=symbol,
        side="BUY",
        quantity=qty,
        business_time=TRADE_TIME,
        source="test",
        limit_price=limit_price,
    )


def _sell(symbol: str = "600000", qty: int = 1000, account_id="acc1"):
    return OrderIntent.create(
        account_id=account_id,
        environment="paper",
        symbol=symbol,
        side="SELL",
        quantity=qty,
        business_time=TRADE_TIME,
        source="test",
    )


def _engine(state: RiskState = RiskState.ACTIVE, repo=None) -> RiskEngine:
    return RiskEngine(
        LIMITS,
        rule_book=default_rule_book(),
        state_provider=lambda _a: state,
        repo=repo,
    )


# ---------------------------------------------------------------------------
# 限额与版本
# ---------------------------------------------------------------------------


def test_limits_version_is_deterministic_and_sensitive():
    v1 = RiskLimits(max_position_pct=0.25).version()
    v2 = RiskLimits(max_position_pct=0.25).version()
    v3 = RiskLimits(max_position_pct=0.20).version()
    assert v1 == v2
    assert v1 != v3


def test_limits_validation():
    with pytest.raises(ValueError):
        RiskLimits(max_position_pct=0.0)
    with pytest.raises(ValueError):
        RiskLimits(max_position_pct=1.5)


def test_limits_from_config_defaults():
    limits = limits_from_config({"risk": {"max_position_pct": 0.3, "max_daily_loss_pct": 0.04}})
    assert limits.max_position_pct == pytest.approx(0.3)
    assert limits.max_daily_loss_pct == pytest.approx(0.04)
    assert limits.max_gross_exposure_pct == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 权重级评估（回测/信号共用）
# ---------------------------------------------------------------------------


def test_evaluate_weights_clips_and_scales():
    close = pd.Series({
        "600000": 50.0, "000001": 10.0, "000002": 10.0,
        "600001": 10.0, "600002": 10.0,
    })
    targets = {s: 0.30 for s in close.index}
    clean, results = evaluate_weights(LIMITS, targets, close, EQUITY)
    # 5 × 0.30 全部截断到 0.25 → 总权重 1.25 → 等比缩放到 100%
    assert all(w <= 0.25 + 1e-9 for w in clean.values())
    assert sum(clean.values()) == pytest.approx(1.0)
    assert any("超过单票上限" in r.message for r in results)
    assert any("等比缩放" in r.message for r in results)


def test_evaluate_weights_drops_missing_price():
    close = pd.Series({"600000": 50.0})
    clean, results = evaluate_weights(LIMITS, {"000002": 0.1}, close, EQUITY)
    assert clean == {}
    assert any("无最新价格" in r.message for r in results)


def test_rules_module_delegates_to_engine():
    """旧接口 validate_weights 与新引擎行为一致。"""
    from quart.risk.rules import validate_weights

    close = pd.Series({"600000": 50.0})
    clean, violations = validate_weights({"600000": 0.3}, close, EQUITY, 0.25)
    engine_clean, results = evaluate_weights(LIMITS, {"600000": 0.3}, close, EQUITY)
    assert clean == engine_clean
    assert violations == [r.message for r in results]


# ---------------------------------------------------------------------------
# 风险状态机
# ---------------------------------------------------------------------------


@pytest.fixture()
def risk_repo(tmp_path):
    return RiskRepository(Database(tmp_path / "risk.db"))


def test_default_state_is_active(risk_repo):
    assert risk_repo.get_state("acc1") is RiskState.ACTIVE


def test_legal_transitions_and_restart_persistence(tmp_path):
    path = tmp_path / "risk.db"
    repo = RiskRepository(Database(path))
    repo.set_state("acc1", "REDUCING", reason="波动加剧", operator="test")
    repo.set_state("acc1", "HALTED", reason="kill switch", operator="test")
    repo.set_state("acc1", "RECOVERY", reason="对账完成", operator="test")
    assert repo.set_state("acc1", "ACTIVE", reason="复核通过", operator="test") is RiskState.ACTIVE

    # 重启（新实例读同一库）后状态仍在
    reborn = RiskRepository(Database(path))
    assert reborn.get_state("acc1") is RiskState.ACTIVE
    history = reborn.state_history("acc1")
    assert [h["state"] for h in history] == ["ACTIVE", "RECOVERY", "HALTED", "REDUCING"]
    assert history[0]["operator"] == "test"


def test_illegal_transitions_raise(risk_repo):
    risk_repo.set_state("acc1", "HALTED", reason="test")
    with pytest.raises(ValueError, match="非法风险状态迁移"):
        risk_repo.set_state("acc1", "ACTIVE")  # 必须经过 RECOVERY
    with pytest.raises(ValueError):
        risk_repo.set_state("acc1", "REDUCING")


def test_same_state_is_noop(risk_repo):
    assert risk_repo.set_state("acc1", "ACTIVE") is RiskState.ACTIVE
    assert risk_repo.state_history("acc1") == []


# ---------------------------------------------------------------------------
# 订单意图级决策
# ---------------------------------------------------------------------------


def test_buy_within_limits_allowed():
    snapshot = _snapshot(prev_close={"600000": 50.0})
    decision = _engine().evaluate(_buy(qty=1000), snapshot)
    assert decision.status is RiskDecisionStatus.ALLOW
    assert decision.approved_quantity == 1000
    assert decision.limit_version == LIMITS.version()
    assert {r.rule_id for r in decision.rules} == {
        "state_gate", "position_limit", "lot_size", "price_band",
    }


def test_buy_beyond_position_cap_is_adjusted():
    snapshot = _snapshot(prev_close={"600000": 50.0})
    decision = _engine().evaluate(_buy(qty=6000), snapshot)
    # 上限 = 1_000_000 × 25% = 250_000 → 5000 股（整手）
    assert decision.status is RiskDecisionStatus.ADJUST
    assert decision.approved_quantity == 5000
    assert "单票上限" in decision.reason


def test_buy_at_cap_is_denied():
    snapshot = _snapshot(positions={"600000": 5000}, prev_close={"600000": 50.0})
    decision = _engine().evaluate(_buy(qty=1000), snapshot)
    assert decision.status is RiskDecisionStatus.DENY
    assert decision.approved_quantity == 0


def test_sell_is_not_limited_by_position_cap():
    snapshot = _snapshot(positions={"600000": 5000}, prev_close={"600000": 50.0})
    decision = _engine().evaluate(_sell(qty=5000), snapshot)
    assert decision.status is RiskDecisionStatus.ALLOW


def test_lot_rounding_adjusts_and_denies_sub_lot():
    big = RiskLimits(max_position_pct=1.0)
    engine = RiskEngine(
        big, rule_book=default_rule_book(), state_provider=lambda _a: RiskState.ACTIVE
    )
    snapshot = _snapshot(prev_close={"600000": 50.0})
    adjusted = engine.evaluate(_buy(qty=150), snapshot)
    assert adjusted.status is RiskDecisionStatus.ADJUST
    assert adjusted.approved_quantity == 100

    denied = engine.evaluate(_buy(qty=50), snapshot)
    assert denied.status is RiskDecisionStatus.DENY
    assert "不足一手" in denied.reason


def test_state_gate_blocks_when_halted():
    snapshot = _snapshot(prev_close={"600000": 50.0})
    engine = _engine(state=RiskState.HALTED)
    assert engine.evaluate(_buy(), snapshot).status is RiskDecisionStatus.DENY
    assert engine.evaluate(_sell(), snapshot).status is RiskDecisionStatus.DENY


def test_reducing_blocks_buys_but_allows_sells():
    snapshot = _snapshot(positions={"600000": 2000}, prev_close={"600000": 50.0})
    engine = _engine(state=RiskState.REDUCING)
    buy = engine.evaluate(_buy(qty=1000), snapshot)
    sell = engine.evaluate(_sell(qty=1000), snapshot)
    assert buy.status is RiskDecisionStatus.DENY
    assert "REDUCING" in buy.reason
    assert sell.status is RiskDecisionStatus.ALLOW


def test_recovery_denies_new_orders():
    snapshot = _snapshot(prev_close={"600000": 50.0})
    decision = _engine(state=RiskState.RECOVERY).evaluate(_buy(), snapshot)
    assert decision.status is RiskDecisionStatus.DENY


# ---------------------------------------------------------------------------
# 价格笼子（RuleBook 按日期解析历史涨跌幅）
# ---------------------------------------------------------------------------


def test_price_band_denies_outside_limit():
    snapshot = _snapshot(prev_close={"600000": 10.0})
    decision = _engine().evaluate(_buy(qty=1000, limit_price=11.5), snapshot)
    assert decision.status is RiskDecisionStatus.DENY
    assert "价格" in decision.reason or "限价" in decision.reason


def test_price_band_allows_inside_limit():
    snapshot = _snapshot(prev_close={"600000": 10.0})
    decision = _engine().evaluate(_buy(qty=1000, limit_price=10.95), snapshot)
    assert decision.status is RiskDecisionStatus.ALLOW


def test_price_band_respects_board_and_date():
    """创业板 2020-08-24 后为 20%：同样 11.5 元限价，主板拒、创业板过。"""
    snapshot = _snapshot(prev_close={"600000": 10.0, "300001": 10.0})
    engine = _engine()
    main_board = engine.evaluate(_buy("600000", 1000, limit_price=11.5), snapshot)
    chinext = engine.evaluate(_buy("300001", 1000, limit_price=11.5), snapshot)
    assert main_board.status is RiskDecisionStatus.DENY
    assert chinext.status is RiskDecisionStatus.ALLOW


def test_price_band_uses_historical_rules():
    """2019 年创业板仍是 10% 限制（改革前）。"""
    old_time = market_datetime("2019-06-10", "09:30")
    intent = OrderIntent.create(
        account_id="acc1", environment="paper", symbol="300001", side="BUY",
        quantity=1000, business_time=old_time, source="test", limit_price=11.5,
    )
    snapshot = PortfolioSnapshot(
        account_id="acc1", business_time=old_time, equity=EQUITY, cash=EQUITY,
        prev_close={"300001": 10.0},
    )
    decision = _engine().evaluate(intent, snapshot)
    assert decision.status is RiskDecisionStatus.DENY


# ---------------------------------------------------------------------------
# 决策持久化与幂等
# ---------------------------------------------------------------------------


def test_decisions_are_persisted_and_idempotent(risk_repo):
    engine = _engine(repo=risk_repo)
    snapshot = _snapshot(prev_close={"600000": 50.0})
    intent = _buy(qty=6000)
    first = engine.evaluate(intent, snapshot)
    second = engine.evaluate(intent, snapshot)
    assert first.decision_id == second.decision_id
    assert first.status is RiskDecisionStatus.ADJUST
    rows = risk_repo.list_decisions("acc1")
    assert len(rows) == 1
    assert rows[0].approved_quantity == 5000
    assert rows[0].limit_version == LIMITS.version()
    assert any(r.rule_id == "position_limit" for r in rows[0].rules)


def test_engine_wired_to_repo_state(risk_repo):
    """状态闸门直接读仓储：HALTED 后同一引擎立即拒单。"""
    engine = RiskEngine(
        LIMITS,
        rule_book=default_rule_book(),
        state_provider=risk_repo.get_state,
        repo=risk_repo,
    )
    snapshot = _snapshot(prev_close={"600000": 50.0})
    assert engine.evaluate(_buy(), snapshot).status is RiskDecisionStatus.ALLOW
    risk_repo.set_state("acc1", "HALTED", reason="kill switch", operator="test")
    assert engine.evaluate(_buy(), snapshot).status is RiskDecisionStatus.DENY


# ---------------------------------------------------------------------------
# 强制链路与一致性（回测/信号/paper）
# ---------------------------------------------------------------------------


def test_require_risk_engine_is_mandatory_outside_research():
    with pytest.raises(RuntimeError):
        require_risk_engine("paper", None)
    with pytest.raises(RuntimeError):
        require_risk_engine("live", None)
    assert require_risk_engine("research", None) is None


def test_backtest_signal_paper_consistency():
    """同一限额下，权重级截断（回测/信号）与意图级截断（paper）数量一致。

    上限 25% × 100 万 = 25 万；价格 50 元 → 5000 股（整手）。
    """
    close = pd.Series({"600000": 50.0})
    # 权重级（回测 risk_pipeline / 信号 validate_weights 同一实现）
    from quart.risk.rules import make_weight_validator

    validator = make_weight_validator(0.25)
    clean = validator({"600000": 0.30}, close, EQUITY)
    weight_shares = int(clean["600000"] * EQUITY / 50.0)

    # 意图级（paper 下单前 Risk Engine）
    snapshot = _snapshot(prev_close={"600000": 50.0})
    decision = _engine().evaluate(_buy(qty=6000), snapshot)

    assert decision.approved_quantity == 5000
    assert weight_shares == 5000


# ---------------------------------------------------------------------------
# Migration 顺序无关（平台统一注册表）
# ---------------------------------------------------------------------------


def test_migration_order_independence(tmp_path):
    # Risk 先初始化：Job 表仍必须齐全
    db1 = Database(tmp_path / "risk_first.db")
    RiskRepository(db1).migrate()
    jobs = JobRepository(db1)
    job = jobs.create(job_type="t", payload={"x": 1})
    assert jobs.get(job.job_id) is not None

    # Job 先初始化：Risk 表仍必须齐全
    db2 = Database(tmp_path / "job_first.db")
    JobRepository(db2).migrate()
    risk = RiskRepository(db2)
    assert risk.get_state("acc1") is RiskState.ACTIVE
    risk.set_state("acc1", "REDUCING", reason="t")
    assert risk.get_state("acc1") is RiskState.REDUCING

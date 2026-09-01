"""QA-001：Paper E2E 与故障演练。

验收标准（docs/DEVELOPMENT_COORDINATION.md §12 QA-001）：
    T+1、重启、断线、重复回报和对账通过

演练链路（批次 4，§10.3 Trading Release）：
    计划审批（TradingRepository）→ RiskEngine 强制风控（决策落库）
    → PersistentPaperBroker 报单（OMS 单一状态源）→ 成交回报入账
    → sync_broker_fills 写入 T+1 账本 → reconcile 券商快照对账
    → OBS 指标与结构化日志可检索

与既有测试的边界：
    - tests/test_baseline_t1_idempotent_restart_dup_fill.py：F 泳道质量基线
      （内存 PaperBroker + 账本，单元级）；
    - tests/test_broker_persistent.py：BROKER-001 单元验收；
    - 本文件把风控/OMS/Paper/账本/对账/OBS 串成端到端演练：故障注入走
      PaperFaultConfig，重启用"重建实例读同一库"模拟——不用 mock 成功
      代替真实持久化和恢复验证（§11.2 F 泳道边界）。
"""
from __future__ import annotations

import json

import loguru
import pytest

from quart.broker.models import BrokerFill, BrokerOrderRequest
from quart.broker.persistent import PaperFaultConfig, PersistentPaperBroker
from quart.broker.sync import sync_broker_fills
from quart.domain import OrderStatus, market_datetime
from quart.domain.enums import RiskDecisionStatus
from quart.infrastructure.db import Database
from quart.manual_trading import PlannedOrderInput, TradingRepository, next_trade_date
from quart.observability.metrics import MetricsRepository, collect_core_metrics
from quart.observability.structured import (
    configure_structured_logging,
    new_trace_id,
    trace_context,
)
from quart.oms import OrderRepository
from quart.risk.engine import PortfolioSnapshot, RiskEngine, RiskLimits
from quart.risk.store import RiskRepository

SIGNAL_DATE = "2026-08-31"  # T 日（周一）收盘决策
TRADE_DATE = "2026-09-01"  # T+1 交易日
SYMBOL = "600519"
PRICE = 1500.0

#: T 日收盘、T+1 开盘与 T+2 开盘的业务时间（上海时区，aware）
T_CLOSE = market_datetime(SIGNAL_DATE, "15:00")
T1_OPEN = market_datetime(TRADE_DATE, "09:30")
T2_OPEN = market_datetime("2026-09-02", "09:30")


# =============================================================================
# 演练环境（每个测试独立的临时库；OMS 与账本分库，与生产布局一致）
# =============================================================================


@pytest.fixture()
def oms_db(tmp_path) -> Database:
    """OMS/风控/指标共用的平台库（生产中同属平台 SQLite）。"""
    return Database(tmp_path / "platform.db")


@pytest.fixture()
def trading_repo(tmp_path) -> TradingRepository:
    repo = TradingRepository(tmp_path / "trading.db")
    repo.initialize_schema()
    return repo


@pytest.fixture()
def risk_engine(oms_db: Database) -> RiskEngine:
    risk_store = RiskRepository(oms_db)
    return RiskEngine(
        RiskLimits(max_position_pct=0.25),
        state_provider=risk_store.get_state,
        repo=risk_store,
    )


def make_broker(oms_db: Database, fault: PaperFaultConfig | None = None) -> PersistentPaperBroker:
    """构造 Paper 券商；对同一 oms_db 重复构造即模拟"进程重启后重建实例"。"""
    return PersistentPaperBroker(OrderRepository(oms_db), fault=fault)


def make_engine(oms_db: Database) -> RiskEngine:
    risk_store = RiskRepository(oms_db)
    return RiskEngine(
        RiskLimits(max_position_pct=0.25),
        state_provider=risk_store.get_state,
        repo=risk_store,
    )


def init_account(repo: TradingRepository, cash: float = 4_000_000.0):
    """初始化账本账户（信号日收盘口径）。"""
    repo.initialize_account(cash=cash, positions={}, as_of=SIGNAL_DATE)
    state = repo.account_state(as_of=SIGNAL_DATE)
    assert state is not None
    return state


def risk_snapshot(state, business_time) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account_id=str(state.account_id),
        business_time=business_time,
        equity=state.cash_total,
        cash=state.cash_total,
        positions={s: p.total_quantity for s, p in state.positions.items()},
    )


def submit_through_risk(
    engine: RiskEngine,
    broker: PersistentPaperBroker,
    state,
    *,
    client_order_id: str,
    side: str,
    quantity: int,
    price: float,
    planned_order_id: int | None = None,
    business_time=T_CLOSE,
):
    """计划订单 → 强制风控 → Paper 报单（§8.2：所有订单经过统一规则和 Risk Engine）。"""
    intent = PlannedOrderInput(SYMBOL, side, quantity, price).to_order_intent(
        account_id=str(state.account_id),
        planned_order_id=planned_order_id,
        business_time=business_time,
    )
    decision = engine.evaluate(intent, risk_snapshot(state, business_time))
    assert decision.status is not RiskDecisionStatus.DENY, decision.reason
    request = BrokerOrderRequest(
        symbol=SYMBOL,
        side=side,
        quantity=decision.approved_quantity,
        limit_price=price,
        client_order_id=client_order_id,
        planned_order_id=planned_order_id,
    )
    return broker.submit_order(request), decision


def paper_fills_as_broker_fills(broker: PersistentPaperBroker) -> list[BrokerFill]:
    """OMS 成交账本 → 可入账的 Broker 回报（与真实券商回报同一通道）。"""
    return [BrokerFill.from_domain(f) for f in broker.oms.list_fills(account_id="paper")]


def sync_new_fills_only(repo: TradingRepository, account_id: int, broker: PersistentPaperBroker) -> list[int]:
    """断点续传语义：按账本已记录的幂等键过滤，只同步未入账的增量回报。"""
    synced = {f["broker_fill_id"] for f in repo.list_fills()}
    fresh = [
        bf for bf in paper_fills_as_broker_fills(broker)
        if bf.to_domain_fill().idempotency_key not in synced
    ]
    return sync_broker_fills(repo, account_id, fresh)


# =============================================================================
# 1. T+1 全链路 E2E：T 收盘决策 → T+1 报单成交 → 账本结算 → 可卖变化
# =============================================================================


class TestT1FullCycleE2E:
    """验收：T+1 通过——买入当日不可卖，下一交易日起可卖并可再交易。"""

    def test_buy_settlement_blocks_same_day_sellable(self, oms_db, trading_repo, risk_engine):
        state = init_account(trading_repo)
        broker = make_broker(oms_db)

        # T 日收盘：计划 → 审批 → 风控 → Paper 报单 → 两笔部分成交
        plan_id = trading_repo.create_trade_plan(
            account_id=state.account_id,
            strategy_name="lowvol_indz",
            signal_date=SIGNAL_DATE,
            intended_trade_date=TRADE_DATE,
            orders=[PlannedOrderInput(SYMBOL, "BUY", 600, PRICE, 0.15)],
        )
        trading_repo.approve_plan(plan_id)
        planned_order_id = int(trading_repo.plan_detail(plan_id)["orders"][0]["planned_order_id"])

        order, decision = submit_through_risk(
            risk_engine, broker, state,
            client_order_id=f"{plan_id}:{planned_order_id}",
            side="BUY", quantity=600, price=PRICE,
            planned_order_id=planned_order_id,
        )
        assert order.status is OrderStatus.SUBMITTED
        assert decision.status is RiskDecisionStatus.ALLOW

        broker.apply_fill(order.client_order_id, 250, PRICE, trade_date=TRADE_DATE,
                          trade_time="09:30:00", broker_fill_id="t1-buy-f1")
        filled = broker.apply_fill(order.client_order_id, 350, PRICE + 2,
                                   trade_date=TRADE_DATE, trade_time="09:31:00",
                                   broker_fill_id="t1-buy-f2")
        assert filled.status is OrderStatus.FILLED
        assert filled.filled_quantity == 600

        # 成交回报统一入账通道 → T+1 账本
        ids = sync_broker_fills(trading_repo, state.account_id, paper_fills_as_broker_fills(broker))
        assert len(ids) == 2

        # T+1 收盘口径：持仓 600，但当日买入不可卖
        t1_state = trading_repo.account_state(as_of=TRADE_DATE)
        assert t1_state is not None
        assert t1_state.total_positions == {SYMBOL: 600}
        assert t1_state.sellable_positions == {SYMBOL: 0}

        # T+2：settle 完成，全部可卖
        t2_state = trading_repo.account_state(as_of="2026-09-02")
        assert t2_state is not None
        assert t2_state.sellable_positions == {SYMBOL: 600}

    def test_sell_after_settle_round_trip(self, oms_db, trading_repo, risk_engine):
        state = init_account(trading_repo)
        broker = make_broker(oms_db)

        # 买入 600 并入账（T+1 可卖）
        plan_buy = trading_repo.create_trade_plan(
            account_id=state.account_id, strategy_name="lowvol_indz",
            signal_date=SIGNAL_DATE, intended_trade_date=TRADE_DATE,
            orders=[PlannedOrderInput(SYMBOL, "BUY", 600, PRICE, 0.15)],
        )
        trading_repo.approve_plan(plan_buy)
        planned_id = int(trading_repo.plan_detail(plan_buy)["orders"][0]["planned_order_id"])
        buy_order, _ = submit_through_risk(
            risk_engine, broker, state,
            client_order_id=f"{plan_buy}:{planned_id}",
            side="BUY", quantity=600, price=PRICE, planned_order_id=planned_id,
        )
        broker.apply_fill(buy_order.client_order_id, 600, PRICE, trade_date=TRADE_DATE,
                          trade_time="09:30:00", broker_fill_id="rt-buy")
        sync_broker_fills(trading_repo, state.account_id, paper_fills_as_broker_fills(broker))

        # T+1 收盘先对账（审批门禁要求信号日账户已对账，与 README 流程一致），
        # 对账后再创建 T+2 卖出计划（平台强制 intended > signal_date）
        state_t1 = trading_repo.account_state(as_of=TRADE_DATE)
        assert state_t1 is not None
        reconciled = trading_repo.reconcile(
            account_name=state_t1.account_name,
            as_of=TRADE_DATE,
            cash_total=state_t1.cash_total,
            cash_available=state_t1.cash_available_to_trade,
            cash_withdrawable=state_t1.cash_withdrawable,
            # T+1 收盘口径：600 股当日买入，09-02 起方可卖出
            positions={SYMBOL: {"total_quantity": 600, "sellable_quantity": 0}},
            confirm=True,
            resolution="QA-001 演练：T+1 收盘对账",
        )
        assert reconciled.matched
        plan_sell = trading_repo.create_trade_plan(
            account_id=state_t1.account_id, strategy_name="lowvol_indz",
            signal_date=TRADE_DATE, intended_trade_date="2026-09-02",
            orders=[PlannedOrderInput(SYMBOL, "SELL", 400, PRICE + 20, 0.10)],
        )
        trading_repo.approve_plan(plan_sell)
        sell_planned_id = int(trading_repo.plan_detail(plan_sell)["orders"][0]["planned_order_id"])
        sell_order, _ = submit_through_risk(
            risk_engine, broker, state_t1,
            client_order_id=f"{plan_sell}:{sell_planned_id}",
            side="SELL", quantity=400, price=PRICE + 20,
            planned_order_id=sell_planned_id, business_time=T2_OPEN,
        )
        broker.apply_fill(sell_order.client_order_id, 400, PRICE + 18,
                          trade_date="2026-09-02", trade_time="09:30:00",
                          broker_fill_id="rt-sell")
        sync_new_fills_only(trading_repo, state_t1.account_id, broker)

        after = trading_repo.account_state(as_of="2026-09-02")
        assert after is not None
        assert after.total_positions == {SYMBOL: 200}
        # 卖出的是已 settle 批次，剩余 200 仍可卖
        assert after.sellable_positions == {SYMBOL: 200}
        # Paper 与账本的持仓查询模型一致（对账前置条件）
        assert broker.positions() == after.total_positions

    def test_plan_trade_date_is_next_trade_day(self, trading_repo):
        """计划交易日必须落在下一交易日（T+1 约束）。"""
        assert next_trade_date(SIGNAL_DATE) == TRADE_DATE


# =============================================================================
# 2. 断线演练：drop_ack（报单确认丢失）与 reject（券商拒绝）
# =============================================================================


class TestDisconnectFaultDrill:
    """验收：断线通过——超时不是失败结论，先查询再补发，重试不重复发单。"""

    def test_drop_ack_recovers_via_query_then_replay(self, oms_db, trading_repo, risk_engine):
        state = init_account(trading_repo)
        broken = make_broker(oms_db, PaperFaultConfig(submit_outcome="drop_ack"))

        order, _ = submit_through_risk(
            risk_engine, broken, state,
            client_order_id="drill-drop-1", side="BUY", quantity=100, price=PRICE,
        )
        assert order.status is OrderStatus.SUBMITTING
        assert order.broker_order_id is None  # 确认丢失，停在途状态

        # 断线期间盲目重试：幂等返回当前状态，不产生新订单/新回报（§8.2 防重复发单）
        retried = broken.submit_order(
            BrokerOrderRequest(symbol=SYMBOL, side="BUY", quantity=100, limit_price=PRICE,
                               client_order_id="drill-drop-1")
        )
        assert retried.status is OrderStatus.SUBMITTING
        assert len(broken.oms.list_orders()) == 1
        assert len(broken.oms.list_reports("drill-drop-1")) == 2

        # 进程重启：新实例读库，恢复入口列出途状态订单
        recovered = make_broker(oms_db)
        assert [o.client_order_id for o in recovered.active_orders()] == ["drill-drop-1"]

        # 恢复纪律：先按 client_order_id 向券商查询，确认已送达才补发 SUBMITTED
        queried = recovered.get_order("drill-drop-1")
        assert queried is not None and queried.status is OrderStatus.SUBMITTING
        confirmed = recovered.confirm_submitted("drill-drop-1")
        assert confirmed.status is OrderStatus.SUBMITTED
        assert confirmed.broker_order_id

        # 恢复后成交照常入账
        recovered.apply_fill("drill-drop-1", 100, PRICE, trade_date=TRADE_DATE,
                             trade_time="09:35:00", broker_fill_id="drill-drop-fill")
        assert recovered.positions() == {SYMBOL: 100}
        ids = sync_broker_fills(trading_repo, state.account_id, paper_fills_as_broker_fills(recovered))
        assert len(ids) == 1
        after = trading_repo.account_state(as_of=TRADE_DATE)
        assert after is not None
        assert after.total_positions == {SYMBOL: 100}

    def test_reject_lands_terminal_state_and_retry_is_idempotent(self, oms_db, trading_repo, risk_engine):
        state = init_account(trading_repo)
        broker = make_broker(oms_db, PaperFaultConfig(submit_outcome="reject"))

        order, _ = submit_through_risk(
            risk_engine, broker, state,
            client_order_id="drill-reject-1", side="BUY", quantity=100, price=PRICE,
        )
        assert order.status is OrderStatus.REJECTED
        assert order.is_terminal
        statuses = [r["status"] for r in broker.oms.list_reports("drill-reject-1")]
        assert statuses[-1] == "REJECTED"

        # 重试同一委托：返回既有终态，不产生新订单
        again = broker.submit_order(
            BrokerOrderRequest(symbol=SYMBOL, side="BUY", quantity=100, limit_price=PRICE,
                               client_order_id="drill-reject-1")
        )
        assert again.status is OrderStatus.REJECTED
        assert len(broker.oms.list_orders()) == 1
        # REJECTED 不在恢复队列里
        assert broker.active_orders() == []


# =============================================================================
# 3. Kill Switch 与风控状态机演练（§10.3：演练 Kill Switch、撤单）
# =============================================================================


class TestKillSwitchDrill:
    """HALTED 禁止新订单、RECOVERY 需人工复核、恢复 ACTIVE 后放行、撤单始终允许。"""

    def test_halted_denies_new_orders_but_allows_cancel(self, oms_db, trading_repo, risk_engine):
        risk_store = RiskRepository(oms_db)
        state = init_account(trading_repo)
        broker = make_broker(oms_db)

        # 正常报单一笔（供撤单演练）
        live, _ = submit_through_risk(
            risk_engine, broker, state,
            client_order_id="ks-live", side="BUY", quantity=100, price=PRICE,
        )
        assert live.status is OrderStatus.SUBMITTED

        # Kill Switch：ACTIVE → HALTED
        risk_store.set_state(str(state.account_id), "HALTED",
                             reason="演练：触发 Kill Switch", operator="qa_drill")
        assert risk_store.get_state(str(state.account_id)).value == "HALTED"

        intent = PlannedOrderInput(SYMBOL, "BUY", 100, PRICE).to_order_intent(
            account_id=str(state.account_id), business_time=T1_OPEN)
        denied = risk_engine.evaluate(intent, risk_snapshot(state, T1_OPEN))
        assert denied.status is RiskDecisionStatus.DENY
        assert "HALTED" in denied.reason
        assert len(broker.oms.list_orders()) == 1  # 新订单未进入 OMS

        # HALTED 期间撤单与查询仍然允许（§9 状态语义）
        canceled = broker.cancel_order(live.client_order_id)
        assert canceled.status is OrderStatus.CANCELED

    def test_recovery_requires_explicit_reactivate(self, oms_db, trading_repo, risk_engine):
        risk_store = RiskRepository(oms_db)
        state = init_account(trading_repo)
        account_key = str(state.account_id)

        def evaluate_buy() -> RiskDecisionStatus:
            intent = PlannedOrderInput(SYMBOL, "BUY", 100, PRICE).to_order_intent(
                account_id=account_key, business_time=T1_OPEN)
            return risk_engine.evaluate(intent, risk_snapshot(state, T1_OPEN)).status

        risk_store.set_state(account_key, "HALTED", reason="演练")
        risk_store.set_state(account_key, "RECOVERY",
                             reason="演练：完成对账后进入恢复", operator="qa_drill")
        assert evaluate_buy() is RiskDecisionStatus.DENY  # RECOVERY 仍禁止新订单

        risk_store.set_state(account_key, "ACTIVE",
                             reason="演练：人工复核通过", operator="qa_drill")
        assert evaluate_buy() is RiskDecisionStatus.ALLOW


# =============================================================================
# 4. 重复回报演练：OMS 层与账本层都不重复入账
# =============================================================================


class TestDuplicateReportDrill:
    """验收：重复回报通过——同 broker_fill_id 重放在两层都幂等。"""

    def test_duplicate_report_replay_is_idempotent_at_oms(self, oms_db, trading_repo, risk_engine):
        state = init_account(trading_repo)
        broker = make_broker(oms_db)
        order, _ = submit_through_risk(
            risk_engine, broker, state,
            client_order_id="dup-oms-1", side="BUY", quantity=500, price=PRICE,
        )
        broker.apply_fill(order.client_order_id, 400, PRICE, trade_date=TRADE_DATE,
                          trade_time="09:30:00", broker_fill_id="dup-fill-1")

        # 同一回报重复推送 3 次
        for _ in range(3):
            replayed = broker.apply_fill(order.client_order_id, 400, PRICE,
                                         trade_date=TRADE_DATE, trade_time="09:30:00",
                                         broker_fill_id="dup-fill-1")
            assert replayed.status is OrderStatus.PARTIALLY_FILLED
        assert len(broker.oms.list_fills(account_id="paper")) == 1
        assert broker.positions() == {SYMBOL: 400}

        # 部分成交继续推进不重叠
        filled = broker.apply_fill(order.client_order_id, 100, PRICE + 1,
                                   trade_date=TRADE_DATE, trade_time="09:31:00",
                                   broker_fill_id="dup-fill-2")
        assert filled.status is OrderStatus.FILLED
        assert filled.filled_quantity == 500

    def test_duplicate_sync_rejected_and_ledger_unchanged(self, oms_db, trading_repo, risk_engine):
        state = init_account(trading_repo)
        broker = make_broker(oms_db)
        order, _ = submit_through_risk(
            risk_engine, broker, state,
            client_order_id="dup-ledger-1", side="BUY", quantity=100, price=PRICE,
        )
        broker.apply_fill(order.client_order_id, 100, PRICE, trade_date=TRADE_DATE,
                          trade_time="09:30:00", broker_fill_id="dup-ledger-fill")

        fills = paper_fills_as_broker_fills(broker)
        assert len(sync_broker_fills(trading_repo, state.account_id, fills)) == 1
        cash_after_first = trading_repo.account_state(as_of=TRADE_DATE)
        assert cash_after_first is not None
        expected_cash = cash_after_first.cash_total

        with pytest.raises(ValueError, match="成交编号重复"):
            sync_broker_fills(trading_repo, state.account_id, fills)

        after = trading_repo.account_state(as_of=TRADE_DATE)
        assert after is not None
        assert after.cash_total == expected_cash
        assert after.total_positions == {SYMBOL: 100}
        assert len(trading_repo.list_fills()) == 1


# =============================================================================
# 5. 重启恢复演练（跨层）：OMS 恢复 + 账本持久 + 全程不重复入账
# =============================================================================


class TestRestartRecoveryE2E:
    """验收：重启通过——两笔在途订单跨进程恢复，部分成交续传不重复。"""

    def test_crash_between_submit_and_fill_recovers_without_double_booking(self, tmp_path):
        oms_path = tmp_path / "platform.db"
        ledger_path = tmp_path / "trading.db"

        # ---- 进程 A：正常单部分成交 + 断线单，随后"崩溃" ----
        oms_a = Database(oms_path)
        ledger_a = TradingRepository(ledger_path)
        ledger_a.initialize_schema()
        state = init_account(ledger_a)
        engine_a = make_engine(oms_a)
        broker_a = make_broker(oms_a)

        ok_order, _ = submit_through_risk(
            engine_a, broker_a, state,
            client_order_id="rs-ok", side="BUY", quantity=600, price=PRICE,
        )
        broker_a.apply_fill(ok_order.client_order_id, 250, PRICE, trade_date=TRADE_DATE,
                            trade_time="09:30:00", broker_fill_id="rs-fill-1")

        faulted = make_broker(oms_a, PaperFaultConfig(submit_outcome="drop_ack"))
        stuck_order, _ = submit_through_risk(
            engine_a, faulted, state,
            client_order_id="rs-stuck", side="BUY", quantity=100, price=PRICE,
        )
        assert stuck_order.status is OrderStatus.SUBMITTING

        # 部分成交已同步账本
        sync_broker_fills(ledger_a, state.account_id, paper_fills_as_broker_fills(broker_a))

        # ---- 进程 B：全部实例重建（同库），模拟重启 ----
        oms_b = Database(oms_path)
        ledger_b = TradingRepository(ledger_path)
        broker_b = make_broker(oms_b)

        # 恢复队列：两笔非终态订单（部分成交 + 途状态）
        active = {o.client_order_id: o for o in broker_b.active_orders()}
        assert set(active) == {"rs-ok", "rs-stuck"}
        assert active["rs-ok"].status is OrderStatus.PARTIALLY_FILLED
        assert active["rs-ok"].filled_quantity == 250
        assert active["rs-stuck"].status is OrderStatus.SUBMITTING

        # 账本跨进程持久：250 股已入账、当日不可卖
        ledger_state = ledger_b.account_state(as_of=TRADE_DATE)
        assert ledger_state is not None
        assert ledger_state.total_positions == {SYMBOL: 250}
        assert ledger_state.sellable_positions == {SYMBOL: 0}

        # 旧进程的重复回报重放：幂等不重复入账
        replay = broker_b.apply_fill("rs-ok", 250, PRICE, trade_date=TRADE_DATE,
                                     trade_time="09:30:00", broker_fill_id="rs-fill-1")
        assert replay.filled_quantity == 250
        assert len(broker_b.oms.list_fills(account_id="paper")) == 1

        # 续传剩余 350 + 断线单走查询补发后成交
        broker_b.apply_fill("rs-ok", 350, PRICE + 1, trade_date=TRADE_DATE,
                            trade_time="09:40:00", broker_fill_id="rs-fill-2")
        confirmed = broker_b.confirm_submitted("rs-stuck")
        assert confirmed.status is OrderStatus.SUBMITTED
        broker_b.apply_fill("rs-stuck", 100, PRICE, trade_date=TRADE_DATE,
                            trade_time="09:41:00", broker_fill_id="rs-fill-3")

        # 重启后同步增量成交（断点续传语义，重复回报靠幂等键去重）
        assert len(sync_new_fills_only(ledger_b, state.account_id, broker_b)) == 2

        assert broker_b.positions() == {SYMBOL: 700}
        final = ledger_b.account_state(as_of=TRADE_DATE)
        assert final is not None
        assert final.total_positions == {SYMBOL: 700}
        assert len(ledger_b.list_fills()) == 3

        # 风控状态机持久：新实例读到同一账户状态
        assert RiskRepository(oms_b).get_state(str(state.account_id)).value == "ACTIVE"


# =============================================================================
# 6. 对账演练：差异可见 → 确认覆盖 → 复核一致（§8.2 差异不被静默覆盖）
# =============================================================================


class TestReconciliationE2E:
    """验收：对账通过——账本 vs 券商快照预览差异，确认后覆盖，复核一致。"""

    def test_reconcile_preview_then_confirm_overrides_ledger(
        self, oms_db, trading_repo, risk_engine
    ):
        state = init_account(trading_repo)
        broker = make_broker(oms_db)

        plan_id = trading_repo.create_trade_plan(
            account_id=state.account_id, strategy_name="lowvol_indz",
            signal_date=SIGNAL_DATE, intended_trade_date=TRADE_DATE,
            orders=[PlannedOrderInput(SYMBOL, "BUY", 600, PRICE, 0.15)],
        )
        trading_repo.approve_plan(plan_id)
        planned_id = int(trading_repo.plan_detail(plan_id)["orders"][0]["planned_order_id"])
        order, _ = submit_through_risk(
            risk_engine, broker, state,
            client_order_id=f"{plan_id}:{planned_id}",
            side="BUY", quantity=600, price=PRICE, planned_order_id=planned_id,
        )
        broker.apply_fill(order.client_order_id, 600, PRICE, trade_date=TRADE_DATE,
                          trade_time="09:30:00", broker_fill_id="rc-fill-1")
        sync_broker_fills(trading_repo, state.account_id, paper_fills_as_broker_fills(broker))

        # 券商侧又成交一笔计划外买入（未同步账本）→ 制造真实差异
        extra, _ = submit_through_risk(
            risk_engine, broker, state,
            client_order_id="rc-extra", side="BUY", quantity=100, price=PRICE,
            business_time=T1_OPEN,
        )
        broker.apply_fill(extra.client_order_id, 100, PRICE + 1, trade_date=TRADE_DATE,
                          trade_time="10:00:00", broker_fill_id="rc-fill-2")
        assert broker.positions() == {SYMBOL: 700}

        ledger = trading_repo.account_state(as_of=TRADE_DATE)
        assert ledger is not None
        # 预览：差异可见（账本 600 vs 券商 700），不覆盖
        preview = trading_repo.reconcile(
            account_name=ledger.account_name,
            as_of=TRADE_DATE,
            cash_total=ledger.cash_total,
            cash_available=ledger.cash_available_to_trade,
            cash_withdrawable=ledger.cash_withdrawable,
            positions={SYMBOL: {"total_quantity": 700, "sellable_quantity": 700}},
        )
        assert not preview.matched
        assert preview.position_differences[SYMBOL]["ledger_total"] == 600
        assert preview.position_differences[SYMBOL]["broker_total"] == 700
        assert preview.reconciliation_id is None  # 仅预览，未落确认记录

        # 确认覆盖：以券商快照为准
        confirmed = trading_repo.reconcile(
            account_name=ledger.account_name,
            as_of=TRADE_DATE,
            cash_total=ledger.cash_total,
            cash_available=ledger.cash_available_to_trade,
            cash_withdrawable=ledger.cash_withdrawable,
            positions={SYMBOL: {"total_quantity": 700, "sellable_quantity": 700}},
            confirm=True,
            resolution="QA-001 演练：以 Paper 券商快照为准",
        )
        assert confirmed.confirmed and confirmed.reconciliation_id is not None

        # 复核：覆盖后账本与券商一致
        after = trading_repo.account_state(as_of=TRADE_DATE)
        assert after is not None
        assert after.total_positions == {SYMBOL: 700}
        recheck = trading_repo.reconcile(
            account_name=after.account_name,
            as_of=TRADE_DATE,
            cash_total=after.cash_total,
            cash_available=after.cash_available_to_trade,
            cash_withdrawable=after.cash_withdrawable,
            positions={SYMBOL: {"total_quantity": 700, "sellable_quantity": 700}},
        )
        assert recheck.matched

    def test_reconcile_matched_when_no_difference(self, oms_db, trading_repo, risk_engine):
        state = init_account(trading_repo)
        broker = make_broker(oms_db)
        order, _ = submit_through_risk(
            risk_engine, broker, state,
            client_order_id="rc-clean", side="BUY", quantity=100, price=PRICE,
        )
        broker.apply_fill(order.client_order_id, 100, PRICE, trade_date=TRADE_DATE,
                          trade_time="09:30:00", broker_fill_id="rc-clean-fill")
        sync_broker_fills(trading_repo, state.account_id, paper_fills_as_broker_fills(broker))

        ledger = trading_repo.account_state(as_of=TRADE_DATE)
        assert ledger is not None
        result = trading_repo.reconcile(
            account_name=ledger.account_name,
            as_of=TRADE_DATE,
            cash_total=ledger.cash_total,
            cash_available=ledger.cash_available_to_trade,
            cash_withdrawable=ledger.cash_withdrawable,
            positions={SYMBOL: {"total_quantity": 100, "sellable_quantity": 0}},
        )
        assert result.matched


# =============================================================================
# 7. 可观测性演练：指标可派生、链路日志可检索（OBS-001 接线验证）
# =============================================================================


class TestObservabilityDrill:
    """演练全程的订单/风控指标与结构化日志满足 §13.2 检索要求。"""

    def test_core_metrics_reflect_drill_outcomes(self, oms_db, trading_repo, risk_engine):
        state = init_account(trading_repo)
        ok_broker = make_broker(oms_db)
        order, _ = submit_through_risk(
            risk_engine, ok_broker, state,
            client_order_id="obs-ok", side="BUY", quantity=100, price=PRICE,
        )
        ok_broker.apply_fill(order.client_order_id, 100, PRICE, trade_date=TRADE_DATE,
                             trade_time="09:30:00", broker_fill_id="obs-fill")

        reject_broker = make_broker(oms_db, PaperFaultConfig(submit_outcome="reject"))
        rejected, _ = submit_through_risk(
            risk_engine, reject_broker, state,
            client_order_id="obs-reject", side="BUY", quantity=100, price=PRICE,
        )
        assert rejected.status is OrderStatus.REJECTED

        metrics = collect_core_metrics(oms_db)
        assert metrics["order"]["orders_total"] == 2
        assert metrics["order"]["orders_filled"] == 1
        assert metrics["order"]["orders_rejected"] == 1
        assert metrics["order"]["order_fill_rate"] == pytest.approx(0.5)
        assert metrics["risk"]["risk_decisions_total"] >= 2

    def test_order_lifecycle_logs_are_field_searchable(self, oms_db, trading_repo, risk_engine, tmp_path):
        log_path = tmp_path / "drill.jsonl"
        sinks = configure_structured_logging(log_path)
        try:
            state = init_account(trading_repo)
            broker = make_broker(oms_db)
            with trace_context(
                trace_id=new_trace_id(),
                account_id=str(state.account_id),
                environment="paper",
                strategy="qa_drill",
            ):
                order, _ = submit_through_risk(
                    risk_engine, broker, state,
                    client_order_id="obs-log-1", side="BUY", quantity=100, price=PRICE,
                )
                broker.apply_fill(order.client_order_id, 100, PRICE, trade_date=TRADE_DATE,
                                  trade_time="09:30:00", broker_fill_id="obs-log-fill")
        finally:
            for sink_id in sinks:
                loguru.logger.remove(sink_id)

        lines = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines() if x]
        transitions = [
            r["record"]["extra"] for r in lines
            if r["record"]["extra"].get("event") == "order.transition"
        ]
        assert transitions, "演练全程必须产生可检索的订单状态事件"
        first = transitions[0]
        # §13.2 标准关联字段：单笔订单可按 account_id/order_id/environment 检索全链路
        assert first["account_id"]
        assert first["order_id"] == "obs-log-1"
        assert first["environment"] == "paper"
        assert {t["status"] for t in transitions} >= {"RISK_APPROVED", "SUBMITTING", "SUBMITTED"}

    def test_custom_metric_round_trip(self, oms_db):
        repo = MetricsRepository(oms_db)
        repo.record("paper_drill.reconcile_matched", 1.0,
                    labels={"account": "paper", "drill": "qa-001"})
        value, labels = repo.latest("paper_drill.reconcile_matched")
        assert value == 1.0
        assert labels["drill"] == "qa-001"

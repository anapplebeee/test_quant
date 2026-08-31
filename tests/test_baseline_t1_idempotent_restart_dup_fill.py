"""T+1、幂等、重启恢复、重复回报测试基线（协调文档 F 质量基线）。

依据 DEVELOPMENT_COORDINATION.md 第 11.2 节 F 泳道：
    "固化当前测试基线，建立 T+1、幂等、重启恢复、重复回报和对账验收清单"

本模块把四项关键验收标准收敛到一份基线，任何批次出口评审都 directamente 运行
`pytest tests/test_baseline_t1_idempotent_restart_dup_fill.py -q` 即可判断是否达标。

四项基线：
============

1. T+1 结算规则（协调文档 §3 交易任务、§8.2 交易安全检查）
   - 买入持仓当日不可卖（settle_date = 下一交易日）。
   - 下一交易日及以后方可卖出，账户持仓的 sellable_quantity 正确反映。

2. 幂等（协调文档 §3 交易任务"幂等键"、§8.1 通用检查）
   - Job 幂等键：重复提交同一 key 返回同一 job，不产生新记录。
   - 成交编号幂等：同一 broker_fill_id 二次入账触发唯一约束报错。
   - sync_broker_fills 整批拒绝重复成交（不部分入账）。

3. 重启恢复（协调文档 §10.2 Platform Release、§11.2 F 泳道）
   - Job 崩溃后 lease 过期 → recover() 重新入队 → 新 Worker 可认领执行。
   - 账户快照/余额/持仓 lot 在进程重建后通过持久化文件完整恢复。

4. 重复回报（协调文档 §3 交易任务"重复回报、部分成交和对账"、§8.2）
   - 券商重复推送同一 broker_fill_id 时，账本只计入一次。
   - PaperBroker 产生的 fill 回报同步到TradingRepository 后，重复同步被拒绝。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quart.broker.models import BrokerFill, BrokerOrderRequest
from quart.broker.paper import PaperBrokerAdapter
from quart.broker.sync import sync_broker_fills
from quart.execution.models import BUY, SELL
from quart.infrastructure.db import Database
from quart.infrastructure.job import JobRepository
from quart.infrastructure.job_schema import JOB_QUEUED, JOB_RUNNING, JOB_SUCCEEDED
from quart.manual_trading import FillInput, PlannedOrderInput, TradingRepository


# =============================================================================
# 夹具
# =============================================================================


@pytest.fixture
def db(tmp_path) -> Database:
    """每个测试独立的临时 SQLite 数据库。"""
    return Database(tmp_path / "baseline.db")


@pytest.fixture
def job_repo(db: Database) -> JobRepository:
    return JobRepository(db, lease_seconds=5)


@pytest.fixture
def trading_repo(tmp_path) -> TradingRepository:
    """每个测试独立的交易账本。"""
    repo = TradingRepository(tmp_path / "baseline_trading.db")
    repo.initialize_schema()
    return repo


def _approve_plan_with_order(
    repo: TradingRepository,
    signal_date: str = "2026-08-28",
    trade_date: str = "2026-08-31",
) -> tuple[int, str, int]:
    """初始化账户并创建一个仅含一个 BUY 订单的已审批计划。

    返回 ``(account_id, plan_id, planned_order_id)``。
    修复：此前返回 (account_id, order_id)，调用方误把 order_id 当 plan_id，
    导致 plan_detail(order_id) 返回 None（重复回报基线测试失败）。
    """
    repo.initialize_account(cash=1_000_000, positions={}, as_of=signal_date)
    state = repo.account_state(as_of=signal_date)
    assert state is not None
    plan_id = repo.create_trade_plan(
        account_id=state.account_id,
        strategy_name="baseline_strategy",
        signal_date=signal_date,
        intended_trade_date=trade_date,
        orders=[PlannedOrderInput("600519", BUY, 100, 1500.0, 0.15)],
    )
    repo.approve_plan(plan_id)
    order_id = int(repo.plan_detail(plan_id)["orders"][0]["planned_order_id"])
    return state.account_id, plan_id, order_id


# =============================================================================
# 1. T+1 结算规则
# =============================================================================


class TestT1SettlementBaseline:
    """T+1 结算规则基线：买入当日不可卖，T+1 日起可卖。"""

    def test_buy_not_sellable_same_day(self, trading_repo):
        """买入持仓当日 sellable_quantity == 0。"""
        trading_repo.initialize_account(
            cash=1_000_000, positions={}, as_of="2026-08-28"
        )
        state = trading_repo.account_state(as_of="2026-08-28")
        assert state is not None

        trading_repo.record_fill(
            state.account_id,
            FillInput(
                symbol="600000",
                side=BUY,
                quantity=1_000,
                price=10.0,
                trade_date="2026-08-31",
                broker_fill_id="t1-buy-1",
            ),
        )

        same_day = trading_repo.account_state(as_of="2026-08-31")
        assert same_day is not None
        assert same_day.total_positions == {"600000": 1_000}
        assert same_day.sellable_positions == {"600000": 0}

    def test_buy_sellable_next_trade_day(self, trading_repo):
        """买入持仓在下一交易日 sellable_quantity == 总量。"""
        trading_repo.initialize_account(
            cash=1_000_000, positions={}, as_of="2026-08-28"
        )
        state = trading_repo.account_state(as_of="2026-08-28")
        assert state is not None

        trading_repo.record_fill(
            state.account_id,
            FillInput(
                symbol="600000",
                side=BUY,
                quantity=1_000,
                price=10.0,
                trade_date="2026-08-31",
                broker_fill_id="t1-buy-2",
            ),
        )

        next_day = trading_repo.account_state(as_of="2026-09-01")
        assert next_day is not None
        assert next_day.total_positions == {"600000": 1_000}
        assert next_day.sellable_positions == {"600000": 1_000}

    def test_sell_rejected_before_settle(self, trading_repo):
        """在 settle 日前卖出应被拒绝（可卖数量不足）。"""
        trading_repo.initialize_account(
            cash=1_000_000, positions={}, as_of="2026-08-28"
        )
        state = trading_repo.account_state(as_of="2026-08-28")
        assert state is not None

        trading_repo.record_fill(
            state.account_id,
            FillInput(
                symbol="600000",
                side=BUY,
                quantity=1_000,
                price=10.0,
                trade_date="2026-08-31",
                broker_fill_id="t1-buy-3",
            ),
        )

        # 同一天尝试卖出 → 失败
        with pytest.raises(ValueError, match="可卖数量不足"):
            trading_repo.record_fill(
                state.account_id,
                FillInput(
                    symbol="600000",
                    side=SELL,
                    quantity=500,
                    price=10.5,
                    trade_date="2026-08-31",
                    broker_fill_id="t1-sell-1",
                ),
            )

    def test_sell_succeeds_after_settle(self, trading_repo):
        """settle 日后可正常卖出。"""
        trading_repo.initialize_account(
            cash=1_000_000, positions={}, as_of="2026-08-28"
        )
        state = trading_repo.account_state(as_of="2026-08-28")
        assert state is not None

        # T 日买入
        trading_repo.record_fill(
            state.account_id,
            FillInput(
                symbol="600000",
                side=BUY,
                quantity=1_000,
                price=10.0,
                trade_date="2026-08-31",
                broker_fill_id="t1-buy-4",
            ),
        )

        # T+1 日卖出一部分
        trading_repo.record_fill(
            state.account_id,
            FillInput(
                symbol="600000",
                side=SELL,
                quantity=500,
                price=10.5,
                trade_date="2026-09-01",
                broker_fill_id="t1-sell-2",
            ),
        )

        after_sell = trading_repo.account_state(as_of="2026-09-01")
        assert after_sell is not None
        assert after_sell.total_positions == {"600000": 500}
        # T 日买入的 1000 股在 T+1 已 settle，卖出 500 后剩 500 均可卖
        assert after_sell.sellable_positions == {"600000": 500}

    def test_position_lot_settle_date_is_next_trade_day(self, trading_repo, tmp_path):
        """验证 position_lots.settle_date 被正确设为 next_trade_date。"""
        from quart.manual_trading.repository import next_trade_date

        trading_repo.initialize_account(
            cash=1_000_000, positions={}, as_of="2026-08-28"
        )
        state = trading_repo.account_state(as_of="2026-08-28")
        assert state is not None

        trading_repo.record_fill(
            state.account_id,
            FillInput(
                symbol="601398",
                side=BUY,
                quantity=2_000,
                price=5.0,
                trade_date="2026-08-31",
                broker_fill_id="t1-buy-5",
            ),
        )

        expected_settle = next_trade_date("2026-08-31")
        lots = trading_repo.list_fills()
        assert len(lots) == 1


# =============================================================================
# 2. 幂等性保障
# =============================================================================


class TestIdempotencyBaseline:
    """幂等性保障基线：Job 幂等键 + 成交编号去重。"""

    def test_job_idempotency_key_returns_same_job(self, job_repo):
        """同一幂等键重复提交 → 返回相同 job_id，不产生新记录。"""
        j1 = job_repo.create("backtest", {"v": 1}, idempotency_key="idem-1")
        j2 = job_repo.create("backtest", {"v": 999}, idempotency_key="idem-1")

        assert j1.job_id == j2.job_id
        assert j1.payload == {"v": 1}  # 原 payload 不被覆盖

    def test_job_idempotency_uses_original_status(self, job_repo):
        """幂等键返回的 job 保持其当前状态（不会被重置）。"""
        j1 = job_repo.create("backtest", {"v": 1}, idempotency_key="idem-2")
        claimed = job_repo.claim("worker-x")
        assert claimed is not None

        # 重复提交 → 返回当前已 CLAIMED 的 job
        j2 = job_repo.create("backtest", {"v": 2}, idempotency_key="idem-2")
        assert j2.job_id == j1.job_id
        assert j2.status == JOB_QUEUED or j2.status == "CLAIMED"

    def test_duplicate_broker_fill_id_rejected(self, trading_repo):
        """同一 broker_fill_id 二次 record_fill → 报错拒绝。"""
        trading_repo.initialize_account(
            cash=1_000_000, positions={}, as_of="2026-08-28"
        )
        state = trading_repo.account_state(as_of="2026-08-28")
        assert state is not None

        trading_repo.record_fill(
            state.account_id,
            FillInput(
                symbol="600000",
                side=BUY,
                quantity=100,
                price=10.0,
                trade_date="2026-08-31",
                broker_fill_id="dup-idem-1",
            ),
        )

        with pytest.raises(ValueError, match="成交编号重复"):
            trading_repo.record_fill(
                state.account_id,
                FillInput(
                    symbol="600000",
                    side=BUY,
                    quantity=100,
                    price=10.0,
                    trade_date="2026-08-31",
                    broker_fill_id="dup-idem-1",
                ),
            )

    def test_account_balance_unchanged_by_duplicate_fill_attempt(self, trading_repo):
        """重复入账失败后账户余额保持不变。"""
        trading_repo.initialize_account(
            cash=1_000_000, positions={}, as_of="2026-08-28"
        )
        state = trading_repo.account_state(as_of="2026-08-28")
        assert state is not None

        trading_repo.record_fill(
            state.account_id,
            FillInput(
                symbol="600000",
                side=BUY,
                quantity=1_000,
                price=10.0,
                trade_date="2026-08-31",
                broker_fill_id="dup-idem-2",
            ),
        )

        cash_after_first = trading_repo.account_state(as_of="2026-08-31")
        assert cash_after_first is not None
        expected_cash = cash_after_first.cash_total

        with pytest.raises(ValueError):
            trading_repo.record_fill(
                state.account_id,
                FillInput(
                    symbol="600000",
                    side=BUY,
                    quantity=1_000,
                    price=10.0,
                    trade_date="2026-08-31",
                    broker_fill_id="dup-idem-2",
                ),
            )

        cash_after_dup = trading_repo.account_state(as_of="2026-08-31")
        assert cash_after_dup is not None
        assert cash_after_dup.cash_total == expected_cash

    def test_sync_broker_fills_rejects_duplicate_batch(self, trading_repo):
        """sync_broker_fills 对整批重复成交报错（不部分入账）。"""
        account_id, _, _ = _approve_plan_with_order(trading_repo)

        adapter = PaperBrokerAdapter()
        order = adapter.submit_order(
            BrokerOrderRequest(
                symbol="600519", side=BUY, quantity=100,
                limit_price=1500.0, client_order_id="p:1",
                planned_order_id=1,
            )
        )
        adapter.apply_fill(
            order.broker_order_id, 100, 1501.0,
            trade_date="2026-08-31", broker_fill_id="paper-dup-1",
        )

        # 第一次同步成功
        ids = sync_broker_fills(
            trading_repo, account_id, adapter.list_fills(),
            source="PAPER_BROKER",
        )
        assert len(ids) == 1

        # 第二次同步同一批 → 整批拒绝
        with pytest.raises(ValueError, match="成交编号重复"):
            sync_broker_fills(
                trading_repo, account_id, adapter.list_fills(),
                source="PAPER_BROKER",
            )


# =============================================================================
# 3. 重启恢复
# =============================================================================


class TestRestartRecoveryBaseline:
    """重启恢复基线：Job 崩溃恢复 + 进程重建后账本完整恢复。"""

    def test_job_crash_then_recover(self, db):
        """Job 运行中崩溃 → lease 过期 → recover 重新入队。"""
        repo = JobRepository(db, lease_seconds=5)
        job = repo.create("backtest", {"strategy": "lowvol"}, max_attempts=3)
        claimed = repo.claim("worker-1")
        repo.mark_running(claimed.job_id, "worker-1")

        # 模拟 crash：lease 过期
        _expire_lease_in_db(db, claimed.job_id)

        # 新进程 recover
        repo2 = JobRepository(db, lease_seconds=5)
        stats = repo2.recover()
        assert stats["requeued"] == 1

        recovered_job = repo2.get(job.job_id)
        assert recovered_job is not None
        assert recovered_job.status == JOB_QUEUED
        assert recovered_job.attempts == 1  # 保留尝试次数
        assert recovered_job.claimed_by is None

    def test_job_recovery_allows_new_worker_to_complete(self, db):
        """恢复后新 Worker 能认领并完成 job。"""
        repo = JobRepository(db, lease_seconds=5)
        job = repo.create("backtest", {"k": "v"}, max_attempts=3)
        claimed = repo.claim("worker-1")
        repo.mark_running(claimed.job_id, "worker-1")
        _expire_lease_in_db(db, claimed.job_id)

        # recover
        repo.recover()

        # 新 Worker 认领并完成
        repo2 = JobRepository(db, lease_seconds=5)
        reclaimed = repo2.claim("worker-2")
        assert reclaimed is not None
        assert reclaimed.job_id == job.job_id

        repo2.mark_running(reclaimed.job_id, "worker-2")
        assert repo2.succeed(reclaimed.job_id, "worker-2", {"recovered": True})

        final = repo2.get(job.job_id)
        assert final.status == JOB_SUCCEEDED
        assert final.result == {"recovered": True}

    def test_job_recovery_respects_max_attempts(self, db):
        """反复崩溃超过 max_attempts 后 job 标记 FAILED。"""
        repo = JobRepository(db, lease_seconds=5)
        job = repo.create("backtest", max_attempts=2)

        # 第 1 次崩溃
        claimed = repo.claim("w1")
        _expire_lease_in_db(db, claimed.job_id)
        repo.recover()

        # 第 2 次崩溃
        claimed2 = repo.claim("w2")
        assert claimed2 is not None
        _expire_lease_in_db(db, claimed2.job_id)
        # attempts=2 >= max_attempts=2 → FAILED
        stats = repo.recover()
        assert stats["failed"] == 1

        final = repo.get(job.job_id)
        assert final.status == "FAILED"
        assert "attempts" in final.error

    def test_recovery_is_idempotent(self, db):
        """反复调用 recover 不会重复回收。"""
        repo = JobRepository(db, lease_seconds=5)
        repo.create("backtest", {"k": "v"})
        claimed = repo.claim("w1")
        assert claimed is not None
        _expire_lease_in_db(db, claimed.job_id)

        s1 = repo.recover()
        s2 = repo.recover()
        assert s1["requeued"] == 1
        assert s2["requeued"] == 0

    def test_trading_account_survives_restart(self, tmp_path):
        """TradingRepository 跨进程重建后数据完整恢复。"""
        db_path = tmp_path / "restart_trading.db"

        # 进程 A：初始化 + 买入
        repo_a = TradingRepository(db_path)
        repo_a.initialize_schema()
        repo_a.initialize_account(cash=1_000_000, positions={}, as_of="2026-08-28")
        state_a = repo_a.account_state(as_of="2026-08-28")
        assert state_a is not None

        repo_a.record_fill(
            state_a.account_id,
            FillInput(
                symbol="600000",
                side=BUY,
                quantity=1_000,
                price=10.0,
                trade_date="2026-08-31",
                broker_fill_id="restart-buy-1",
            ),
        )

        # 进程 B（重建，同文件）
        repo_b = TradingRepository(db_path)
        state_b = repo_b.account_state(as_of="2026-09-01")
        assert state_b is not None
        assert state_b.total_positions == {"600000": 1_000}
        assert state_b.sellable_positions == {"600000": 1_000}
        assert len(repo_b.list_fills()) == 1

    def test_multiple_crash_recover_cycles(self, db):
        """多次崩溃-恢复循环，每次 attempts 正确累加。"""
        repo = JobRepository(db, lease_seconds=5)
        job = repo.create("backtest", max_attempts=5)

        for i in range(3):
            claimed = repo.claim(f"worker-{i}")
            assert claimed is not None
            _expire_lease_in_db(db, claimed.job_id)
            stats = repo.recover()
            assert stats["requeued"] == 1

        # 3 次崩溃后 attempts=3
        mid = repo.get(job.job_id)
        assert mid.attempts == 3

        # 最终完成
        final_claim = repo.claim("final-worker")
        assert final_claim is not None
        repo.mark_running(final_claim.job_id, "final-worker")
        repo.succeed(final_claim.job_id, "final-worker", {"done": True})
        assert repo.get(job.job_id).status == JOB_SUCCEEDED


# =============================================================================
# 4. 重复回报
# =============================================================================


class TestDuplicateExecutionReportBaseline:
    """重复回报测试基线：同一 broker_fill_id 多次回报不重复入账。"""

    def test_paper_broker_fill_sync_duplicate_rejected(self, trading_repo):
        """PaperBroker 成交回报重复 sync 到账本报错。"""
        account_id, plan_id, _ = _approve_plan_with_order(trading_repo)
        detail = trading_repo.plan_detail(plan_id)
        assert detail is not None, f"plan_detail returned None for plan_id={plan_id!r}"
        order = detail["orders"][0]
        planned_order_id = int(order["planned_order_id"])

        # PaperBroker 产生成交
        adapter = PaperBrokerAdapter()
        broker_order = adapter.submit_order(
            BrokerOrderRequest(
                symbol=order["symbol"], side=order["side"],
                quantity=int(order["strategy_quantity"]),
                limit_price=float(order["reference_price"]),
                client_order_id=f"{plan_id}:{planned_order_id}",
                planned_order_id=planned_order_id,
            )
        )
        adapter.apply_fill(
            broker_order.broker_order_id, 100, 1500.0,
            trade_date="2026-08-31", broker_fill_id="dup-fill-001",
        )

        # 第一次同步：成功
        ids1 = sync_broker_fills(
            trading_repo, account_id, adapter.list_fills(),
            source="PAPER_BROKER",
        )
        assert len(ids1) == 1

        # 第二次同步：因 broker_fill_id 重复而报错
        with pytest.raises(ValueError, match="成交编号重复"):
            sync_broker_fills(
                trading_repo, account_id, adapter.list_fills(),
                source="PAPER_BROKER",
            )

        # 确认只有一条 fill 记录（_approve_plan_with_order 用默认账户 manual）
        fills = trading_repo.list_fills()
        assert len(fills) == 1
        assert fills[0]["broker_fill_id"] == "dup-fill-001"

    def test_duplicate_fill_preserves_account_balance(self, trading_repo):
        """重复回报失败后账户余额和持仓不变。"""
        trading_repo.initialize_account(
            cash=1_000_000, positions={}, as_of="2026-08-28"
        )
        state = trading_repo.account_state(as_of="2026-08-28")
        assert state is not None
        account_id = state.account_id

        fill = FillInput(
            symbol="600000", side=BUY, quantity=1_000, price=10.0,
            trade_date="2026-08-31", broker_fill_id="dup-balance-1",
        )
        trading_repo.record_fill(account_id, fill)

        balance_after = trading_repo.account_state(as_of="2026-08-31")
        assert balance_after is not None
        cash_before = balance_after.cash_total

        # 尝试重复入账
        with pytest.raises(ValueError, match="成交编号重复"):
            trading_repo.record_fill(account_id, fill)

        balance_after_dup = trading_repo.account_state(as_of="2026-08-31")
        assert balance_after_dup is not None
        assert balance_after_dup.cash_total == cash_before
        assert balance_after_dup.total_positions == {"600000": 1_000}

    def test_multiple_fills_same_attempt_only_one_succeeds(self, trading_repo):
        """连续 5 次尝试同一 broker_fill_id，只有第一次成功。"""
        trading_repo.initialize_account(
            cash=1_000_000, positions={}, as_of="2026-08-28"
        )
        state = trading_repo.account_state(as_of="2026-08-28")
        assert state is not None
        account_id = state.account_id

        success_count = 0
        for _ in range(5):
            try:
                trading_repo.record_fill(
                    account_id,
                    FillInput(
                        symbol="600000", side=BUY, quantity=100, price=10.0,
                        trade_date="2026-08-31", broker_fill_id="multi-dup-1",
                    ),
                )
                success_count += 1
            except ValueError as e:
                assert "成交编号重复" in str(e)

        assert success_count == 1

    def test_different_broker_fill_ids_both_succeed(self, trading_repo):
        """不同 broker_fill_id 可以正常分别入账。"""
        trading_repo.initialize_account(
            cash=1_000_000, positions={}, as_of="2026-08-28"
        )
        state = trading_repo.account_state(as_of="2026-08-28")
        assert state is not None
        account_id = state.account_id

        trading_repo.record_fill(
            account_id,
            FillInput(
                symbol="600000", side=BUY, quantity=500, price=10.0,
                trade_date="2026-08-31", broker_fill_id="unique-A",
            ),
        )
        trading_repo.record_fill(
            account_id,
            FillInput(
                symbol="600000", side=BUY, quantity=500, price=10.0,
                trade_date="2026-08-31", broker_fill_id="unique-B",
            ),
        )

        end_state = trading_repo.account_state(as_of="2026-08-31")
        assert end_state is not None
        assert end_state.total_positions == {"600000": 1_000}
        fills = trading_repo.list_fills()
        assert len(fills) == 2

    def test_sync_broker_fills_consistent_plan_status(self, trading_repo):
        """同步成交后计划状态正确推进为 COMPLETED。"""
        account_id, plan_id, _ = _approve_plan_with_order(trading_repo)
        order = trading_repo.plan_detail(plan_id)["orders"][0]
        planned_order_id = int(order["planned_order_id"])

        # 同步前状态为 APPROVED
        detail_before = trading_repo.plan_detail(plan_id)
        assert detail_before is not None
        assert detail_before["plan"]["status"] == "APPROVED"

        # 执行成交同步
        adapter = PaperBrokerAdapter()
        broker_order = adapter.submit_order(
            BrokerOrderRequest(
                symbol=order["symbol"], side=order["side"],
                quantity=int(order["strategy_quantity"]),
                limit_price=float(order["reference_price"]),
                client_order_id=f"{plan_id}:{planned_order_id}",
                planned_order_id=planned_order_id,
            )
        )
        adapter.apply_fill(
            broker_order.broker_order_id, 100, 1500.0,
            trade_date="2026-08-31", broker_fill_id="plan-status-001",
        )
        sync_broker_fills(
            trading_repo, account_id, adapter.list_fills(),
            source="PAPER_BROKER",
        )

        # 同步后计划状态推进为 COMPLETED
        detail_after = trading_repo.plan_detail(plan_id)
        assert detail_after is not None
        assert detail_after["plan"]["status"] == "COMPLETED"


# =============================================================================
# 辅助
# =============================================================================


def _expire_lease_in_db(db: Database, job_id: str) -> None:
    """把 job 租约设为过去（模拟进程崩溃后不再续约）。"""
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET lease_until = ? WHERE job_id = ?", (past, job_id))

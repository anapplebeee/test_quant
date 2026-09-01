"""全量代码检视缺陷回归测试（2026-09-01 审计）。

本文件锁住一次全量检视中修复的缺陷，防止回退。每条测试都对应一个
**已确认的真实故障**，而非风格改进：

1. 迁移脚本不可逆销毁历史数据（读旧分区失败 → 用部分数据覆盖 + 删源）；
2. 卖出不回补 `cash_withdrawable` → 可取资金单向衰减到 0；
3. `broker_fill_id` 为空时两层幂等键失效 → 重复回报重复入账；
4. 终态订单的重复回报从"幂等返回"退化为抛错；
5. 脚本中的未定义名（NameError），其中一处发生在 `--apply` 副作用之后；
6. 生成器函数用带值 `return` 返回错误 → Gradio 拿不到，用户看到"点了没反应"。

静态检查类测试（5、6）用 AST / ruff 扫描源码，覆盖到**尚未被执行过**的
代码路径——这是纯运行时测试做不到的。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from quart.broker.models import BrokerOrderRequest
from quart.broker.persistent import PersistentPaperBroker
from quart.data.store import PARTITION_PREFIX, BarStore
from quart.domain import OrderStatus
from quart.execution.models import BUY, SELL
from quart.infrastructure.db import Database
from quart.manual_trading import FillInput, TradingRepository
from quart.oms import OrderRepository

REPO_ROOT = Path(__file__).resolve().parent.parent


def _repository(tmp_path) -> TradingRepository:
    repo = TradingRepository(tmp_path / "trading.db")
    repo.initialize_schema()
    return repo


def _broker(db_path) -> PersistentPaperBroker:
    return PersistentPaperBroker(OrderRepository(Database(db_path)))


def _request(client_order_id: str = "client-1", quantity: int = 1000) -> BrokerOrderRequest:
    return BrokerOrderRequest(
        symbol="600000.SH", side="BUY", quantity=quantity, client_order_id=client_order_id,
    )


# =============================================================================
# 1. 迁移脚本：任何失败都必须保留源数据（不可逆销毁防护）
# =============================================================================


def _make_bars(symbol: str, dates, price: float = 100.0):
    import pandas as pd

    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "symbol": symbol,
        "open": price, "high": price + 1, "low": price - 1, "close": price,
        "volume": 1_000_000.0,
        "amount": price * 1_000_000.0,
    })


class TestMigrationNeverDestroysData:
    """迁移是唯一会**删除源文件**的操作，失败路径必须 fail-safe。"""

    def test_aborts_and_keeps_source_when_partition_unreadable(self, tmp_path):
        """目标分区读不出来时：中止该 symbol，源文件保留，数据一条不少。"""
        store = BarStore(root=tmp_path, partitioned=False)
        store.save(_make_bars("600519", __import__("pandas").bdate_range("2024-01-01", "2025-12-31"), 10.0))
        src = tmp_path / "daily" / "600519.parquet"
        assert src.exists()

        # 预置一个损坏的分区文件，模拟 parquet 损坏 / 权限 / 版本不兼容
        dst_dir = tmp_path / "daily" / f"{PARTITION_PREFIX}2024"
        dst_dir.mkdir(parents=True, exist_ok=True)
        (dst_dir / "600519.parquet").write_bytes(b"corrupted-not-a-parquet")

        stats = store.migrate_to_partitioned()

        assert stats["skipped"] == 1, "读入失败必须计入 skipped"
        assert stats["symbols"] == 0, "失败的 symbol 不应被计为迁移成功"
        assert src.exists(), "源文件必须保留——否则历史数据被不可逆销毁"
        # 损坏的分区也未被覆盖写坏
        assert (dst_dir / "600519.parquet").read_bytes() == b"corrupted-not-a-parquet"

    def test_merges_with_existing_partition_instead_of_overwriting(self, tmp_path):
        """分区已存在时必须合并去重，不能用本次迁移的数据覆盖旧分区。"""
        store = BarStore(root=tmp_path, partitioned=False)
        pd = __import__("pandas")
        # 旧布局：2025 全年
        store.save(_make_bars("600519", pd.bdate_range("2025-01-01", "2025-12-31"), 10.0))
        # 分区里已有 2024 全年（模拟过去迁移过一部分）
        part = BarStore(root=tmp_path, partitioned=True)
        part.save(_make_bars("600519", pd.bdate_range("2024-01-01", "2024-12-31"), 20.0))

        stats = BarStore(root=tmp_path, partitioned=False).migrate_to_partitioned()
        assert stats["symbols"] == 1

        loaded = BarStore(root=tmp_path).load()
        years = set(pd.to_datetime(loaded["date"]).dt.year)
        assert years == {2024, 2025}, f"旧分区数据被覆盖丢失，实际只剩 {years}"
        assert len(loaded) == len(pd.bdate_range("2024-01-01", "2025-12-31"))


# =============================================================================
# 2. 卖出必须回补可取资金
# =============================================================================


class TestCashWithdrawableSymmetry:
    """买入扣减 / 卖出回补对称，否则可取资金单向衰减。"""

    def test_sell_restores_cash_withdrawable(self, tmp_path):
        repo = _repository(tmp_path)
        # 以 08-27 初始化并持有可卖持仓，08-28 起即可卖出
        repo.initialize_account(cash=100_000, positions={"600000": 1_000}, as_of="2026-08-27")
        state = repo.account_state(as_of="2026-08-28")
        assert state is not None

        before = repo.account_state(as_of="2026-08-28")
        assert before is not None

        # 买入：扣减 cash_withdrawable
        repo.record_fill(
            state.account_id,
            FillInput("600000", BUY, 100, 10.0, "2026-08-28", broker_fill_id="buy-1"),
        )
        after_buy = repo.account_state(as_of="2026-08-28")
        assert after_buy is not None
        assert after_buy.cash_withdrawable < before.cash_withdrawable

        # 卖出：必须回补，而不是继续减少或不变
        repo.record_fill(
            state.account_id,
            FillInput("600000", SELL, 100, 12.0, "2026-08-28", broker_fill_id="sell-1"),
        )
        after_sell = repo.account_state(as_of="2026-08-28")
        assert after_sell is not None
        assert after_sell.cash_withdrawable > after_buy.cash_withdrawable, (
            "卖出未回补可取资金：反复买卖后 cash_withdrawable 会单调衰减到 0"
        )

    def test_round_trip_does_not_erode_withdrawable(self, tmp_path):
        """多轮买卖后可取资金不应系统性低于现金总额。"""
        repo = _repository(tmp_path)
        repo.initialize_account(cash=200_000, positions={"600000": 2_000}, as_of="2026-08-27")
        state = repo.account_state(as_of="2026-08-28")
        assert state is not None

        for i in range(5):
            repo.record_fill(
                state.account_id,
                FillInput("600000", SELL, 100, 10.0, "2026-08-28", broker_fill_id=f"s{i}"),
            )
            after = repo.account_state(as_of="2026-08-28")
            assert after is not None
            # 卖出净回笼（1000 - 手续费）应逐次累加进可取
            assert after.cash_withdrawable > 200_000 + i * 900, (
                f"第 {i + 1} 轮卖出后可取资金未正确累加"
            )


# =============================================================================
# 3. 幂等键在 broker_fill_id 为空时不得失效
# =============================================================================


class TestFillIdempotencyWithoutBrokerFillId:
    """券商不返回成交编号是合法场景，两层去重都必须仍然生效。"""

    def test_oms_layer_replay_is_idempotent_without_broker_fill_id(self, tmp_path):
        broker = _broker(tmp_path / "broker.db")
        order = broker.submit_order(_request("client-nofillid", quantity=1000))

        broker.apply_fill(order.client_order_id, 500, 10.0,
                          trade_date="2026-08-31", trade_time="10:00:00")
        # 同一笔回报重放 3 次（无 broker_fill_id）
        for _ in range(3):
            replayed = broker.apply_fill(
                order.client_order_id, 500, 10.0,
                trade_date="2026-08-31", trade_time="10:00:00",
            )
            assert replayed.filled_quantity == 500

        assert len(broker.oms.list_fills(account_id="paper")) == 1, (
            "无 broker_fill_id 时重复回报被重复入账"
        )
        assert broker.positions() == {"600000.SH": 500}

    def test_ledger_layer_rejects_duplicate_without_broker_fill_id(self, tmp_path):
        repo = _repository(tmp_path)
        repo.initialize_account(cash=100_000, positions={}, as_of="2026-08-28")
        state = repo.account_state(as_of="2026-08-31")
        assert state is not None

        fill = FillInput("600000", BUY, 100, 10.0, "2026-08-31")
        assert not fill.broker_fill_id  # 前提：确实没有成交编号
        repo.record_fill(state.account_id, fill)

        with pytest.raises(ValueError, match="成交编号重复"):
            repo.record_fill(state.account_id, FillInput("600000", BUY, 100, 10.0, "2026-08-31"))

        assert len(repo.list_fills()) == 1

    def test_distinct_fills_are_not_mistaken_for_replay(self, tmp_path):
        """不同成交不得被派生键误判为重放（键必须含累计量/时间区分度）。"""
        broker = _broker(tmp_path / "broker.db")
        order = broker.submit_order(_request("client-multi", quantity=1000))

        broker.apply_fill(order.client_order_id, 500, 10.0,
                          trade_date="2026-08-31", trade_time="10:00:00")
        broker.apply_fill(order.client_order_id, 300, 10.1,
                          trade_date="2026-08-31", trade_time="10:05:00")
        filled = broker.apply_fill(order.client_order_id, 200, 10.2,
                                   trade_date="2026-08-31", trade_time="10:10:00")

        assert filled.status is OrderStatus.FILLED
        assert filled.filled_quantity == 1000
        assert len(broker.oms.list_fills(account_id="paper")) == 3


# =============================================================================
# 4. 终态订单的重复回报仍须幂等返回（查重先于状态校验）
# =============================================================================


class TestReplayOnTerminalOrder:
    def test_replay_after_filled_returns_current_state(self, tmp_path):
        """订单已 FILLED 后券商重推同一笔回报：幂等返回，不得抛"状态不可成交"。"""
        broker = _broker(tmp_path / "broker.db")
        order = broker.submit_order(_request("client-terminal", quantity=1000))
        broker.apply_fill(order.client_order_id, 1000, 10.0,
                          trade_date="2026-08-31", trade_time="10:00:00",
                          broker_fill_id="F-terminal")
        assert broker.get_order("client-terminal").status is OrderStatus.FILLED

        replayed = broker.apply_fill(
            order.client_order_id, 1000, 10.0,
            trade_date="2026-08-31", trade_time="10:00:00",
            broker_fill_id="F-terminal",
        )
        assert replayed.status is OrderStatus.FILLED
        assert replayed.filled_quantity == 1000
        assert len(broker.oms.list_fills(account_id="paper")) == 1

    def test_new_fill_on_filled_order_still_rejected(self, tmp_path):
        """幂等的反面：真正的新成交打到终态订单，仍必须报错。"""
        broker = _broker(tmp_path / "broker.db")
        order = broker.submit_order(_request("client-overfill", quantity=1000))
        broker.apply_fill(order.client_order_id, 1000, 10.0,
                          trade_date="2026-08-31", trade_time="10:00:00",
                          broker_fill_id="F-1")
        with pytest.raises(ValueError, match="不可成交"):
            broker.apply_fill(order.client_order_id, 100, 10.0,
                              trade_date="2026-08-31", trade_time="11:00:00",
                              broker_fill_id="F-2")


# =============================================================================
# 5. 静态：源码不得有未定义名（NameError）
# =============================================================================


class TestNoUndefinedNames:
    """未定义名只在执行到该行时才炸，静态扫描能覆盖未执行路径。

    真实事故：`scripts/data_quality_scan.py` 用 `np.inf` 却没导入 numpy；
    同一文件在 `--apply` 的副作用（写阻断清单 + 隔离数据文件）之后才打印
    `QUARANTINE_DIR`，崩溃发生在破坏已完成之后，用户会以为操作没生效。
    """

    @pytest.mark.parametrize(
        "target", ["quart", "scripts", "api", "frontend", "common.py", "app.py"]
    )
    def test_no_f821_undefined_names(self, target):
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", target,
             "--select", "F821", "--output-format", "concise",
             "--no-cache"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            f"{target} 存在未定义名（F821），运行时会抛 NameError:\n"
            f"{proc.stdout}{proc.stderr}"
        )


# =============================================================================
# 6. 静态：生成器函数不得用带值 return 返回错误
# =============================================================================


def _iter_own_nodes(node: ast.AST):
    """遍历节点的 AST，但**不深入**嵌套的函数/lambda/类定义。

    用于区分"本函数是生成器"与"本函数里嵌了个生成器"。
    """
    stack = list(ast.iter_child_nodes(node))
    while stack:
        cur = stack.pop()
        yield cur
        if not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(cur))


def _generator_valued_returns(path: Path) -> list[str]:
    """找出所有"函数体内含 yield，却用 return <value> 返回"的位置。

    生成器里的 `return value` 会变成 StopIteration.value，Gradio 消费
    迭代器时取不到该值——前端表现为"点了按钮没有任何反应"，而错误
    分支恰好是最需要让用户看到内容的路径。
    """
    problems: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return problems

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        own = list(_iter_own_nodes(node))
        if not any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in own):
            continue
        for n in own:
            if isinstance(n, ast.Return) and n.value is not None:
                problems.append(f"{path.name}:{n.lineno} {node.name}()")
    return problems


class TestGeneratorErrorReturnsUseYield:
    def test_frontend_generators_do_not_return_values(self):
        """frontend 下所有生成器回调的错误分支必须用 yield 返回。"""
        roots = [REPO_ROOT / "frontend"]
        problems: list[str] = []
        for root in roots:
            if not root.exists():
                continue
            for py in sorted(root.rglob("*.py")):
                problems.extend(_generator_valued_returns(py))
        assert not problems, (
            "生成器函数用 return <value> 返回，Gradio 拿不到该值（应改 yield）:\n  "
            + "\n  ".join(problems)
        )

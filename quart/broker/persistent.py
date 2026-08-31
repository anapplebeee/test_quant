"""持久化 PaperBroker（BROKER-001，TARGET_ARCHITECTURE_V3 §11 优先路径 1）。

与内存版 `PaperBrokerAdapter` 的区别：

- **单一状态源是 OMS**：订单、回报、成交全部落在 `OrderRepository`，
  Adapter 不再是账户权威源，进程重启后无需"恢复内存"——直接读库；
- **重复回报/重启不重复入账**：所有状态推进都走 `OrderRepository.apply_report`，
  幂等键去重 + 同事务入账由 OMS 保证；
- **故障注入**：`PaperFaultConfig` 模拟报单拒绝（`reject`）与
  报单确认丢失（`drop_ack`，订单停在 SUBMITTING）。超时不是失败结论：
  恢复路径必须先按 `client_order_id` 查询，再决定补发哪一条回报。

内存版 Adapter 保留，用于不接 OMS 的旧路径与单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from quart.broker.models import BrokerOrderRequest, OrderStatus
from quart.domain import (
    BrokerOrder as DomainBrokerOrder,
)
from quart.domain import (
    ExecutionReport,
    RiskDecision,
    apply_execution_report,
    make_execution_report,
    market_datetime,
    new_id,
)
from quart.oms import OrderRepository

_SUBMIT_OUTCOMES = ("ok", "reject", "drop_ack")


@dataclass(frozen=True)
class PaperFaultConfig:
    """故障注入开关（仅 Paper 环境，测试/演练用）。

    - `ok`：正常报单；
    - `reject`：券商拒绝委托（REJECTED 终态落库）；
    - `drop_ack`：报单确认丢失，订单停在 SUBMITTING——
      恢复时必须先按 `client_order_id` 查询，再决定补发回报。
    """

    submit_outcome: str = "ok"

    def __post_init__(self) -> None:
        if self.submit_outcome not in _SUBMIT_OUTCOMES:
            raise ValueError(
                f"未知故障注入模式: {self.submit_outcome}（可选: {', '.join(_SUBMIT_OUTCOMES)}）"
            )


class PersistentPaperBroker:
    """以 OMS 为单一状态源的模拟券商。"""

    def __init__(
        self,
        oms: OrderRepository,
        *,
        account_id: str = "paper",
        fault: PaperFaultConfig | None = None,
    ):
        self.oms = oms
        self.account_id = account_id
        self.fault = fault or PaperFaultConfig()

    # ---------------- 报单 ----------------

    def submit_order(self, request: BrokerOrderRequest) -> DomainBrokerOrder:
        """幂等报单：同 `client_order_id` 重试返回当前订单状态，不产生重复订单。

        已推进过的订单（非 CREATED）直接返回——网络超时不是失败结论，
        调用方应按查询结果决定后续动作。
        """
        normalized = request.normalized()
        assert normalized.client_order_id is not None
        assert normalized.idempotency_key is not None
        intent = normalized.to_order_intent()
        decision = RiskDecision.allow(
            intent,
            limit_version="paper-compat-v1",
            source="PAPER_BROKER",
            reason="模拟盘兼容风控放行",
        )
        order = DomainBrokerOrder.from_intent(
            intent,
            decision,
            client_order_id=normalized.client_order_id,
            source="PAPER_BROKER",
        )
        order = self.oms.create_order(order)
        if order.status is not OrderStatus.CREATED:
            return order

        order = self._advance(order, ExecutionReport.from_risk_decision(order, decision))

        order = self._advance(
            order,
            make_execution_report(
                order,
                status=OrderStatus.SUBMITTING,
                source="PAPER_BROKER",
                idempotency_key=f"{normalized.idempotency_key}:submitting",
            ),
        )
        if self.fault.submit_outcome == "reject":
            return self._advance(
                order,
                make_execution_report(
                    order,
                    status=OrderStatus.REJECTED,
                    source="PAPER_BROKER",
                    idempotency_key=f"{normalized.idempotency_key}:rejected",
                    reason="故障注入：券商拒绝委托",
                ),
            )
        if self.fault.submit_outcome == "drop_ack":
            return order
        broker_order_id = new_id("paper_order")
        return self._advance(
            order,
            make_execution_report(
                order,
                status=OrderStatus.SUBMITTED,
                source="PAPER_BROKER",
                idempotency_key=f"{normalized.idempotency_key}:submitted",
                broker_order_id=broker_order_id,
            ),
        )

    def confirm_submitted(self, client_order_id: str, broker_order_id: str | None = None) -> DomainBrokerOrder:
        """查询确认报单已送达后补发 SUBMITTED（drop_ack 恢复路径）。"""
        order = self._required_order(client_order_id)
        if order.status is OrderStatus.SUBMITTED or order.is_terminal:
            return order
        if order.status is not OrderStatus.SUBMITTING:
            raise ValueError(f"订单当前状态不能补发报单确认: {order.status}")
        return self._advance(
            order,
            make_execution_report(
                order,
                status=OrderStatus.SUBMITTED,
                source="PAPER_BROKER",
                idempotency_key=f"{order.idempotency_key}:submitted",
                broker_order_id=broker_order_id or new_id("paper_order"),
            ),
        )

    # ---------------- 成交与撤单 ----------------

    def apply_fill(
        self,
        client_order_id: str,
        quantity: int,
        price: float,
        trade_date: str | None = None,
        trade_time: str | None = None,
        broker_fill_id: str | None = None,
    ) -> DomainBrokerOrder:
        """按回报入账成交；同 `broker_fill_id` 重复回报幂等不重复入账。"""
        order = self._required_order(client_order_id)
        fill_key = broker_fill_id or new_id("paper_fill")
        report_key = f"paper-fill:{fill_key}"
        if broker_fill_id is not None and any(
            r["idempotency_key"] == report_key for r in self.oms.list_reports(client_order_id)
        ):
            return order  # 重复回报幂等重放：返回当前状态，不再次入账
        if order.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
            raise ValueError(f"订单当前状态不可成交: {order.status}")
        if quantity <= 0 or price <= 0:
            raise ValueError("成交数量和价格必须为正")
        if quantity > order.remaining_quantity:
            raise ValueError("成交数量超过订单剩余数量")
        new_filled = order.filled_quantity + quantity
        next_status = (
            OrderStatus.FILLED
            if new_filled == order.approved_quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        report = make_execution_report(
            order,
            status=next_status,
            source="PAPER_BROKER",
            idempotency_key=report_key,
            cumulative_filled_quantity=new_filled,
            last_filled_quantity=quantity,
            last_fill_price=price,
            broker_order_id=order.broker_order_id,
            business_time=market_datetime(trade_date or date.today().isoformat(), trade_time),
        )
        return self._advance(order, report)

    def cancel_order(self, client_order_id: str) -> DomainBrokerOrder:
        order = self._required_order(client_order_id)
        if order.status is OrderStatus.CANCELED:
            return order  # 重复撤单请求幂等返回
        if order.is_terminal:
            raise ValueError(f"订单当前状态不可撤销: {order.status}")
        return self._advance(
            order,
            make_execution_report(
                order,
                status=OrderStatus.CANCELED,
                source="PAPER_BROKER",
                idempotency_key=f"{order.idempotency_key}:cancel:{order.version}",
                reason="模拟撤单",
            ),
        )

    # ---------------- 查询与恢复 ----------------

    def get_order(self, client_order_id: str) -> DomainBrokerOrder | None:
        """按 `client_order_id` 查询（超时恢复的第一步）。"""
        return self.oms.get_order(client_order_id)

    def active_orders(self) -> list[DomainBrokerOrder]:
        """重启恢复入口：本账户全部非终态订单。"""
        return [
            o for o in self.oms.list_active_orders() if o.account_id == self.account_id
        ]

    def positions(self) -> dict[str, int]:
        """成交账本派生的持仓查询模型（不是账户权威源）。"""
        return self.oms.positions_from_fills(self.account_id)

    # ---------------- 内部 ----------------

    def _advance(self, order: DomainBrokerOrder, report: ExecutionReport) -> DomainBrokerOrder:
        """先本地推进校验，再经 OMS 落库；以 OMS 返回为准（幂等重放安全）。"""
        apply_execution_report(order, report)
        return self.oms.apply_report(report)

    def _required_order(self, client_order_id: str) -> DomainBrokerOrder:
        order = self.oms.get_order(client_order_id)
        if order is None:
            raise KeyError(f"订单不存在: {client_order_id}")
        return order


__all__ = ["PaperFaultConfig", "PersistentPaperBroker"]

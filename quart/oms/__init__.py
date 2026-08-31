"""持久化订单状态机（OMS-001，TARGET_ARCHITECTURE_V3 §5.2）。

- 订单、回报、成交全部持久化，状态只能由 `ExecutionReport` 推进；
- 回报与成交按幂等键去重：重复回报/重启不重复入账；
- 领域合同（`BrokerOrder`/`ExecutionReport`/`Fill`）在 `quart/domain`，
  本包只负责持久化与恢复。
"""
from quart.oms.oms_schema import OMS_MIGRATIONS
from quart.oms.store import OrderRepository

__all__ = ["OMS_MIGRATIONS", "OrderRepository"]

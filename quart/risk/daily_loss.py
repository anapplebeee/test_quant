"""账户日损守卫（RISK-002）。

日损不能由某一笔订单临时估算：它必须从日初权益开始，按交易日持续留痕，
并在触发时写入风险状态机。本模块只定义计算和状态迁移合同；SQLite 持久化
由 :mod:`quart.risk.store` 实现，避免风控逻辑依赖具体账本。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Protocol

import pandas as pd

from quart.risk.engine import RiskLimits, RiskState


@dataclass(frozen=True, slots=True)
class DailyEquityMark:
    """账户某交易日的日初/当前权益观测，作为下一日的 PIT 基线。"""

    account_id: str
    trade_date: date
    opening_equity: float
    current_equity: float
    daily_loss_pct: float
    baseline_date: date | None
    baseline_available: bool
    limit_version: str
    triggered_state: RiskState | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["trade_date"] = self.trade_date.isoformat()
        data["baseline_date"] = self.baseline_date.isoformat() if self.baseline_date else None
        data["triggered_state"] = self.triggered_state.value if self.triggered_state else None
        return data


@dataclass(frozen=True, slots=True)
class DailyLossAssessment:
    """一次日损评估的完整审计结果。"""

    mark: DailyEquityMark
    threshold: float
    state_before: RiskState
    state_after: RiskState
    triggered: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            **self.mark.to_dict(),
            "threshold": self.threshold,
            "state_before": self.state_before.value,
            "state_after": self.state_after.value,
            "triggered": self.triggered,
            "reason": self.reason,
        }


class DailyLossStore(Protocol):
    """日损守卫所需的最小持久化合同。"""

    def get_state(self, account_id: str) -> RiskState: ...

    def set_state(
        self,
        account_id: str,
        new_state: RiskState | str,
        *,
        reason: str = "",
        operator: str = "",
        limit_version: str = "",
    ) -> RiskState: ...

    def get_daily_mark(self, account_id: str, trade_date: date) -> DailyEquityMark | None: ...

    def latest_daily_mark_before(
        self, account_id: str, trade_date: date
    ) -> DailyEquityMark | None: ...

    def upsert_daily_mark(self, mark: DailyEquityMark) -> DailyEquityMark: ...


class DailyLossGuard:
    """用已持久化的日初权益评估日损，并在触发时熔断账户。"""

    rule_id = "daily_loss"

    def __init__(self, limits: RiskLimits, store: DailyLossStore):
        self.limits = limits
        self.store = store

    @staticmethod
    def _coerce_date(value: str | date | pd.Timestamp) -> date:
        return pd.Timestamp(value).date()

    @staticmethod
    def _validate_equity(value: float, field: str, *, positive: bool) -> float:
        number = float(value)
        if not math.isfinite(number) or (number <= 0 if positive else number < 0):
            comparator = "正数" if positive else "非负数"
            raise ValueError(f"{field} 必须是有限{comparator}")
        return number

    def evaluate(
        self,
        account_id: str,
        trade_date: str | date | pd.Timestamp,
        current_equity: float,
        *,
        opening_equity: float | None = None,
    ) -> DailyLossAssessment:
        """评估并写入日损观测。

        首次接入若未提供 ``opening_equity``，只初始化基线，不会把未知日初
        权益伪装成 0 损益；下一交易日使用该日终权益作为日初基线。重跑同一
        交易日会复用既有日初权益，保证结果可重现。
        """
        account = str(account_id).strip()
        if not account:
            raise ValueError("account_id 不能为空")
        day = self._coerce_date(trade_date)
        # 权益为零/负值时无法形成可交易的账户快照；由调用方 fail-closed，
        # 不能把它当作首次基线而继续生成订单。
        current = self._validate_equity(current_equity, "current_equity", positive=True)
        existing = self.store.get_daily_mark(account, day)

        baseline_available = True
        baseline_date: date | None
        if existing is not None:
            opening = self._validate_equity(existing.opening_equity, "已存日初权益", positive=True)
            baseline_date = existing.baseline_date
            baseline_available = existing.baseline_available
        elif opening_equity is not None:
            opening = self._validate_equity(opening_equity, "opening_equity", positive=True)
            baseline_date = day
        else:
            previous = self.store.latest_daily_mark_before(account, day)
            if previous is None:
                # 无法知道首次接入日的日初权益；显式记录“无基线”，而非静默放行。
                opening = max(current, 1.0)
                baseline_date = None
                baseline_available = False
            else:
                opening = self._validate_equity(previous.current_equity, "前一日权益", positive=True)
                baseline_date = previous.trade_date

        daily_loss_pct = max(0.0, (opening - current) / opening)
        state_before = self.store.get_state(account)
        triggered = baseline_available and daily_loss_pct >= self.limits.max_daily_loss_pct
        state_after = state_before
        reason = ""
        if triggered:
            reason = (
                f"日损 {daily_loss_pct:.2%} 触发阈值 {self.limits.max_daily_loss_pct:.2%}"
                f"（日初权益 {opening:.2f}，当前权益 {current:.2f}）"
            )
            if state_before is not RiskState.HALTED:
                state_after = self.store.set_state(
                    account,
                    RiskState.HALTED,
                    reason=reason,
                    operator="daily_loss_guard",
                    limit_version=self.limits.version(),
                )

        mark = DailyEquityMark(
            account_id=account,
            trade_date=day,
            opening_equity=opening,
            current_equity=current,
            daily_loss_pct=daily_loss_pct,
            baseline_date=baseline_date,
            baseline_available=baseline_available,
            limit_version=self.limits.version(),
            triggered_state=state_after if triggered else None,
        )
        persisted = self.store.upsert_daily_mark(mark)
        return DailyLossAssessment(
            mark=persisted,
            threshold=self.limits.max_daily_loss_pct,
            state_before=state_before,
            state_after=state_after,
            triggered=triggered,
            reason=reason,
        )


__all__ = ["DailyEquityMark", "DailyLossAssessment", "DailyLossGuard", "DailyLossStore"]

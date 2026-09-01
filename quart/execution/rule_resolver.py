"""执行层对 RuleBook 与 SecurityMaster 的统一适配（RULE-002）。

订单生成器只关心“当前订单能否成交、最小单位是多少”；规则书与证券状态的
查询细节集中在这里，避免回测、信号和 Paper 各自按代码前缀猜涨跌停。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from quart.data.calendar import TradingCalendar
from quart.data.security_master import MASTER_PATH, SecurityMaster
from quart.execution.constraints import LIMIT_TOLERANCE
from quart.execution.models import BUY
from quart.market_rules.rule_book import RULE_BOOK_PATH, RuleBook, RuleSet, default_rule_book

_A_SHARE_CODE = re.compile(r"^\d{6}$")


@dataclass(frozen=True)
class ResolvedTradeRule:
    """某证券在某一交易日的规则解析结果，便于审计与测试。"""

    symbol: str
    trade_date: pd.Timestamp
    status: str
    trading_age: int | None
    ruleset: RuleSet | None


def _load_active_rule_book(path: Path | None = None) -> RuleBook:
    target = path or RULE_BOOK_PATH
    return RuleBook.load(target) if target.exists() else default_rule_book()


def _load_security_master(path: Path | None = None) -> SecurityMaster | None:
    target = path or MASTER_PATH
    return SecurityMaster.load(target) if target.exists() else None


class ExecutionRuleResolver:
    """根据执行日期、证券状态和交易日龄解析可执行交易规则。"""

    def __init__(
        self,
        rule_book: RuleBook | None = None,
        security_master: SecurityMaster | None = None,
        calendar: TradingCalendar | None = None,
        *,
        autoload_security_master: bool = False,
    ):
        self.rule_book = rule_book or _load_active_rule_book()
        self.security_master = security_master
        if self.security_master is None and autoload_security_master:
            self.security_master = _load_security_master()
        self.calendar = calendar or TradingCalendar.from_csv()

    @property
    def version(self) -> str:
        return self.rule_book.version()

    @staticmethod
    def _status_for(record: dict | None) -> str:
        status = str((record or {}).get("status", "listed")).lower()
        if status in {"st", "listed", "delisting"}:
            return status
        if status in {"delisted", "delist"}:
            return "delisting"
        return "listed"

    def _trading_age(self, symbol: str, trade_date: pd.Timestamp) -> int | None:
        if self.security_master is None:
            return None
        rows = self.security_master.table[self.security_master.table["symbol"] == symbol]
        if rows.empty or rows["listed_at"].isna().all():
            return None
        listed_at = pd.Timestamp(rows["listed_at"].dropna().min()).normalize()
        if listed_at > trade_date:
            return None
        if self.calendar.has_cache:
            sessions = [date for date in self.calendar.dates if listed_at.date() <= date <= trade_date.date()]
            return max(0, len(sessions) - 1)
        # 缓存缺失时只作工作日近似；formal 的 PIT 数据门禁会记录日历覆盖。
        return max(0, len(pd.bdate_range(listed_at, trade_date)) - 1)

    def _lifecycle_status(self, symbol: str, trade_date: pd.Timestamp) -> str | None:
        """返回主数据可明确判定的不可交易生命周期状态。

        ``status_as_of`` 负责 ST/退市整理等规则状态，但一条覆盖全历史的
        ``listed`` 状态记录本身不会因 ``delisted_at`` 自动失效。因此这里
        单独检查上市和退市边界，确保回测不会用今日仍可见的主数据交易历史
        上尚未上市或已经退市的证券。
        """
        if self.security_master is None:
            return None
        rows = self.security_master.table[self.security_master.table["symbol"] == symbol]
        if rows.empty:
            return None
        listed = rows["listed_at"].dropna()
        if not listed.empty and trade_date < pd.Timestamp(listed.min()).normalize():
            return "not_listed"
        delisted = rows["delisted_at"].dropna()
        if not delisted.empty and trade_date >= pd.Timestamp(delisted.min()).normalize():
            return "delisted"
        return None

    def resolve(self, symbol: str, trade_date: str | pd.Timestamp) -> ResolvedTradeRule:
        code = str(symbol).split(".")[0].zfill(6)
        ts = pd.Timestamp(trade_date).normalize()
        lifecycle = self._lifecycle_status(code, ts)
        record = self.security_master.status_as_of(code, ts) if self.security_master is not None else None
        status = lifecycle or self._status_for(record)
        ruleset = None if lifecycle else self.rule_book.resolve_symbol(code, ts, status=status)
        return ResolvedTradeRule(
            symbol=code,
            trade_date=ts,
            status=status,
            trading_age=self._trading_age(code, ts),
            ruleset=ruleset,
        )

    def lot_size(self, symbol: str, trade_date: str | pd.Timestamp, fallback: int = 100) -> int:
        resolved = self.resolve(symbol, trade_date)
        return int(resolved.ruleset.lot_size) if resolved.ruleset is not None else int(fallback)

    def blocked_reason(
        self,
        symbol: str,
        side: str,
        base_price: float,
        prev_close: float,
        trade_date: str | pd.Timestamp,
    ) -> str | None:
        """给出 A 股日线撮合的规则性拒单原因；None 表示规则允许成交。"""
        code = str(symbol).split(".")[0]
        if not _A_SHARE_CODE.fullmatch(code):
            return None  # 合成测试符号由旧兼容规则处理
        if not math.isfinite(prev_close) or not math.isfinite(base_price):
            return None
        resolved = self.resolve(code, trade_date)
        if resolved.status == "not_listed":
            return "证券尚未上市，委托无法成交"
        if resolved.status == "delisted":
            return "证券已退市，委托无法成交"
        if resolved.ruleset is None:
            return f"RuleBook 未覆盖 {code} 在 {resolved.trade_date.date()} 的交易规则"
        limits = self.rule_book.price_limits(
            resolved.ruleset,
            prev_close,
            trading_age=resolved.trading_age,
        )
        if limits is None:
            return None
        upper, lower = limits
        if side == BUY and base_price >= upper - LIMIT_TOLERANCE:
            return f"开盘涨停（RuleBook {resolved.status}），买单无法成交"
        if side != BUY and base_price <= lower + LIMIT_TOLERANCE:
            return f"开盘跌停（RuleBook {resolved.status}），卖单无法成交"
        return None

    def limit_note(
        self,
        symbol: str,
        side: str,
        base_price: float,
        prev_close: float,
        trade_date: str | pd.Timestamp,
    ) -> str | None:
        """实盘计划用的非阻塞提示，和回测共用同一规则解析。"""
        reason = self.blocked_reason(symbol, side, base_price, prev_close, trade_date)
        if reason is None:
            return None
        if side == BUY and "涨停" in reason:
            return f"{symbol}: {reason}，次日可能开板，请人工确认"
        if side != BUY and "跌停" in reason:
            return f"{symbol}: {reason}，次日可能开板，请人工确认"
        return f"{symbol}: {reason}"


__all__ = ["ExecutionRuleResolver", "ResolvedTradeRule"]

"""A 股交易规则 RuleBook（RULE-001，对应 TARGET_ARCHITECTURE_V3 §8）。

定位
----
涨跌停、新股阶段、整手/最小价位、结算与费用等规则**按日期生效**：
同一证券在不同历史日期适用不同规则。此前这些规则按代码前缀静态推断
（`quart/execution/constraints.py`），无法表达创业板注册制改革、
北交所开市等历史变更，也无法被风控/发单链路引用版本。

规则查询键（§8）::

    trade_date + exchange + board + security_type + status

回测撮合、交易计划、事前风控和券商发单前校验必须调用同一规则引擎
（Phase B 验收标准）。本模块只做规则解析，不自行判断停牌/ST 等
数据驱动状态 —— 那是 SecurityMaster 与数据质量模块的职责。

v1 覆盖（`default_rule_book`）
------------------------------
- 主板 10%（ST 5%）；2023-04-10 前新股首日 +44%/-36%，
  之后（全面注册制）新股前 5 个交易日无涨跌幅；
- 创业板 2020-08-24 前 10%、起 20%（注册制改革），改革后新股前 5 日无涨跌幅；
- 科创板 20%（2019-07-22 开市起），新股前 5 日无涨跌幅；
- 北交所 30%（2021-11-15 开市起），新股前 5 日无涨跌幅；
- 退市整理期（status="delisting"）沿用板块涨跌幅（v1 口径）；
- 整手 100 股、最小价位 0.01 元、T+1 结算、买入整手/零股卖出；
- 费用历史：印花税 2023-08-28 起卖出万 5（原千 1）。

区间合同：``effective_from`` 含、``effective_to`` 不含；``None`` 表示无界。
"""
from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from quart.config import data_root

RULE_BOOK_PATH = Path(data_root()) / "meta" / "rule_book.json"

#: 主板注册制改革生效日（首批主板注册制新股上市）
MAIN_BOARD_REGISTRATION_REFORM = pd.Timestamp("2023-04-10")
#: 创业板注册制改革生效日（存量股涨跌幅 10%→20%）
CHINEXT_REGISTRATION_REFORM = pd.Timestamp("2020-08-24")
#: 科创板开市日
STAR_MARKET_OPEN = pd.Timestamp("2019-07-22")
#: 北交所开市日
BSE_OPEN = pd.Timestamp("2021-11-15")
#: 印花税下调生效日（卖出千 1 → 万 5）
STAMP_TAX_CUT = pd.Timestamp("2023-08-28")


@dataclass(frozen=True)
class RuleSet:
    """某一 (exchange, board, security_type, status) 在生效区间内的规则集。"""

    exchange: str
    board: str
    security_type: str
    status: str
    effective_from: pd.Timestamp | None
    effective_to: pd.Timestamp | None
    price_limit_pct: float
    lot_size: int = 100
    tick_size: float = 0.01
    settlement_rule: str = "T+1"
    buy_in_lots: bool = True
    odd_lot_sell: bool = True
    #: 上市后前 N 个交易日无涨跌幅（0 = 上市首日即受限）
    ipo_no_limit_days: int = 0
    #: 首日特殊涨跌幅 (上限, 下限)，如旧主板新股 +44%/-36%；None = 无
    ipo_first_day_limits: tuple[float, float] | None = None
    notes: str = ""

    def covers(self, ts: pd.Timestamp) -> bool:
        start_ok = self.effective_from is None or self.effective_from <= ts
        end_ok = self.effective_to is None or ts < self.effective_to
        return start_ok and end_ok

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("effective_from", "effective_to"):
            value = getattr(self, key)
            d[key] = None if value is None else str(pd.Timestamp(value).date())
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RuleSet:
        d = dict(d)
        for key in ("effective_from", "effective_to"):
            d[key] = None if d[key] is None else pd.Timestamp(d[key])
        limits = d.get("ipo_first_day_limits")
        d["ipo_first_day_limits"] = None if limits is None else (float(limits[0]), float(limits[1]))
        return cls(**d)


class RuleBook:
    """按日期版本化的交易规则集合。

    不变量：同一查询键下生效区间互不重叠（`validate()` 强制）。
    """

    def __init__(self, rules: list[RuleSet]):
        self.rules = sorted(
            rules,
            key=lambda r: (r.exchange, r.board, r.security_type, r.status,
                           r.effective_from or pd.Timestamp.min),
        )

    # ---------------- 查询 ----------------

    def lookup(
        self,
        trade_date: str | pd.Timestamp,
        *,
        exchange: str,
        board: str,
        security_type: str = "stock",
        status: str = "listed",
    ) -> RuleSet | None:
        """按查询键解析该日期生效的规则集；无记录返回 None。

        歧义（区间重叠）是数据错误，抛 ValueError 而不是任选其一 ——
        正确性优先（§2.1）。
        """
        ts = pd.Timestamp(trade_date)
        hits = [
            r for r in self.rules
            if r.exchange == exchange
            and r.board == board
            and r.security_type == security_type
            and r.status == status
            and r.covers(ts)
        ]
        if not hits:
            return None
        if len(hits) > 1:
            raise ValueError(
                f"overlapping rules for {exchange}/{board}/{security_type}/{status} "
                f"at {ts.date()}: {[h.notes for h in hits]}"
            )
        return hits[0]

    def resolve_symbol(
        self,
        symbol: str,
        trade_date: str | pd.Timestamp,
        *,
        security_type: str = "stock",
        status: str = "listed",
    ) -> RuleSet | None:
        """按代码前缀推导板块后查询（与 SecurityMaster._board_of 同源）。"""
        from quart.data.security_master import _board_of

        exchange, board, sec_type, _ = _board_of(symbol)
        if sec_type != security_type and security_type == "stock":
            return None
        return self.lookup(
            trade_date,
            exchange=exchange,
            board=board,
            security_type=sec_type,
            status=status,
        )

    # ---------------- 涨跌幅解析 ----------------

    def price_limits(
        self,
        ruleset: RuleSet,
        prev_close: float,
        *,
        trading_age: int | None = None,
    ) -> tuple[float, float] | None:
        """返回 (涨停价, 跌停价)；新股无涨跌幅阶段返回 None。

        Parameters
        ----------
        trading_age
            上市后经过的交易日数（0 = 上市首日）。未知时传 None，
            按非新股处理 —— 调用方若需要新股正确性必须提供该值。
        """
        if pd.isna(prev_close) or prev_close <= 0:
            return None
        if trading_age is not None and trading_age < ruleset.ipo_no_limit_days:
            return None  # 无涨跌幅阶段
        if trading_age == 0 and ruleset.ipo_first_day_limits is not None:
            up_pct, down_pct = ruleset.ipo_first_day_limits
        else:
            up_pct = down_pct = ruleset.price_limit_pct
        up = round(prev_close * (1 + up_pct) + 1e-9, 2)
        down = round(prev_close * (1 - down_pct) - 1e-9, 2)
        return up, max(down, ruleset.tick_size)

    # ---------------- 校验 ----------------

    def validate(self) -> list[str]:
        """区间重叠/字段合法性校验，返回问题列表（空 = 通过）。"""
        problems: list[str] = []
        grouped: dict[tuple[str, str, str, str], list[RuleSet]] = {}
        for r in self.rules:
            grouped.setdefault((r.exchange, r.board, r.security_type, r.status), []).append(r)
        for key, recs in grouped.items():
            recs = sorted(recs, key=lambda r: r.effective_from or pd.Timestamp.min)
            for a, b in itertools.pairwise(recs):
                # 已按起点排序：a 与 b 重叠 ⟺ a 无右界或 a 的右界晚于 b 的起点
                if (
                    a.effective_to is None
                    or b.effective_from is None
                    or a.effective_to > b.effective_from
                ):
                    at = b.effective_from.date() if b.effective_from is not None else "-inf"
                    problems.append(f"overlap: {key} at {at}")
        for r in self.rules:
            if r.price_limit_pct < 0 or r.price_limit_pct > 1:
                problems.append(f"bad price_limit_pct: {r.exchange}/{r.board} {r.price_limit_pct}")
            if r.lot_size <= 0 or r.tick_size <= 0:
                problems.append(f"bad lot/tick: {r.exchange}/{r.board}")
            if (
                r.effective_from is not None
                and r.effective_to is not None
                and r.effective_from >= r.effective_to
            ):
                problems.append(f"empty interval: {r.exchange}/{r.board}/{r.status}")
        return problems

    # ---------------- 版本与落盘 ----------------

    def version(self) -> str:
        """规则内容哈希（rule_book_version，16 hex）。

        任何规则（含历史区间）变化都会改变版本 —— 与快照内容哈希同原则。
        """
        canonical = json.dumps(
            [r.to_dict() for r in self.rules], ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def to_json(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.rules]

    @classmethod
    def from_json(cls, records: list[dict[str, Any]]) -> RuleBook:
        return cls([RuleSet.from_dict(d) for d in records])

    def save(self, path: Path | None = None) -> Path:
        p = path or RULE_BOOK_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"rules": self.to_json(), "version": self.version()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("rule book saved: {} rules -> {}", len(self.rules), p)
        return p

    @classmethod
    def load(cls, path: Path | None = None) -> RuleBook:
        p = path or RULE_BOOK_PATH
        if not p.exists():
            raise FileNotFoundError(f"rule book not found: {p}")
        payload = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_json(payload["rules"])


# ---------------- v1 默认规则集 ----------------


def _stock(exchange: str, board: str, status: str,
           effective_from: pd.Timestamp | None, effective_to: pd.Timestamp | None,
           limit: float, **kw: Any) -> RuleSet:
    return RuleSet(
        exchange=exchange, board=board, security_type="stock", status=status,
        effective_from=effective_from, effective_to=effective_to,
        price_limit_pct=limit, **kw,
    )


def default_rule_book() -> RuleBook:
    """A 股 v1 规则集（含历史变更，见模块 docstring）。"""
    rules: list[RuleSet] = []

    # ---- 主板（SSE 60 / SZSE 00）：10%；ST 5% ----
    for exchange in ("SSE", "SZSE"):
        rules += [
            # 注册制改革前：新股首日 +44%/-36%，次日起 10%
            _stock(exchange, "MAIN", "listed", None, MAIN_BOARD_REGISTRATION_REFORM, 0.10,
                   ipo_first_day_limits=(0.44, 0.36),
                   notes="主板核准制时期：首日 44%/36%，其后 10%"),
            _stock(exchange, "MAIN", "listed", MAIN_BOARD_REGISTRATION_REFORM, None, 0.10,
                   ipo_no_limit_days=5,
                   notes="主板全面注册制：新股前 5 个交易日无涨跌幅"),
            _stock(exchange, "MAIN", "st", None, MAIN_BOARD_REGISTRATION_REFORM, 0.05,
                   ipo_first_day_limits=(0.44, 0.36),
                   notes="主板 ST 风险警示：5%"),
            _stock(exchange, "MAIN", "st", MAIN_BOARD_REGISTRATION_REFORM, None, 0.05,
                   ipo_no_limit_days=5,
                   notes="主板注册制时期 ST：5%"),
            _stock(exchange, "MAIN", "delisting", None, None, 0.10,
                   notes="退市整理期沿用板块涨跌幅（v1 口径）"),
        ]

    # ---- 创业板（SZSE 30）：2020-08-24 注册制改革 10% → 20% ----
    rules += [
        _stock("SZSE", "CHINEXT", "listed", None, CHINEXT_REGISTRATION_REFORM, 0.10,
               ipo_first_day_limits=(0.44, 0.36),
               notes="创业板核准制时期：10%，首日 44%/36%"),
        _stock("SZSE", "CHINEXT", "listed", CHINEXT_REGISTRATION_REFORM, None, 0.20,
               ipo_no_limit_days=5,
               notes="创业板注册制：20%，新股前 5 日无涨跌幅"),
        _stock("SZSE", "CHINEXT", "st", None, CHINEXT_REGISTRATION_REFORM, 0.05,
               notes="创业板改革前 ST：5%"),
        _stock("SZSE", "CHINEXT", "st", CHINEXT_REGISTRATION_REFORM, None, 0.20,
               notes="创业板注册制后 ST：20%"),
        _stock("SZSE", "CHINEXT", "delisting", None, None, 0.20,
               notes="退市整理期（v1 口径）"),
    ]

    # ---- 科创板（SSE 68）：开市即 20%，新股前 5 日无涨跌幅 ----
    rules += [
        _stock("SSE", "STAR", "listed", STAR_MARKET_OPEN, None, 0.20,
               ipo_no_limit_days=5, notes="科创板：20%，新股前 5 日无涨跌幅"),
        _stock("SSE", "STAR", "st", STAR_MARKET_OPEN, None, 0.20,
               notes="科创板 ST：20%"),
        _stock("SSE", "STAR", "delisting", STAR_MARKET_OPEN, None, 0.20,
               notes="退市整理期（v1 口径）"),
    ]

    # ---- 北交所（43/83/87/88/92）：开市即 30%，新股前 5 日无涨跌幅 ----
    rules += [
        _stock("BSE", "BSE", "listed", BSE_OPEN, None, 0.30,
               ipo_no_limit_days=5, notes="北交所：30%，新股前 5 日无涨跌幅"),
        _stock("BSE", "BSE", "st", BSE_OPEN, None, 0.30,
               notes="北交所 ST：30%"),
        _stock("BSE", "BSE", "delisting", BSE_OPEN, None, 0.30,
               notes="退市整理期（v1 口径）"),
    ]

    book = RuleBook(rules)
    problems = book.validate()
    if problems:
        raise ValueError(f"default rule book invalid: {problems}")
    return book


# ---------------- 费用规则（历史变更） ----------------


@dataclass(frozen=True)
class FeeRule:
    """按日期生效的费用规则（§8"费用规则及历史变更"）。"""

    effective_from: pd.Timestamp | None
    effective_to: pd.Timestamp | None
    stamp_tax_rate: float
    notes: str = ""

    def covers(self, ts: pd.Timestamp) -> bool:
        start_ok = self.effective_from is None or self.effective_from <= ts
        end_ok = self.effective_to is None or ts < self.effective_to
        return start_ok and end_ok


#: 印花税历史：2023-08-28 前卖出千 1，其后万 5
FEE_RULES: tuple[FeeRule, ...] = (
    FeeRule(None, STAMP_TAX_CUT, 0.001, "印花税卖出千 1"),
    FeeRule(STAMP_TAX_CUT, None, 0.0005, "2023-08-28 起印花税卖出万 5"),
)


def stamp_tax_as_of(trade_date: str | pd.Timestamp) -> float:
    """该日期适用的印花税率（仅卖出征收）。"""
    ts = pd.Timestamp(trade_date)
    for rule in FEE_RULES:
        if rule.covers(ts):
            return rule.stamp_tax_rate
    raise ValueError(f"no fee rule covers {ts.date()}")


def load_rule_book_version(path: Path | None = None) -> str:
    """读取规则书版本；文件不存在时用默认规则集（供快照 PIT 元数据）。"""
    p = path or RULE_BOOK_PATH
    if p.exists():
        return RuleBook.load(p).version()
    return default_rule_book().version()


__all__ = [
    "BSE_OPEN",
    "CHINEXT_REGISTRATION_REFORM",
    "FEE_RULES",
    "MAIN_BOARD_REGISTRATION_REFORM",
    "RULE_BOOK_PATH",
    "STAMP_TAX_CUT",
    "STAR_MARKET_OPEN",
    "FeeRule",
    "RuleBook",
    "RuleSet",
    "default_rule_book",
    "load_rule_book_version",
    "stamp_tax_as_of",
]

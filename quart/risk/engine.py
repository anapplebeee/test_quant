"""强制 Risk Engine 与风险状态机（RISK-001，TARGET_ARCHITECTURE_V3 §9）。

设计要点
--------
- **风险状态机**：`ACTIVE → REDUCING → HALTED → RECOVERY → ACTIVE`，
  状态持久化在 `risk_states`（切换历史进 `risk_state_history`）；
- **强制链路**：正式信号与实盘订单不能关闭 Risk Engine
  （`require_risk_engine` 在 paper/live 环境缺失引擎时直接抛错）；
- **可审计决策**：每笔 `RiskDecision` 携带 `limit_version`（限额内容哈希）、
  全部规则结果与原因，落库 `risk_decisions`（按幂等键去重）；
- **一致性**：权重级（回测 `risk_pipeline` / 信号 `validate_weights`）与
  订单意图级（paper/live 下单前）共享同一 `RiskLimits` 与同一限额语义。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import pandas as pd
from loguru import logger

from quart.domain.enums import OrderSide, RiskRuleOutcome, TradingEnvironment
from quart.domain.orders import OrderIntent, RiskDecision, RiskRuleResult
from quart.domain.time import SHANGHAI_TZ, require_aware

if TYPE_CHECKING:
    from quart.market_rules.rule_book import RuleBook, RuleSet


class DecisionRecorder(Protocol):
    """决策持久化接口（由 quart.risk.store.RiskRepository 实现）。"""

    def record_decision(self, decision: RiskDecision) -> RiskDecision: ...


class RiskState(StrEnum):
    """账户级风险状态（TARGET_ARCHITECTURE_V3 §9）。"""

    ACTIVE = "ACTIVE"      # 正常接受增减仓
    REDUCING = "REDUCING"  # 只允许降低风险（卖出/降敞口）
    HALTED = "HALTED"      # 禁止新增和修改订单，允许撤单与查询
    RECOVERY = "RECOVERY"  # 对账、数据和人工复核后才能恢复

    @classmethod
    def coerce(cls, value: RiskState | str) -> RiskState:
        if isinstance(value, cls):
            return value
        return cls(str(value).strip().upper())


#: 合法状态迁移（其余一律拒绝，恢复路径必须经过 RECOVERY 人工复核）
ALLOWED_TRANSITIONS: dict[RiskState, frozenset[RiskState]] = {
    RiskState.ACTIVE: frozenset({RiskState.REDUCING, RiskState.HALTED}),
    RiskState.REDUCING: frozenset({RiskState.ACTIVE, RiskState.HALTED}),
    RiskState.HALTED: frozenset({RiskState.RECOVERY}),
    RiskState.RECOVERY: frozenset({RiskState.ACTIVE, RiskState.HALTED}),
}


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """风控限额（内容哈希即 `limit_version`，改动任何字段都会换版本）。"""

    max_position_pct: float
    max_daily_loss_pct: float = 0.05
    max_gross_exposure_pct: float = 1.0

    def __post_init__(self) -> None:
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("max_position_pct 必须在 (0, 1]")
        if not 0 < self.max_daily_loss_pct <= 1:
            raise ValueError("max_daily_loss_pct 必须在 (0, 1]")
        if not 0 < self.max_gross_exposure_pct <= 2:
            raise ValueError("max_gross_exposure_pct 必须在 (0, 2]")

    def version(self) -> str:
        canonical = json.dumps(
            {
                "max_position_pct": self.max_position_pct,
                "max_daily_loss_pct": self.max_daily_loss_pct,
                "max_gross_exposure_pct": self.max_gross_exposure_pct,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def limits_from_config(cfg: Mapping) -> RiskLimits:
    """从 settings 的 `risk:` 段构建限额（缺省字段用平台默认值）。"""
    risk = cfg.get("risk", {}) if isinstance(cfg, Mapping) else {}
    return RiskLimits(
        max_position_pct=float(risk.get("max_position_pct", 0.25)),
        max_daily_loss_pct=float(risk.get("max_daily_loss_pct", 0.05)),
        max_gross_exposure_pct=float(risk.get("max_gross_exposure_pct", 1.0)),
    )


# ---------------------------------------------------------------------------
# 权重级评估（回测与信号共用；quart/risk/rules.py 委托到这里）
# ---------------------------------------------------------------------------


def evaluate_weights(
    limits: RiskLimits,
    targets: Mapping[str, float],
    latest_close: pd.Series,
    equity: float,
) -> tuple[dict[str, float], list[RiskRuleResult]]:
    """单票上限 + 总权重归一的权重级风控（单一事实来源）。

    与历史 `validate_weights` 行为一致：无价剔除、超上限截断、总权重缩放。
    """
    results: list[RiskRuleResult] = []
    clean: dict[str, float] = {}
    total = 0.0
    for sym, w in sorted(targets.items()):
        if w <= 0:
            continue
        if sym not in latest_close.index or pd.isna(latest_close[sym]):
            results.append(RiskRuleResult(
                rule_id="weight.no_price",
                outcome=RiskRuleOutcome.DENY,
                message=f"{sym}: 无最新价格，剔除",
            ))
            continue
        if w > limits.max_position_pct:
            results.append(RiskRuleResult(
                rule_id="weight.position_limit",
                outcome=RiskRuleOutcome.ADJUST,
                message=(
                    f"{sym}: 权重 {w:.1%} 超过单票上限 "
                    f"{limits.max_position_pct:.1%}，已截断"
                ),
            ))
            w = limits.max_position_pct
        clean[sym] = w
        total += w
    if total > 1.0:
        scale = 1.0 / total
        clean = {s: w * scale for s, w in clean.items()}
        results.append(RiskRuleResult(
            rule_id="weight.gross_exposure",
            outcome=RiskRuleOutcome.ADJUST,
            message=f"总权重 {total:.1%} 超限，已等比缩放至 100%",
        ))
    return clean, results


# ---------------------------------------------------------------------------
# 订单意图级评估（paper/live 下单前强制链路）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """风控评估输入：账户权益、持仓与前收盘价。"""

    account_id: str
    business_time: datetime
    equity: float
    cash: float
    positions: Mapping[str, int] = field(default_factory=dict)
    prev_close: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.account_id).strip():
            raise ValueError("account_id 不能为空")
        object.__setattr__(self, "business_time", require_aware(self.business_time, "business_time"))
        if self.equity <= 0:
            raise ValueError("equity 必须为正")

    @property
    def trade_date(self) -> date:
        """按 A 股市场时区解释业务时间所属交易日。"""
        return self.business_time.astimezone(SHANGHAI_TZ).date()


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """规则共享上下文（状态、限额、规则书、组合快照）。"""

    limits: RiskLimits
    state: RiskState
    snapshot: PortfolioSnapshot
    rule_book: RuleBook

    @property
    def trade_date(self) -> date:
        return self.snapshot.trade_date

    def ruleset_of(self, symbol: str) -> RuleSet | None:
        return self.rule_book.resolve_symbol(symbol, self.trade_date)

    def lot_of(self, symbol: str) -> int:
        ruleset = self.ruleset_of(symbol)
        return int(ruleset.lot_size) if ruleset is not None else 100


class RiskRule:
    """风控规则基类：`check` 给出结论，`quantity_cap` 可选给出数量上限。"""

    rule_id: str = "base"

    def check(self, intent: OrderIntent, ctx: EvaluationContext) -> RiskRuleResult:
        raise NotImplementedError

    def quantity_cap(self, intent: OrderIntent, ctx: EvaluationContext) -> int | None:
        """该规则允许的最大批准数量；None 表示不额外限制。"""
        return None


class StateGateRule(RiskRule):
    """风险状态闸门：HALTED/RECOVERY 禁止新订单，REDUCING 只许降风险。"""

    rule_id = "state_gate"

    def check(self, intent: OrderIntent, ctx: EvaluationContext) -> RiskRuleResult:
        state = ctx.state
        if state is RiskState.HALTED:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.DENY,
                message="风险状态 HALTED：禁止新增订单（允许撤单与查询）",
            )
        if state is RiskState.RECOVERY:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.DENY,
                message="风险状态 RECOVERY：完成对账与人工复核前禁止新订单",
            )
        if state is RiskState.REDUCING and intent.side is OrderSide.BUY:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.DENY,
                message="风险状态 REDUCING：只允许降低风险方向的订单",
            )
        return RiskRuleResult(
            rule_id=self.rule_id, outcome=RiskRuleOutcome.PASS,
            message=f"风险状态 {state.value} 检查通过",
        )


class PositionLimitRule(RiskRule):
    """单票市值上限：买入后持仓市值不得超过权益 × max_position_pct。"""

    rule_id = "position_limit"

    def check(self, intent: OrderIntent, ctx: EvaluationContext) -> RiskRuleResult:
        if intent.side is OrderSide.SELL:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.PASS,
                message="卖出为降风险方向，不占用单票上限",
            )
        cap = self._max_add_shares(intent, ctx)
        if cap is None:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.DENY,
                message=f"{intent.symbol}: 无参考价格，无法评估单票上限",
            )
        if cap <= 0:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.DENY,
                message=(
                    f"{intent.symbol}: 持仓已达单票上限 "
                    f"{ctx.limits.max_position_pct:.1%}"
                ),
            )
        if cap < intent.quantity:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.ADJUST,
                message=f"{intent.symbol}: 按单票上限截断至 {cap} 股",
            )
        return RiskRuleResult(
            rule_id=self.rule_id, outcome=RiskRuleOutcome.PASS,
            message="单票上限检查通过",
        )

    def quantity_cap(self, intent: OrderIntent, ctx: EvaluationContext) -> int | None:
        if intent.side is OrderSide.SELL:
            return None
        return self._max_add_shares(intent, ctx)

    def _max_add_shares(self, intent: OrderIntent, ctx: EvaluationContext) -> int | None:
        """买入后不突破单票上限的最大可加仓位（整手向下取整）；无价返回 None。"""
        snapshot = ctx.snapshot
        prev = snapshot.prev_close.get(intent.symbol)
        price = float(prev) if prev else (
            float(intent.limit_price) if intent.limit_price is not None else None
        )
        if not price or price <= 0:
            return None
        current_value = snapshot.positions.get(intent.symbol, 0) * price
        headroom = ctx.limits.max_position_pct * snapshot.equity - current_value
        if headroom <= 0:
            return 0
        lot = ctx.lot_of(intent.symbol)
        return int(headroom / price) // lot * lot


class LotSizeRule(RiskRule):
    """买入整手约束（手数来自 RuleBook 当日生效规则）。"""

    rule_id = "lot_size"

    def check(self, intent: OrderIntent, ctx: EvaluationContext) -> RiskRuleResult:
        if intent.side is OrderSide.SELL:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.PASS,
                message="卖出允许零股",
            )
        lot = ctx.lot_of(intent.symbol)
        remainder = intent.quantity % lot
        if remainder == 0:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.PASS,
                message=f"整手检查通过（{lot} 股/手）",
            )
        rounded = intent.quantity - remainder
        if rounded <= 0:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.DENY,
                message=f"{intent.symbol}: 不足一手（{lot} 股）",
            )
        return RiskRuleResult(
            rule_id=self.rule_id, outcome=RiskRuleOutcome.ADJUST,
            message=f"{intent.symbol}: 按整手（{lot} 股）取整至 {rounded} 股",
        )

    def quantity_cap(self, intent: OrderIntent, ctx: EvaluationContext) -> int | None:
        if intent.side is OrderSide.SELL:
            return None
        lot = ctx.lot_of(intent.symbol)
        return intent.quantity // lot * lot


class PriceBandRule(RiskRule):
    """限价必须在当日涨跌幅区间内（RuleBook 按日期解析历史规则）。"""

    rule_id = "price_band"

    def check(self, intent: OrderIntent, ctx: EvaluationContext) -> RiskRuleResult:
        if intent.limit_price is None:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.PASS,
                message="无限价，跳过价格笼子检查",
            )
        prev = ctx.snapshot.prev_close.get(intent.symbol)
        if not prev or prev <= 0:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.PASS,
                message="无昨收价，跳过价格笼子检查",
            )
        ruleset = ctx.ruleset_of(intent.symbol)
        if ruleset is None:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.PASS,
                message=f"{intent.symbol}: 规则书无记录，跳过价格笼子检查",
            )
        band = ctx.rule_book.price_limits(ruleset, float(prev))
        if band is None:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.PASS,
                message="新股无涨跌幅阶段",
            )
        upper, lower = band
        limit_price = float(intent.limit_price)
        if limit_price > upper + 1e-6 or limit_price < lower - 1e-6:
            return RiskRuleResult(
                rule_id=self.rule_id, outcome=RiskRuleOutcome.DENY,
                message=(
                    f"{intent.symbol}: 限价 {limit_price:.2f} 超出当日区间 "
                    f"[{lower:.2f}, {upper:.2f}]"
                ),
            )
        return RiskRuleResult(
            rule_id=self.rule_id, outcome=RiskRuleOutcome.PASS,
            message="价格笼子检查通过",
        )


def _default_rule_book() -> RuleBook:
    """优先加载持久化规则书，缺失/损坏时回退默认规则集（并告警）。"""
    from quart.market_rules.rule_book import RULE_BOOK_PATH, RuleBook, default_rule_book

    try:
        if RULE_BOOK_PATH.exists():
            return RuleBook.load(RULE_BOOK_PATH)
    except Exception as exc:
        logger.warning("risk: rule_book load failed, fallback to default: {}", exc)
    return default_rule_book()


class RiskEngine:
    """订单意图的强制风控链路。

    评估顺序：状态闸门 → 单票上限 → 整手 → 价格笼子。
    任一 DENY 即拒绝；否则取所有数量上限的最小值为批准数量。
    传入 `repo` 时，决策落库并按幂等键去重。
    """

    def __init__(
        self,
        limits: RiskLimits,
        *,
        rule_book: RuleBook | None = None,
        state_provider: Callable[[str], RiskState] | None = None,
        repo: DecisionRecorder | None = None,
        rules: tuple[RiskRule, ...] | None = None,
    ):
        self.limits = limits
        self.limit_version = limits.version()
        self.rule_book = rule_book or _default_rule_book()
        self.state_provider = state_provider or (lambda _account_id: RiskState.ACTIVE)
        self.repo = repo
        self.rules: tuple[RiskRule, ...] = rules or (
            StateGateRule(),
            PositionLimitRule(),
            LotSizeRule(),
            PriceBandRule(),
        )

    def evaluate(
        self, intent: OrderIntent, snapshot: PortfolioSnapshot
    ) -> RiskDecision:
        if intent.account_id != snapshot.account_id:
            raise ValueError("OrderIntent 与 PortfolioSnapshot 账户不一致")
        ctx = EvaluationContext(
            limits=self.limits,
            state=RiskState.coerce(self.state_provider(intent.account_id)),
            snapshot=snapshot,
            rule_book=self.rule_book,
        )
        results = [rule.check(intent, ctx) for rule in self.rules]
        denied = [r for r in results if r.outcome is RiskRuleOutcome.DENY]
        if denied:
            decision = RiskDecision.deny(
                intent,
                rules=tuple(results),
                limit_version=self.limit_version,
                reason="; ".join(r.message for r in denied),
                business_time=intent.business_time,
            )
        else:
            caps = [cap for cap in (
                rule.quantity_cap(intent, ctx) for rule in self.rules
            ) if cap is not None]
            approved = min([intent.quantity, *caps])
            adjusted = [r for r in results if r.outcome is RiskRuleOutcome.ADJUST]
            if approved <= 0:
                decision = RiskDecision.deny(
                    intent,
                    rules=tuple(results),
                    limit_version=self.limit_version,
                    reason="数量上限收敛为 0",
                    business_time=intent.business_time,
                )
            elif approved < intent.quantity:
                decision = RiskDecision.adjust(
                    intent,
                    approved_quantity=approved,
                    rules=tuple(results),
                    limit_version=self.limit_version,
                    reason="; ".join(r.message for r in adjusted) or "数量被限额截断",
                    business_time=intent.business_time,
                )
            else:
                decision = RiskDecision.allow(
                    intent,
                    rules=tuple(results),
                    limit_version=self.limit_version,
                    business_time=intent.business_time,
                )
        if self.repo is not None:
            decision = self.repo.record_decision(decision)
        return decision


def require_risk_engine(
    environment: TradingEnvironment | str, engine: RiskEngine | None
) -> RiskEngine | None:
    """正式信号（paper）与实盘（live）不允许缺少 Risk Engine；研究环境可选。"""
    env = TradingEnvironment.coerce(environment)
    if engine is None and env in (TradingEnvironment.PAPER, TradingEnvironment.LIVE):
        raise RuntimeError(f"{env.value} 环境必须启用 Risk Engine（RISK-001 强制链路）")
    return engine


__all__ = [
    "ALLOWED_TRANSITIONS",
    "EvaluationContext",
    "LotSizeRule",
    "PortfolioSnapshot",
    "PositionLimitRule",
    "PriceBandRule",
    "RiskEngine",
    "RiskLimits",
    "RiskRule",
    "RiskState",
    "StateGateRule",
    "evaluate_weights",
    "limits_from_config",
    "require_risk_engine",
]

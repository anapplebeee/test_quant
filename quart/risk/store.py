"""Risk 状态与决策的持久化仓储（RISK-001）。

- `risk_states`：每账户最新风险状态（权威行）；
- `risk_state_history`：状态切换审计流水（操作者 + 原因）；
- `risk_decisions`：风控决策，按 `idempotency_key` 幂等。

状态切换必须满足 `ALLOWED_TRANSITIONS`（非法迁移抛 ValueError）；
HALTED 之后恢复只能走 `RECOVERY → ACTIVE`（人工复核闸门）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from quart.domain.orders import RiskDecision, RiskRuleResult
from quart.infrastructure.db import Database
from quart.risk.daily_loss import DailyEquityMark
from quart.risk.engine import ALLOWED_TRANSITIONS, RiskState


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class RiskRepository:
    """风险状态与决策仓储。测试可传独立临时库。"""

    def __init__(self, db: Database | None = None):
        if db is None:
            from quart.infrastructure.db import get_db

            db = get_db()
        self.db: Database = db
        self._lock = threading.Lock()

    def migrate(self) -> list[int]:
        """应用平台 schema migration（全量）。"""
        from quart.infrastructure.migrations import PLATFORM_MIGRATIONS

        return self.db.apply(PLATFORM_MIGRATIONS)

    # ---------------- 状态 ----------------

    def get_state(self, account_id: str) -> RiskState:
        """账户当前风险状态；无记录视为 ACTIVE（新账户正常展业）。"""
        self.migrate()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT state FROM risk_states WHERE account_id = ?", (account_id,)
            ).fetchone()
        return RiskState.coerce(row["state"]) if row else RiskState.ACTIVE

    def set_state(
        self,
        account_id: str,
        new_state: RiskState | str,
        *,
        reason: str = "",
        operator: str = "",
        limit_version: str = "",
    ) -> RiskState:
        """切换风险状态；非法迁移抛 ValueError。返回切换后的状态。"""
        target = RiskState.coerce(new_state)
        self.migrate()
        with self._lock, self.db.connect() as conn:
            row = conn.execute(
                "SELECT state FROM risk_states WHERE account_id = ?", (account_id,)
            ).fetchone()
            current = RiskState.coerce(row["state"]) if row else RiskState.ACTIVE
            if target is current:
                return current
            allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
            if target not in allowed:
                legal = ", ".join(sorted(s.value for s in allowed)) or "（无）"
                raise ValueError(
                    f"非法风险状态迁移: {current.value} -> {target.value}（允许: {legal}）"
                )
            now = _now()
            if row:
                conn.execute(
                    """
                    UPDATE risk_states
                    SET state = ?, reason = ?, operator = ?, limit_version = ?, updated_at = ?
                    WHERE account_id = ?
                    """,
                    (target.value, reason, operator, limit_version, now, account_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO risk_states
                        (account_id, state, reason, operator, limit_version, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (account_id, target.value, reason, operator, limit_version, now),
                )
            conn.execute(
                """
                INSERT INTO risk_state_history
                    (account_id, state, reason, operator, limit_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (account_id, target.value, reason, operator, limit_version, now),
            )
            conn.commit()
        return target

    def state_history(self, account_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """状态切换审计流水（新→旧）。"""
        self.migrate()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT state, reason, operator, limit_version, created_at
                FROM risk_state_history
                WHERE account_id = ?
                ORDER BY history_id DESC LIMIT ?
                """,
                (account_id, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 日损权益基线 ----------------

    def get_daily_mark(self, account_id: str, trade_date) -> DailyEquityMark | None:
        """读取账户在指定交易日已记录的日损权益观测。"""
        day = str(trade_date)[:10]
        self.migrate()
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM risk_daily_equity_marks
                WHERE account_id = ? AND trade_date = ?
                """,
                (str(account_id), day),
            ).fetchone()
        return self._daily_mark_from_row(row)

    def latest_daily_mark_before(self, account_id: str, trade_date) -> DailyEquityMark | None:
        """读取严格早于指定日的最近日终权益，作为下一个交易日的日初基线。"""
        day = str(trade_date)[:10]
        self.migrate()
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM risk_daily_equity_marks
                WHERE account_id = ? AND trade_date < ?
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                (str(account_id), day),
            ).fetchone()
        return self._daily_mark_from_row(row)

    def upsert_daily_mark(self, mark: DailyEquityMark) -> DailyEquityMark:
        """原子更新同日权益观测，保留首次入库时间供审计。"""
        self.migrate()
        now = _now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO risk_daily_equity_marks (
                    account_id, trade_date, opening_equity, current_equity,
                    daily_loss_pct, baseline_date, baseline_available,
                    limit_version, triggered_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, trade_date) DO UPDATE SET
                    opening_equity = excluded.opening_equity,
                    current_equity = excluded.current_equity,
                    daily_loss_pct = excluded.daily_loss_pct,
                    baseline_date = excluded.baseline_date,
                    baseline_available = excluded.baseline_available,
                    limit_version = excluded.limit_version,
                    triggered_state = excluded.triggered_state,
                    updated_at = excluded.updated_at
                """,
                (
                    mark.account_id,
                    mark.trade_date.isoformat(),
                    mark.opening_equity,
                    mark.current_equity,
                    mark.daily_loss_pct,
                    mark.baseline_date.isoformat() if mark.baseline_date else None,
                    int(mark.baseline_available),
                    mark.limit_version,
                    mark.triggered_state.value if mark.triggered_state else None,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_daily_mark(mark.account_id, mark.trade_date) or mark

    @staticmethod
    def _daily_mark_from_row(row) -> DailyEquityMark | None:
        if row is None:
            return None
        try:
            value = dict(row)
            return DailyEquityMark(
                account_id=str(value["account_id"]),
                trade_date=datetime.fromisoformat(str(value["trade_date"])).date(),
                opening_equity=float(value["opening_equity"]),
                current_equity=float(value["current_equity"]),
                daily_loss_pct=float(value["daily_loss_pct"]),
                baseline_date=(
                    datetime.fromisoformat(str(value["baseline_date"])).date()
                    if value.get("baseline_date")
                    else None
                ),
                baseline_available=bool(value["baseline_available"]),
                limit_version=str(value["limit_version"]),
                triggered_state=(
                    RiskState.coerce(value["triggered_state"])
                    if value.get("triggered_state")
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    # ---------------- 决策 ----------------

    def record_decision(self, decision: RiskDecision) -> RiskDecision:
        """落库一条决策；幂等键已存在时返回既有记录（重试语义一致）。"""
        self.migrate()
        rules_json = json.dumps(
            [
                {"rule_id": r.rule_id, "outcome": r.outcome.value, "message": r.message}
                for r in decision.rules
            ],
            ensure_ascii=False,
        )
        with self.db.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO risk_decisions (
                        decision_id, idempotency_key, intent_id, account_id,
                        environment, status, requested_quantity, approved_quantity,
                        rules_json, limit_version, reason, business_time, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.decision_id,
                        decision.idempotency_key,
                        decision.intent_id,
                        decision.account_id,
                        decision.environment.value,
                        decision.status.value,
                        decision.requested_quantity,
                        decision.approved_quantity,
                        rules_json,
                        decision.limit_version,
                        decision.reason,
                        decision.business_time.isoformat(),
                        decision.created_at.isoformat(),
                    ),
                )
                conn.commit()
                return decision
            except sqlite3.IntegrityError:
                conn.rollback()
        existing = self.get_decision_by_key(decision.idempotency_key)
        return existing if existing is not None else decision

    def get_decision_by_key(self, idempotency_key: str) -> RiskDecision | None:
        self.migrate()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM risk_decisions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._decision_from_row(row) if row else None

    def list_decisions(
        self, account_id: str | None = None, limit: int = 50
    ) -> list[RiskDecision]:
        self.migrate()
        sql = "SELECT * FROM risk_decisions"
        params: list[Any] = []
        if account_id:
            sql += " WHERE account_id = ?"
            params.append(account_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [d for d in (self._decision_from_row(r) for r in rows) if d is not None]

    @staticmethod
    def _decision_from_row(row) -> RiskDecision | None:
        d = dict(row)
        try:
            rules = tuple(
                RiskRuleResult(
                    rule_id=r["rule_id"], outcome=r["outcome"], message=r["message"]
                )
                for r in json.loads(d.get("rules_json") or "[]")
            )
            return RiskDecision(
                decision_id=d["decision_id"],
                intent_id=d["intent_id"],
                account_id=d["account_id"],
                environment=d["environment"],
                status=d["status"],
                requested_quantity=int(d["requested_quantity"]),
                approved_quantity=int(d["approved_quantity"]),
                rules=rules,
                limit_version=d["limit_version"],
                business_time=_parse_dt(d["business_time"]),
                source="RISK_ENGINE",
                idempotency_key=d["idempotency_key"],
                reason=d.get("reason") or "",
                created_at=_parse_dt(d["created_at"]),
            )
        except (KeyError, ValueError):
            return None


__all__ = ["RiskRepository"]

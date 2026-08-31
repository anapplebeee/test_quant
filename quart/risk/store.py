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
                    (account_id, state, reason, operator, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (account_id, target.value, reason, operator, now),
            )
            conn.commit()
        return target

    def state_history(self, account_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """状态切换审计流水（新→旧）。"""
        self.migrate()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT state, reason, operator, created_at
                FROM risk_state_history
                WHERE account_id = ?
                ORDER BY history_id DESC LIMIT ?
                """,
                (account_id, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

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

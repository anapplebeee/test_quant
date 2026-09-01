"""正式研究的 PIT 数据证据门禁（DATA-002B）。

``filter_for_pit_universe`` 只能保证指数成分股历史不回退为当前快照；它不能
证明证券主数据和公司行为账本与回测区间相匹配。本模块把这几项证据收敛成一个
可序列化的结果，供正式回测、WFA 和 Artifact 共同使用。

探索模式可以不调用本模块；正式模式必须调用 ``require_pit_evidence``。缺少
任一证据时直接失败，而不是在报告中仅写一个 warning。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from quart.config import data_root
from quart.data.corporate_actions import CORPORATE_ACTION_PATH, CorporateActionLedger
from quart.data.security_master import MASTER_PATH, SecurityMaster
from quart.data.universe_history import HISTORY_FILE


@dataclass(frozen=True)
class PITEvidenceIssue:
    """一条可展示、可写入 Artifact 的 PIT 证据问题。"""

    rule_id: str
    severity: str
    message: str
    affected_rows: int = 0
    samples: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PITEvidenceResult:
    """PIT 证据检查结果；只有没有 ERROR 才可进入 formal 主链路。"""

    issues: list[PITEvidenceIssue] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "ERROR" for issue in self.issues)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "issues": [issue.to_dict() for issue in self.issues],
            "metadata": self.metadata,
        }


class PITEvidenceError(RuntimeError):
    """正式研究所需 PIT 证据缺失或覆盖不足。"""


def _open_end() -> pd.Timestamp:
    return pd.Timestamp("2262-01-01")


def _master_coverage(master: SecurityMaster, bars: pd.DataFrame) -> tuple[int, list[str]]:
    """返回无法由上市/退市区间证明的行情行数量和小样本。"""
    rows = bars[["symbol", "date"]].copy()
    rows["symbol"] = rows["symbol"].astype(str).str.zfill(6)
    rows["date"] = pd.to_datetime(rows["date"]).dt.normalize()
    rows = rows.drop_duplicates()

    base = master.table[["symbol", "listed_at", "delisted_at"]].copy()
    base["symbol"] = base["symbol"].astype(str).str.zfill(6)
    base = base.groupby("symbol", as_index=False).agg(
        listed_at=("listed_at", "min"),
        delisted_at=("delisted_at", lambda values: values.dropna().min() if values.notna().any() else pd.NaT),
    )
    joined = rows.merge(base, how="left", on="symbol")
    missing = joined["listed_at"].isna() | (joined["date"] < joined["listed_at"])
    missing |= joined["delisted_at"].notna() & (joined["date"] >= joined["delisted_at"])
    samples = [
        f"{row.symbol}@{row.date.date()}"
        for row in joined.loc[missing, ["symbol", "date"]].head(10).itertuples(index=False)
    ]
    return int(missing.sum()), samples


def _universe_coverage(root: Path, index_code: str, dates: pd.Series) -> tuple[int, list[str]]:
    """验证指定日期均有非空 PIT 成分记录，不允许静默回退当前快照。"""
    path = root / "universe" / f"{index_code}_{HISTORY_FILE}"
    if not path.exists():
        return len(dates.drop_duplicates()), [f"missing history: {path.name}"]
    hist = pd.read_parquet(path)
    if hist.empty or not {"symbol", "in_date", "out_date"}.issubset(hist.columns):
        return len(dates.drop_duplicates()), [f"invalid history: {path.name}"]
    hist["in_date"] = pd.to_datetime(hist["in_date"])
    hist["out_date"] = pd.to_datetime(hist["out_date"]).fillna(_open_end())
    unique_dates = pd.DatetimeIndex(pd.to_datetime(dates).drop_duplicates().sort_values())
    missing = [date for date in unique_dates if not ((hist["in_date"] <= date) & (hist["out_date"] >= date)).any()]
    return len(missing), [str(date.date()) for date in missing[:10]]


def check_pit_evidence(
    bars: pd.DataFrame,
    *,
    index_code: str,
    root: Path | str | None = None,
) -> PITEvidenceResult:
    """检查正式回测所需的成分、主数据和公司行为 PIT 证据。

    ``bars`` 应是原始行情或已按股票池过滤的日线长表。检查使用其所有日期/标的，
    所以即使上游误把一只未来上市股票混入，也会被证券主数据覆盖校验捕获。
    """
    base = Path(root) if root else Path(data_root())
    result = PITEvidenceResult()
    required = {"symbol", "date"}
    missing_columns = sorted(required - set(bars.columns))
    if bars.empty:
        result.issues.append(PITEvidenceIssue("PIT-001", "ERROR", "行情为空，无法验证 PIT 覆盖"))
        return result
    if missing_columns:
        result.issues.append(PITEvidenceIssue("PIT-002", "ERROR", f"行情缺少列: {missing_columns}"))
        return result

    dates = pd.to_datetime(bars["date"], errors="coerce")
    invalid_dates = int(dates.isna().sum())
    if invalid_dates:
        result.issues.append(PITEvidenceIssue("PIT-003", "ERROR", "行情包含无法解析的交易日期", invalid_dates))
        return result
    result.metadata.update(
        {
            "index_code": str(index_code),
            "symbols": int(bars["symbol"].astype(str).nunique()),
            "start": str(dates.min().date()),
            "end": str(dates.max().date()),
        }
    )

    missing_universe, universe_samples = _universe_coverage(base, str(index_code), dates)
    if missing_universe:
        result.issues.append(
            PITEvidenceIssue(
                "PIT-010",
                "ERROR",
                f"{index_code} PIT 成分历史未覆盖全部交易日；请补历史快照后重跑 formal",
                missing_universe,
                tuple(universe_samples),
            )
        )
    else:
        result.metadata["universe_history"] = f"{index_code}_{HISTORY_FILE}"

    master_path = base / "meta" / MASTER_PATH.name
    if not master_path.exists():
        result.issues.append(
            PITEvidenceIssue(
                "PIT-020",
                "ERROR",
                "缺少 security_master.parquet；运行 scripts/build_security_master.py 后重试",
            )
        )
    else:
        try:
            master = SecurityMaster.load(master_path)
            master_problems = master.validate()
            if master_problems:
                result.issues.append(
                    PITEvidenceIssue(
                        "PIT-021",
                        "ERROR",
                        "证券主数据区间不合法: " + "; ".join(master_problems[:3]),
                    )
                )
            uncovered, samples = _master_coverage(master, bars)
            if uncovered:
                result.issues.append(
                    PITEvidenceIssue(
                        "PIT-022",
                        "ERROR",
                        "证券主数据无法证明部分行情行当日已上市且未退市",
                        uncovered,
                        tuple(samples),
                    )
                )
            result.metadata["security_master_version"] = master.version()
        except Exception as exc:
            result.issues.append(PITEvidenceIssue("PIT-023", "ERROR", f"无法读取证券主数据: {exc}"))

    action_path = base / "meta" / CORPORATE_ACTION_PATH.name
    if not action_path.exists():
        result.issues.append(
            PITEvidenceIssue(
                "PIT-030",
                "ERROR",
                "缺少 corporate_actions.parquet；先导入公司行为账本并绑定复权口径",
            )
        )
    else:
        try:
            ledger = CorporateActionLedger.load(action_path)
            result.metadata["corporate_action_version"] = ledger.version()
            result.metadata["corporate_action_rows"] = len(ledger.table)
        except Exception as exc:
            result.issues.append(PITEvidenceIssue("PIT-031", "ERROR", f"无法读取公司行为账本: {exc}"))

    return result


def require_pit_evidence(
    bars: pd.DataFrame,
    *,
    index_code: str,
    root: Path | str | None = None,
) -> PITEvidenceResult:
    """返回通过结果；证据不完整时以可操作错误阻断 formal 运行。"""
    result = check_pit_evidence(bars, index_code=index_code, root=root)
    if result.passed:
        return result
    lines = ["正式运行缺少 PIT 数据证据："]
    for issue in result.issues:
        if issue.severity == "ERROR":
            sample_text = f"；样例: {', '.join(issue.samples)}" if issue.samples else ""
            lines.append(f"- [{issue.rule_id}] {issue.message}{sample_text}")
    raise PITEvidenceError("\n".join(lines))


__all__ = [
    "PITEvidenceError",
    "PITEvidenceIssue",
    "PITEvidenceResult",
    "check_pit_evidence",
    "require_pit_evidence",
]

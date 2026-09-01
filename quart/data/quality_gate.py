"""统一数据质量 Preflight（QUALITY-002）。

扫描脚本适合离线诊断；正式研究和每日信号还需要一个同步、确定性的门禁：在
任何因子计算、回测或交易计划之前，检查本次实际使用的行情与基准，并把结果
写成可审计的结构化状态。此模块不修复数据，也不会静默丢弃错误行。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from quart.config import data_root
from quart.data.quality import load_blocklist

QUALITY_GATE_PATH = Path(data_root()) / "meta" / "last_quality_gate.json"
_BAR_COLUMNS = ("date", "symbol", "open", "high", "low", "close", "volume", "amount")


@dataclass(frozen=True)
class QualityIssue:
    """可展示、可归档的一条数据质量问题。"""

    rule_id: str
    severity: str
    message: str
    affected_rows: int = 0
    samples: tuple[str, ...] = ()
    remediation: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QualityGateResult:
    """本次输入的质量门禁结果；有 ERROR 即不允许 strict 主链路继续。"""

    issues: list[QualityIssue] = field(default_factory=list)
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


class DataQualityError(RuntimeError):
    """strict 模式的数据质量失败。"""


def _samples(frame: pd.DataFrame, columns: Iterable[str], limit: int = 10) -> tuple[str, ...]:
    if frame.empty:
        return ()
    return tuple(
        "@".join(str(value) for value in row)
        for row in frame.loc[:, list(columns)].head(limit).itertuples(index=False, name=None)
    )


def _append(
    result: QualityGateResult,
    rule_id: str,
    message: str,
    frame: pd.DataFrame | None = None,
    *,
    columns: tuple[str, ...] = ("symbol", "date"),
    remediation: str,
) -> None:
    rows = 0 if frame is None else len(frame)
    samples = () if frame is None else _samples(frame, columns)
    result.issues.append(
        QualityIssue(
            rule_id=rule_id,
            severity="ERROR",
            message=message,
            affected_rows=rows,
            samples=samples,
            remediation=remediation,
        )
    )


def evaluate_quality_gate(
    bars: pd.DataFrame,
    benchmark: pd.DataFrame | None = None,
    *,
    as_of: str | pd.Timestamp | None = None,
    max_data_lag_days: int = 5,
    max_symbol_lag_days: int = 30,
    blocked_symbols: set[str] | None = None,
) -> QualityGateResult:
    """检查本次运行实际使用的行情和基准。

    ``as_of`` 不传时以行情最新日为基准，适合历史回测；每日信号应传入当前日期，
    从而把新鲜度变成硬门禁。调用方可传入显式 blocklist，默认读全局治理清单。
    """
    result = QualityGateResult()
    missing_columns = sorted(set(_BAR_COLUMNS) - set(bars.columns))
    if bars.empty:
        _append(
            result,
            "QG-001",
            "行情为空，不能生成研究或交易结论",
            remediation="先运行 scripts/update_data.py 并确认 daily 数据落盘。",
        )
        return result
    if missing_columns:
        _append(
            result,
            "QG-002",
            f"行情缺少必需列: {missing_columns}",
            remediation="修复数据源字段映射后重新拉取对应分区。",
        )
        return result

    frame = bars.loc[:, _BAR_COLUMNS].copy()
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    bad_dates = frame[frame["date"].isna()]
    if not bad_dates.empty:
        _append(
            result,
            "QG-003",
            "行情包含无法解析的交易日期",
            bad_dates,
            remediation="修复日期列并重建受影响分区。",
        )
        return result

    for column in ("open", "high", "low", "close", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    latest = pd.Timestamp(frame["date"].max()).normalize()
    reference = pd.Timestamp(as_of).normalize() if as_of is not None else latest
    result.metadata.update(
        {
            "start": str(pd.Timestamp(frame["date"].min()).date()),
            "end": str(latest.date()),
            "as_of": str(reference.date()),
            "rows": len(frame),
            "symbols": int(frame["symbol"].nunique()),
            "max_data_lag_days": int(max_data_lag_days),
            "max_symbol_lag_days": int(max_symbol_lag_days),
        }
    )

    if reference < latest:
        _append(
            result,
            "QG-004",
            "运行时点早于行情最新日期，存在使用未来行情的风险",
            frame[frame["date"] > reference],
            remediation="将 --end/信号日期限制在可得数据日期，或传入正确 as_of。",
        )
    if (reference - latest).days > max_data_lag_days:
        _append(
            result,
            "QG-005",
            f"行情距运行时点已滞后 {(reference - latest).days} 天，超过 {max_data_lag_days} 天阈值",
            remediation="更新日线和基准数据后重新运行。",
        )

    duplicates = frame[frame.duplicated(["symbol", "date"], keep=False)]
    if not duplicates.empty:
        _append(
            result,
            "QG-010",
            "行情存在重复的 symbol/date 记录",
            duplicates,
            remediation="按数据源优先级去重并重建对应 parquet 分区。",
        )

    invalid_price = frame[
        frame[["open", "high", "low", "close"]].isna().any(axis=1)
        | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
    ]
    if not invalid_price.empty:
        _append(
            result,
            "QG-011",
            "OHLC 含缺失、零或负价格",
            invalid_price,
            remediation="核对复权方式与原始行情，修复后重拉分区。",
        )

    invalid_ohlc = frame[
        (frame["high"] < frame["low"])
        | (frame["open"] > frame["high"])
        | (frame["open"] < frame["low"])
        | (frame["close"] > frame["high"])
        | (frame["close"] < frame["low"])
    ]
    if not invalid_ohlc.empty:
        _append(
            result,
            "QG-012",
            "OHLC 价格关系不成立",
            invalid_ohlc,
            remediation="检查源字段顺序、复权处理和异常修复记录。",
        )

    invalid_volume = frame[(frame["volume"] < 0) | (frame["amount"] < 0)]
    if not invalid_volume.empty:
        _append(
            result,
            "QG-013",
            "成交量或成交额为负",
            invalid_volume,
            remediation="修复数据源单位/符号错误后重建分区。",
        )
    missing_amount = frame[(frame["volume"] > 0) & (frame["amount"].isna() | (frame["amount"] <= 0))]
    if not missing_amount.empty:
        _append(
            result,
            "QG-014",
            "有成交量但成交额缺失或非正，容量与冲击成本无法审计",
            missing_amount,
            remediation="补齐 amount 字段或隔离该数据源。",
        )

    # 个股最后一根行情落后不一定是数据缺失（可能是退市或历史成分调出）；
    # 该问题需要结合证券状态 PIT 才能判定，交给离线扫描器和 PIT 门禁处理，
    # 这里只记录数量，避免把正常退市样本误判为 quality failure。
    max_dates = frame.groupby("symbol")["date"].max()
    result.metadata["stale_symbol_candidates"] = int(
        ((latest - max_dates).dt.days > max_symbol_lag_days).sum()
    )

    blocked = blocked_symbols if blocked_symbols is not None else load_blocklist()
    blocked = {str(symbol).zfill(6) for symbol in blocked}
    hit = frame[frame["symbol"].isin(blocked)]
    if not hit.empty:
        _append(
            result,
            "QG-030",
            "本次输入包含已被治理清单阻断的证券",
            hit,
            remediation="先复核/修复隔离数据；不能通过过滤清单绕过 formal 或信号门禁。",
        )

    if benchmark is None or benchmark.empty:
        _append(
            result,
            "QG-040",
            "基准数据为空，无法计算基准/超额收益",
            remediation="更新配置 benchmark 对应的指数日线。",
        )
    elif not {"date", "close"}.issubset(benchmark.columns):
        _append(
            result,
            "QG-041",
            "基准缺少 date 或 close 列",
            remediation="修复指数数据源字段映射后重新拉取。",
        )
    else:
        bench = benchmark[["date", "close"]].copy()
        bench["date"] = pd.to_datetime(bench["date"], errors="coerce")
        bench["close"] = pd.to_numeric(bench["close"], errors="coerce")
        required_dates = pd.DatetimeIndex(frame["date"].drop_duplicates())
        valid_bench_dates = set(bench.loc[bench["close"] > 0, "date"].dropna())
        missing_bench = [date for date in required_dates if date not in valid_bench_dates]
        if missing_bench:
            missing_frame = pd.DataFrame({"symbol": "benchmark", "date": missing_bench})
            _append(
                result,
                "QG-042",
                "基准未覆盖本次行情的全部交易日",
                missing_frame,
                remediation="补齐指数日线；formal 不允许以 ffill 掩盖缺失基准。",
            )
    return result


def save_quality_gate(result: QualityGateResult, path: Path | str | None = None) -> Path:
    """原子写入最近一次门禁状态，供前端和运维页读取。"""
    target = Path(path) if path else QUALITY_GATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def load_quality_gate(path: Path | str | None = None) -> dict | None:
    """读取最近门禁状态；不存在或损坏时返回 None，不伪造通过状态。"""
    target = Path(path) if path else QUALITY_GATE_PATH
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def require_quality_gate(
    bars: pd.DataFrame,
    benchmark: pd.DataFrame | None = None,
    *,
    as_of: str | pd.Timestamp | None = None,
    max_data_lag_days: int = 5,
    max_symbol_lag_days: int = 30,
    blocked_symbols: set[str] | None = None,
    status_path: Path | str | None = None,
) -> QualityGateResult:
    """执行并落盘门禁；存在 ERROR 时阻断调用方。"""
    result = evaluate_quality_gate(
        bars,
        benchmark,
        as_of=as_of,
        max_data_lag_days=max_data_lag_days,
        max_symbol_lag_days=max_symbol_lag_days,
        blocked_symbols=blocked_symbols,
    )
    save_quality_gate(result, status_path)
    if result.passed:
        return result
    lines = ["数据质量门禁失败："]
    for issue in result.issues:
        if issue.severity == "ERROR":
            samples = f"；样例: {', '.join(issue.samples)}" if issue.samples else ""
            lines.append(f"- [{issue.rule_id}] {issue.message}{samples}。修复：{issue.remediation}")
    raise DataQualityError("\n".join(lines))


__all__ = [
    "QUALITY_GATE_PATH",
    "DataQualityError",
    "QualityGateResult",
    "QualityIssue",
    "evaluate_quality_gate",
    "load_quality_gate",
    "require_quality_gate",
    "save_quality_gate",
]

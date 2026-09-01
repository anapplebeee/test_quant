"""公司行为的 Point-in-Time（PIT）账本。

价格的复权方式只能回答“价格序列如何换算”；它不能回答一项分红、送转或配股
在历史某天是否已经公开。研究和回测需要后一个答案，才能同时做到：

* 对现金分红/送转/拆分/配股保留可审计的原始事实；
* 使用 ``available_at`` 阻止后来修订的数据泄漏到历史；
* 将公司行为内容版本写入数据快照和研究 Artifact。

本模块只定义并保存权威的事实账本，不自行修改行情价格。复权价格或总收益
计算器必须显式引用同一份 ``CorporateActionLedger``，避免出现隐式、不可追溯
的复权口径。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from quart.config import data_root

CORPORATE_ACTION_PATH = Path(data_root()) / "meta" / "corporate_actions.parquet"

ACTION_TYPES = frozenset(
    {
        "cash_dividend",
        "stock_dividend",
        "split",
        "rights_issue",
        "placement",
    }
)

ACTION_COLUMNS = [
    "action_id",
    "revision_id",
    "symbol",
    "action_type",
    "announcement_date",
    "available_at",
    "revised_at",
    "record_date",
    "ex_date",
    "cash_per_share",
    "share_ratio",
    "rights_ratio",
    "rights_price",
    "source",
    "source_ref",
    "ingested_at",
]

_ALIASES: dict[str, tuple[str, ...]] = {
    "action_id": ("action_id", "id", "公告编号"),
    "revision_id": ("revision_id", "revision", "修订编号"),
    "symbol": ("symbol", "code", "ticker", "证券代码", "股票代码"),
    "action_type": ("action_type", "type", "行为类型", "公司行为类型"),
    "announcement_date": ("announcement_date", "announce_date", "ann_date", "公告日期", "披露日期"),
    "available_at": ("available_at", "known_at", "可用日期", "可交易日期"),
    "revised_at": ("revised_at", "revision_at", "修订日期", "修订时间"),
    "record_date": ("record_date", "登记日", "股权登记日"),
    "ex_date": ("ex_date", "除权除息日", "除权日", "除息日", "实施日"),
    "cash_per_share": ("cash_per_share", "cash_dividend", "每股派息", "每股现金分红"),
    "share_ratio": ("share_ratio", "stock_ratio", "送转比例", "送股比例", "转增比例"),
    "rights_ratio": ("rights_ratio", "配股比例"),
    "rights_price": ("rights_price", "配股价", "配股价格"),
    "source": ("source", "来源"),
    "source_ref": ("source_ref", "source_id", "公告链接", "公告编号"),
    "ingested_at": ("ingested_at", "采集时间", "抓取时间"),
}

_ACTION_TYPE_ALIASES = {
    "cash_dividend": "cash_dividend",
    "dividend": "cash_dividend",
    "现金分红": "cash_dividend",
    "派息": "cash_dividend",
    "stock_dividend": "stock_dividend",
    "bonus_share": "stock_dividend",
    "送股": "stock_dividend",
    "转增": "stock_dividend",
    "split": "split",
    "拆分": "split",
    "rights_issue": "rights_issue",
    "rights": "rights_issue",
    "配股": "rights_issue",
    "placement": "placement",
    "增发": "placement",
    "定向增发": "placement",
}


def _alias_values(frame: pd.DataFrame, aliases: tuple[str, ...]) -> pd.Series:
    """按别名优先级逐行取第一个非空值，而非整列只选一个别名。"""
    values = pd.Series(pd.NA, index=frame.index, dtype="object")
    for name in aliases:
        if name in frame.columns:
            missing = values.isna()
            values.loc[missing] = frame.loc[missing, name]
    return values


def _canonical_action_id(row: pd.Series) -> str:
    """生成稳定的自然键；来源修订会保留为同一动作的更新版本。"""
    payload = {
        "symbol": row["symbol"],
        "action_type": row["action_type"],
        "announcement_date": _date_text(row["announcement_date"]),
        "ex_date": _date_text(row["ex_date"]),
        "source": row["source"],
        "source_ref": row["source_ref"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _canonical_revision_id(row: pd.Series) -> str:
    """按经济字段生成修订内容标识；相同内容的重复导入保持幂等。"""
    payload = {
        "action_id": row["action_id"],
        "available_at": _date_text(row["available_at"]),
        "record_date": _date_text(row["record_date"]),
        "ex_date": _date_text(row["ex_date"]),
        "cash_per_share": _number_or_none(row["cash_per_share"]),
        "share_ratio": _number_or_none(row["share_ratio"]),
        "rights_ratio": _number_or_none(row["rights_ratio"]),
        "rights_price": _number_or_none(row["rights_price"]),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _date_text(value: Any) -> str | None:
    if pd.isna(value):
        return None
    return str(pd.Timestamp(value).normalize().date())


def _number_or_none(value: Any) -> float | None:
    return None if pd.isna(value) else float(value)


def normalize_corporate_actions(
    actions: pd.DataFrame,
    *,
    source: str | None = None,
    ingested_at: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """将外部公司行为数据标准化为稳定、可版本化的 PIT 合同。

    ``announcement_date`` 与 ``ex_date`` 是必填字段。若上游只有日期、没有
    发布时间，默认 ``available_at = announcement_date + 1 天``，宁可保守地延后
    使用，也不把收盘后公告泄漏至当日决策。数据源有精确可交易日期时应显式提供
    ``available_at`` 覆盖该默认值。
    """
    if actions is None:
        raise ValueError("actions 不能为空")
    raw = actions.copy()
    out = pd.DataFrame(index=raw.index)
    for target, aliases in _ALIASES.items():
        out[target] = _alias_values(raw, aliases)

    if source is not None:
        out["source"] = source
    out["source"] = out["source"].fillna("unknown").astype(str)
    out["source_ref"] = out["source_ref"].fillna("").astype(str)
    out["symbol"] = out["symbol"].astype(str).str.extract(r"(\d+)", expand=False).str.zfill(6)
    out["action_type"] = (
        out["action_type"]
        .astype(str)
        .str.strip()
        .str.lower()
        .map(_ACTION_TYPE_ALIASES)
        .fillna(out["action_type"].astype(str).str.strip().str.lower())
    )

    for column in (
        "announcement_date",
        "available_at",
        "revised_at",
        "record_date",
        "ex_date",
        "ingested_at",
    ):
        out[column] = pd.to_datetime(out[column], errors="coerce").dt.normalize()
    for column in ("cash_per_share", "share_ratio", "rights_ratio", "rights_price"):
        out[column] = pd.to_numeric(out[column], errors="coerce")

    if ingested_at is not None:
        out["ingested_at"] = pd.Timestamp(ingested_at).normalize()
    else:
        out["ingested_at"] = out["ingested_at"].fillna(pd.Timestamp.now().normalize())
    default_available_at = out["announcement_date"].map(
        lambda value: value + pd.offsets.Day(1) if pd.notna(value) else pd.NaT
    )
    out["available_at"] = out["available_at"].fillna(default_available_at)
    # 没有供应商修订时间时，不能把本次采集时间倒灌进历史；以该版本的可得日
    # 作为保守的首次可见时间，采集时间仅保留为审计元数据。
    out["revised_at"] = out["revised_at"].fillna(out["available_at"])
    out["action_id"] = out["action_id"].fillna("").astype(str).str.strip()
    missing_id = out["action_id"].eq("")
    out.loc[missing_id, "action_id"] = out.loc[missing_id].apply(_canonical_action_id, axis=1)
    out["revision_id"] = out["revision_id"].fillna("").astype(str).str.strip()
    missing_revision_id = out["revision_id"].eq("")
    out.loc[missing_revision_id, "revision_id"] = out.loc[missing_revision_id].apply(_canonical_revision_id, axis=1)
    return (
        out[ACTION_COLUMNS]
        .sort_values(
            ["symbol", "ex_date", "announcement_date", "action_id", "revised_at"],
            na_position="last",
        )
        .reset_index(drop=True)
    )


class CorporateActionLedger:
    """可审计、按可得日期查询的公司行为账本。"""

    def __init__(self, actions: pd.DataFrame):
        self.table = normalize_corporate_actions(actions)
        problems = self.validate()
        if problems:
            raise ValueError("invalid corporate action ledger: " + "; ".join(problems))

    def validate(self) -> list[str]:
        """返回合同问题，不静默修复经济字段或泄漏日期。"""
        df = self.table
        problems: list[str] = []
        if df["symbol"].isna().any() or df["symbol"].eq("nan").any():
            problems.append("missing symbol")
        if (~df["action_type"].isin(ACTION_TYPES)).any():
            bad = sorted(df.loc[~df["action_type"].isin(ACTION_TYPES), "action_type"].unique().tolist())
            problems.append(f"unknown action_type: {bad}")
        for column in ("announcement_date", "available_at", "revised_at", "ex_date"):
            if df[column].isna().any():
                problems.append(f"missing {column}")
        if df["revision_id"].duplicated().any():
            problems.append("duplicated revision_id")
        if (df["available_at"] < df["announcement_date"]).any():
            problems.append("available_at earlier than announcement_date")
        if (df["revised_at"] < df["announcement_date"]).any():
            problems.append("revised_at earlier than announcement_date")
        for column in ("cash_per_share", "share_ratio", "rights_ratio", "rights_price"):
            if (df[column].dropna() < 0).any():
                problems.append(f"negative {column}")
        needs_cash = df["action_type"].eq("cash_dividend") & df["cash_per_share"].isna()
        if needs_cash.any():
            problems.append("cash_dividend missing cash_per_share")
        needs_share = df["action_type"].isin({"stock_dividend", "split"}) & df["share_ratio"].isna()
        if needs_share.any():
            problems.append("stock_dividend/split missing share_ratio")
        needs_rights = df["action_type"].eq("rights_issue") & df["rights_ratio"].isna()
        if needs_rights.any():
            problems.append("rights_issue missing rights_ratio")
        return problems

    def as_of(self, date: str | pd.Timestamp, symbol: str | None = None) -> pd.DataFrame:
        """仅返回截至 ``date`` 已可得的每项公司行为的最新事实版本。"""
        ts = pd.Timestamp(date).normalize()
        out = self.table[(self.table["available_at"] <= ts) & (self.table["revised_at"] <= ts)]
        if symbol is not None:
            out = out[out["symbol"] == str(symbol).zfill(6)]
        return (
            out.sort_values(["action_id", "revised_at", "ingested_at", "revision_id"])
            .groupby("action_id", as_index=False, sort=False)
            .tail(1)
            .sort_values(["symbol", "ex_date", "action_id"])
            .reset_index(drop=True)
        )

    def effective_on(self, date: str | pd.Timestamp, symbol: str | None = None) -> pd.DataFrame:
        """返回在该日除权/实施且当时已知的行为，不使用未来修订。"""
        ts = pd.Timestamp(date).normalize()
        out = self.as_of(ts, symbol)
        return out[out["ex_date"] == ts].copy().reset_index(drop=True)

    def version(self) -> str:
        """由规范化内容生成稳定版本，便于快照与研究制品绑定。"""
        canonical = self.table.copy()
        for column in (
            "announcement_date",
            "available_at",
            "revised_at",
            "record_date",
            "ex_date",
            "ingested_at",
        ):
            canonical[column] = canonical[column].map(_date_text)
        canonical = canonical.sort_values(["action_id", "revised_at", "revision_id"]).fillna("")
        raw = canonical.to_json(orient="records", force_ascii=False, date_format="iso")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def save(self, path: Path | None = None) -> Path:
        target = Path(path) if path else CORPORATE_ACTION_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        self.table.to_parquet(target, index=False)
        logger.info("corporate action ledger saved: {} rows -> {}", len(self.table), target)
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> CorporateActionLedger:
        target = Path(path) if path else CORPORATE_ACTION_PATH
        if not target.exists():
            raise FileNotFoundError(f"corporate action ledger not found: {target}")
        return cls(pd.read_parquet(target))


def load_corporate_action_version(path: Path | None = None) -> str:
    """读取账本内容版本；缺文件时保持与主数据版本函数相同的失败语义。"""
    return CorporateActionLedger.load(path).version()


__all__ = [
    "ACTION_COLUMNS",
    "ACTION_TYPES",
    "CORPORATE_ACTION_PATH",
    "CorporateActionLedger",
    "load_corporate_action_version",
    "normalize_corporate_actions",
]

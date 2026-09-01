"""指数基准权重与股票暴露的 PIT 快照库。

文件为追加式快照表，而不是“今天的一份行业映射”。每个快照记录对应市场
``as_of``、实际可得时间 ``available_at``、来源和版本；在决策日只能选择当时
已经可得的完整版本。该库是指数增强/组合暴露约束的唯一数据入口。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from quart.config import data_root
from quart.risk.exposure import ExposureDataError, ExposureSnapshot

REQUIRED_COLUMNS = ("as_of", "available_at", "symbol", "benchmark_weight", "source", "version")


def exposure_history_path(index_code: str) -> Path:
    return data_root() / "exposures" / f"{index_code!s}_exposure_history.parquet"


class PITExposureStore:
    """完整快照版本的 in-memory 查询器。"""

    def __init__(self, history: pd.DataFrame):
        self.history = _normalize(history)

    @classmethod
    def load(cls, index_code: str, path: Path | None = None) -> PITExposureStore:
        target = path or exposure_history_path(index_code)
        if not target.exists():
            raise FileNotFoundError(
                f"缺少 {index_code} PIT 暴露历史: {target}；"
                "请先导入权重、行业、市值和风格快照"
            )
        return cls(pd.read_parquet(target))

    def save(self, index_code: str, path: Path | None = None) -> Path:
        target = path or exposure_history_path(index_code)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.history.to_parquet(target, index=False)
        return target

    def snapshot_at(self, decision_date: str | pd.Timestamp) -> ExposureSnapshot:
        """返回决策日可用的最新完整快照；无数据或版本歧义均 fail-closed。"""
        date = pd.Timestamp(decision_date).normalize()
        eligible = self.history[
            (self.history["as_of"] <= date) & (self.history["available_at"] <= date)
        ]
        if eligible.empty:
            raise ExposureDataError(f"{date.date()} 前没有可用的 PIT 暴露快照")
        versions = (
            eligible[["as_of", "available_at", "source", "version"]]
            .drop_duplicates()
            .sort_values(["as_of", "available_at", "source", "version"])
        )
        latest_time = versions[["as_of", "available_at"]].iloc[-1]
        latest = versions[
            (versions["as_of"] == latest_time["as_of"])
            & (versions["available_at"] == latest_time["available_at"])
        ]
        if len(latest) != 1:
            raise ExposureDataError(
                f"{date.date()} 的最新 PIT 暴露快照存在多个 source/version，无法确定唯一版本"
            )
        meta = latest.iloc[0]
        frame = eligible[
            (eligible["as_of"] == meta["as_of"])
            & (eligible["available_at"] == meta["available_at"])
            & (eligible["source"] == meta["source"])
            & (eligible["version"] == meta["version"])
        ].copy()
        if frame["symbol"].duplicated().any():
            dup = sorted(frame.loc[frame["symbol"].duplicated(keep=False), "symbol"].unique())
            raise ExposureDataError(f"暴露快照含重复 symbol: {dup}")
        return ExposureSnapshot(
            as_of=pd.Timestamp(meta["as_of"]),
            available_at=pd.Timestamp(meta["available_at"]),
            benchmark_weights=frame.set_index("symbol")["benchmark_weight"],
            industries=(frame.set_index("symbol")["industry"] if "industry" in frame.columns else None),
            market_caps=(frame.set_index("symbol")["market_cap"] if "market_cap" in frame.columns else None),
            style_exposures=_style_columns(frame),
            source=str(meta["source"]),
            version=str(meta["version"]),
        )


def _normalize(history: pd.DataFrame) -> pd.DataFrame:
    frame = history.copy()
    missing = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
    if missing:
        raise ExposureDataError(f"PIT 暴露历史缺少列: {missing}")
    frame["as_of"] = pd.to_datetime(frame["as_of"], errors="coerce").dt.normalize()
    frame["available_at"] = pd.to_datetime(frame["available_at"], errors="coerce").dt.normalize()
    frame["symbol"] = frame["symbol"].astype(str)
    frame["benchmark_weight"] = pd.to_numeric(frame["benchmark_weight"], errors="coerce")
    if frame[["as_of", "available_at", "benchmark_weight"]].isna().any().any():
        raise ExposureDataError("PIT 暴露历史的日期和 benchmark_weight 不能缺失")
    if (frame["benchmark_weight"] < 0).any():
        raise ExposureDataError("PIT 暴露历史 benchmark_weight 不能为负")
    # 允许信息在成分生效后披露；真正的前视由 snapshot_at(decision_date)
    # 对 available_at 严格过滤，而不是假设 as_of 当天即可获得。
    if frame["source"].astype(str).str.strip().eq("").any() or frame["version"].astype(str).str.strip().eq("").any():
        raise ExposureDataError("PIT 暴露历史 source 与 version 不能为空")
    key = ["as_of", "available_at", "source", "version", "symbol"]
    if frame.duplicated(key).any():
        raise ExposureDataError("PIT 暴露历史存在重复快照行")
    return frame.sort_values(key).reset_index(drop=True)


def _style_columns(frame: pd.DataFrame) -> pd.DataFrame | None:
    excluded = set(REQUIRED_COLUMNS) | {"industry", "market_cap"}
    columns = sorted(column for column in frame.columns if column not in excluded)
    if not columns:
        return None
    return frame.set_index("symbol")[columns]


__all__ = ["REQUIRED_COLUMNS", "PITExposureStore", "exposure_history_path"]

"""组合暴露的 PIT 数据合同与风险限额（RISK-002B）。

行业、市值和风格约束的数学计算在 ``PortfolioConstructor`` 中保持唯一事实
来源；本模块只负责让这些约束所需的基准与暴露数据以 Point-in-Time 合同进入
构建器。没有明确的可得时间、来源版本或完整覆盖时，必须 fail-closed，不能
退化为当前行业映射、等权基准或静态市值表。
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


class ExposureDataError(ValueError):
    """暴露数据不满足 PIT、版本或覆盖要求。"""


@dataclass(frozen=True, slots=True)
class ExposureLimits:
    """组合级行业、市值、风格主动暴露硬限额。"""

    industry_active_bounds: float | Mapping[str, float] | None = None
    market_cap_active_bound: float | None = None
    style_active_bounds: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        industry = self.industry_active_bounds
        if isinstance(industry, Mapping):
            if not industry or any(float(bound) < 0 for bound in industry.values()):
                raise ValueError("industry_active_bounds 映射不能为空且上限不能为负")
        elif industry is not None and float(industry) < 0:
            raise ValueError("industry_active_bounds 不能为负")
        if self.market_cap_active_bound is not None and float(self.market_cap_active_bound) < 0:
            raise ValueError("market_cap_active_bound 不能为负")
        if any(not str(name).strip() or float(bound) < 0 for name, bound in self.style_active_bounds.items()):
            raise ValueError("style_active_bounds 因子名不能为空且上限不能为负")

    @property
    def enabled(self) -> bool:
        return (
            self.industry_active_bounds is not None
            or self.market_cap_active_bound is not None
            or bool(self.style_active_bounds)
        )


@dataclass(frozen=True, slots=True)
class ExposureInputs:
    """经 PIT 校验、可直接传入 ``PortfolioConstructionInput`` 的数据。"""

    benchmark_weights: pd.Series
    industries: pd.Series | None
    market_caps: pd.Series | None
    style_exposures: pd.DataFrame | None
    source: str
    version: str
    available_at: pd.Timestamp


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    """某个已知时点可用的一版组合暴露数据。

    ``as_of`` 是数据对应市场时点，``available_at`` 是市场实际可使用时点；
    后者晚于决策日即构成前视，严禁用于策略构建。基准权重必须来自该快照，
    不能在无权重时由候选股票临时等权填补。
    """

    as_of: pd.Timestamp
    available_at: pd.Timestamp
    benchmark_weights: pd.Series
    industries: pd.Series | None = None
    market_caps: pd.Series | None = None
    style_exposures: pd.DataFrame | None = None
    source: str = ""
    version: str = ""

    def resolve(
        self,
        decision_date: str | pd.Timestamp,
        symbols: Collection[str],
        limits: ExposureLimits,
    ) -> ExposureInputs:
        """在决策日解析所需字段；缺失和前视均抛出明确异常。"""
        if not limits.enabled:
            raise ExposureDataError("未启用任何组合暴露限额，不应请求 ExposureSnapshot")
        date = pd.Timestamp(decision_date).normalize()
        as_of = pd.Timestamp(self.as_of).normalize()
        available = pd.Timestamp(self.available_at).normalize()
        if as_of > date:
            raise ExposureDataError(f"暴露快照 as_of={as_of.date()} 晚于决策日 {date.date()}")
        if available > date:
            raise ExposureDataError(
                f"暴露快照 available_at={available.date()} 晚于决策日 {date.date()}（前视）"
            )
        if not str(self.source).strip() or not str(self.version).strip():
            raise ExposureDataError("暴露快照必须记录 source 与 version")

        index = pd.Index(sorted({str(symbol) for symbol in symbols}, key=str))
        benchmark = _weights(self.benchmark_weights, index, "benchmark_weights")
        industries = None
        market_caps = None
        styles = None
        if limits.industry_active_bounds is not None:
            industries = _categories(self.industries, index, "industries")
        if limits.market_cap_active_bound is not None:
            market_caps = _positive(self.market_caps, index, "market_caps")
        if limits.style_active_bounds:
            styles = _styles(self.style_exposures, index, limits.style_active_bounds)
        return ExposureInputs(
            benchmark_weights=benchmark,
            industries=industries,
            market_caps=market_caps,
            style_exposures=styles,
            source=str(self.source),
            version=str(self.version),
            available_at=available,
        )


def parse_style_bounds(raw: str | Mapping[str, float] | None) -> dict[str, float]:
    """解析 ``factor=bound,...`` 参数，拒绝模糊或重复配置。"""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        parsed = {str(key).strip(): float(value) for key, value in raw.items()}
    else:
        parsed = {}
        text = str(raw).strip()
        if not text:
            return parsed
        for item in text.split(","):
            if "=" not in item:
                raise ValueError("style_active_bounds 应为 factor=bound[,factor=bound]")
            name, raw_bound = item.split("=", 1)
            name = name.strip()
            if not name or name in parsed:
                raise ValueError("style_active_bounds 因子名不能为空且不能重复")
            parsed[name] = float(raw_bound.strip())
    if any(not name or not np.isfinite(bound) or bound < 0 for name, bound in parsed.items()):
        raise ValueError("style_active_bounds 的上限必须是有限非负数")
    return dict(sorted(parsed.items()))


def _weights(value: pd.Series, index: pd.Index, name: str) -> pd.Series:
    series = pd.Series(value, dtype="float64").copy()
    series.index = series.index.map(str)
    series = series.reindex(index)
    if not np.isfinite(series).all() or (series < 0).any():
        missing = sorted(series[(~np.isfinite(series)) | (series < 0)].index)
        raise ExposureDataError(f"{name} 必须覆盖全部股票且为有限非负数: {missing}")
    if float(series.sum()) > 1.0 + 1e-10:
        raise ExposureDataError(f"{name} 总权重超过 100%")
    return series


def _categories(value: pd.Series | None, index: pd.Index, name: str) -> pd.Series:
    if value is None:
        raise ExposureDataError(f"启用行业暴露限额必须提供 {name}")
    series = pd.Series(value, dtype="object").copy()
    series.index = series.index.map(str)
    series = series.reindex(index)
    missing = series.isna() | (series.astype(str).str.strip() == "")
    if missing.any():
        raise ExposureDataError(f"{name} 缺少分类: {sorted(series[missing].index)}")
    return series.astype(str)


def _positive(value: pd.Series | None, index: pd.Index, name: str) -> pd.Series:
    if value is None:
        raise ExposureDataError(f"启用市值暴露限额必须提供 {name}")
    series = pd.Series(value, dtype="float64").copy()
    series.index = series.index.map(str)
    series = series.reindex(index)
    invalid = (~np.isfinite(series)) | (series <= 0)
    if invalid.any():
        raise ExposureDataError(f"{name} 必须覆盖全部股票且为正: {sorted(series[invalid].index)}")
    return series


def _styles(
    value: pd.DataFrame | None,
    index: pd.Index,
    bounds: Mapping[str, float],
) -> pd.DataFrame:
    if value is None:
        raise ExposureDataError("启用风格暴露限额必须提供 style_exposures")
    frame = value.copy()
    frame.index = frame.index.map(str)
    missing_columns = sorted(set(bounds) - set(frame.columns))
    if missing_columns:
        raise ExposureDataError(f"style_exposures 缺少因子: {missing_columns}")
    frame = frame.reindex(index=index, columns=sorted(bounds)).astype("float64")
    if not np.isfinite(frame.to_numpy()).all():
        raise ExposureDataError("style_exposures 必须覆盖全部股票且为有限数值")
    return frame


__all__ = [
    "ExposureDataError",
    "ExposureInputs",
    "ExposureLimits",
    "ExposureSnapshot",
    "parse_style_bounds",
]

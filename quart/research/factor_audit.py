"""Point-in-time factor audit with executable T+1 labels and stability diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.execution.constraints import LIMIT_TOLERANCE
from quart.research.event_factors import limit_event_panels, price_limit_panel


@dataclass(frozen=True)
class FactorSpec:
    name: str
    category: str
    description: str
    is_new: bool = False
    in_strategy: bool = False


@dataclass
class FactorAuditResult:
    summary: pd.DataFrame
    ic_history: pd.DataFrame
    correlation: pd.DataFrame
    metadata: dict
    baseline: pd.DataFrame = field(default_factory=pd.DataFrame)


FACTOR_SPECS = (
    FactorSpec("vol20_neg", "低波", "20 日收益波动率（负向）", in_strategy=True),
    FactorSpec("amp20_neg", "低波", "20 日平均振幅（负向）", in_strategy=True),
    FactorSpec("lottery20_neg", "尾部风险", "20 日最大单日收益（负向）", in_strategy=True),
    FactorSpec("rev5", "反转", "5 日收益反转", in_strategy=True),
    FactorSpec("mom60", "动量", "60 日价格动量"),
    FactorSpec("mom120", "动量", "120 日价格动量"),
    FactorSpec("sharpe_mom60", "动量", "60 日风险调整动量"),
    FactorSpec("downvol_ratio_neg", "低波", "下行波动占总波动比例（负向）"),
    FactorSpec("net_flow20", "量价", "20 日方向成交量净流入"),
    FactorSpec("pv_corr20_neg", "量价", "收益与成交额相关性（负向）"),
    FactorSpec("trend_eff_dir", "趋势质量", "有方向的 60 日价格效率"),
    FactorSpec("gap_avg", "隔夜", "20 日平均隔夜跳空"),
    FactorSpec("beta60_neg", "风险", "60 日市场 Beta（负向）"),
    FactorSpec("idio_vol60_neg", "风险", "60 日特质波动率（负向）"),
    FactorSpec("amihud20_neg", "流动性", "20 日 Amihud 冲击成本代理（负向）"),
    FactorSpec("vwap_pos20_neg", "量价", "收盘价相对 20 日 VWAP 位置（负向）"),
    FactorSpec("rel_ind_rev20", "行业相对", "20 日行业内相对收益反转"),
    FactorSpec("downside_semivol20_neg", "尾部风险", "20 日下行半方差（负向）", is_new=True),
    FactorSpec("downside_semivol60_neg", "尾部风险", "60 日下行半方差（负向）", is_new=True),
    FactorSpec("tail_loss60", "尾部风险", "60 日收益 10% 分位数（越高尾损越小）", is_new=True),
    FactorSpec("gap_vol20_neg", "隔夜", "20 日隔夜跳空波动（负向）", is_new=True),
    FactorSpec("amount_cv60_neg", "可执行性", "60 日成交额变异系数（负向）", is_new=True),
    FactorSpec("residual_downside60_neg", "尾部风险", "市场中性残差下行半方差（负向）", is_new=True),
    FactorSpec("size_neg", "市值", "流通市值（负向，小市值偏好）", is_new=True),
    FactorSpec("turn20_neg", "流动性", "20 日平均换手率（负向，低换手）", is_new=True),
    FactorSpec("ep_ttm", "价值", "市盈率倒数（仅盈利为正）", is_new=True),
    FactorSpec("bp", "价值", "市净率倒数（仅净资产为正）", is_new=True),
    FactorSpec("roe_stability", "财报质量", "过去八期 ROE 稳定性（负标准差）", is_new=True),
    FactorSpec("profit_accel", "财报质量", "净利润同比增速的报告期边际变化", is_new=True),
    FactorSpec(
        "earnings_surprise_proxy", "财报事件",
        "净利润同比增速减营收同比增速（非分析师 SUE）", is_new=True,
    ),
    FactorSpec("limit_hit_count20_neg", "事件拥挤", "20 日收盘涨停次数（负向）", is_new=True),
    FactorSpec("near_limit_count20_neg", "事件拥挤", "20 日接近涨停次数（负向）", is_new=True),
    FactorSpec(
        "speculative_crowding20_neg", "事件拥挤",
        "接近涨停程度 × 相对成交额的 20 日拥挤度（负向）", is_new=True,
    ),
    FactorSpec(
        "crowding_liq20_neg", "事件拥挤·容量化",
        "拥挤度 ÷ ADV 横截面分位（下限截断）——同等热度下流动性差者扣分更重，"
        "Top 篮子自动偏向高 ADV 股票（RESEARCH-002 容量复盘的正向解）", is_new=True,
    ),
    FactorSpec(
        "sector_heat20_neg", "板块拥挤",
        "个股投机热度聚合到一级行业的板块拥挤反向（板块层容量结构）", is_new=True,
    ),
)


def rank_correlation(left: pd.Series, right: pd.Series) -> float:
    """Spearman correlation without requiring SciPy at runtime."""
    joined = pd.concat([left.rename("left"), right.rename("right")], axis=1).dropna()
    if len(joined) < 2:
        return float("nan")
    left_rank = joined["left"].rank(method="average")
    right_rank = joined["right"].rank(method="average")
    if left_rank.nunique() < 2 or right_rank.nunique() < 2:
        return float("nan")
    return float(left_rank.corr(right_rank))


class FactorInputs:
    def __init__(self, market: MarketData):
        self.market = market
        self.close = market.close_val
        self.open = market.opens
        self.high = market.highs
        self.low = market.lows
        self.volume = market.volumes
        self.amount = market.amounts if market.amounts is not None else market.volumes * np.nan

    @cached_property
    def returns(self) -> pd.DataFrame:
        return self.close.pct_change(fill_method=None)

    @cached_property
    def market_return(self) -> pd.Series:
        return self.returns.mean(axis=1)

    @cached_property
    def beta60(self) -> pd.DataFrame:
        window = 60
        ret = self.returns
        market = self.market_return
        mean_ret = ret.rolling(window).mean()
        mean_market = market.rolling(window).mean()
        covariance = ret.mul(market, axis=0).rolling(window).mean() - mean_ret.mul(mean_market, axis=0)
        variance = (market.pow(2).rolling(window).mean() - mean_market.pow(2)).clip(lower=0)
        return covariance.div(variance.replace(0, np.nan), axis=0)

    @cached_property
    def residual_return(self) -> pd.DataFrame:
        return self.returns - self.beta60.mul(self.market_return, axis=0)

    @cached_property
    def fundamental_frames(self) -> dict[str, pd.DataFrame] | None:
        """PIT 基本面宽表（流通市值/换手率/估值），数据缺失时整体返回 None。"""
        try:
            from quart.data.fundamental import fundamental_wide

            return {
                column: fundamental_wide(column).reindex(
                    index=self.close.index, columns=self.close.columns
                )
                for column in ("float_mcap", "turn", "pe_ttm", "pb")
            }
        except (FileNotFoundError, ValueError):
            return None

    @cached_property
    def financial_candidate_frames(self) -> dict[str, pd.DataFrame] | None:
        """季频财报候选宽表；真实披露时间优先，缺失时用 120 天保守时滞。"""
        from quart.config import data_root
        from quart.research.value_growth import pit_panels

        path = data_root() / "factors" / "financials.parquet"
        if not path.exists():
            return None
        financials = pd.read_parquet(path)
        factors = ("roe_stability", "profit_accel", "earnings_surprise_proxy")
        panels = pit_panels(financials, self.close, factors=factors)
        return panels or None

    @cached_property
    def price_event_frames(self) -> dict[str, pd.DataFrame]:
        return limit_event_panels(self.market)

    def compute(self, name: str) -> pd.DataFrame | None:
        ret = self.returns
        close = self.close
        amount = self.amount

        if name == "vol20_neg":
            value = -ret.rolling(20).std()
        elif name == "amp20_neg":
            value = -((self.high - self.low) / close.shift(1).replace(0, np.nan)).rolling(20).mean()
        elif name == "lottery20_neg":
            value = -ret.rolling(20).max()
        elif name == "rev5":
            value = -close.pct_change(5, fill_method=None)
        elif name == "mom60":
            value = close.pct_change(60, fill_method=None)
        elif name == "mom120":
            value = close.pct_change(120, fill_method=None)
        elif name == "sharpe_mom60":
            value = close.pct_change(60, fill_method=None) / ret.rolling(60).std().replace(0, np.nan)
        elif name == "downvol_ratio_neg":
            downside = ret.clip(upper=0).rolling(20).std()
            value = -(downside / ret.abs().rolling(20).std().replace(0, np.nan))
        elif name == "net_flow20":
            value = (np.sign(ret) * self.volume).rolling(20).sum() / self.volume.rolling(20).sum()
        elif name == "pv_corr20_neg":
            value = -ret.rolling(20).corr(np.log(amount.where(amount > 0)))
        elif name == "trend_eff_dir":
            momentum = close.pct_change(60, fill_method=None)
            value = momentum.abs() / ret.abs().rolling(60).sum().replace(0, np.nan) * np.sign(momentum)
        elif name == "gap_avg":
            value = (self.open / close.shift(1).replace(0, np.nan) - 1.0).rolling(20).mean()
        elif name == "beta60_neg":
            value = -self.beta60
        elif name == "idio_vol60_neg":
            value = -self.residual_return.rolling(60).std()
        elif name == "amihud20_neg":
            value = -(ret.abs() / amount.replace(0, np.nan)).rolling(20).mean() * 1e9
        elif name == "vwap_pos20_neg":
            traded_shares = self.volume * 100.0
            vwap = amount.rolling(20).sum() / traded_shares.replace(0, np.nan).rolling(20).sum()
            value = -((close - vwap) / vwap.replace(0, np.nan))
        elif name == "rel_ind_rev20":
            value = self._relative_industry_reversal()
        elif name == "downside_semivol20_neg":
            value = -np.sqrt(ret.clip(upper=0).pow(2).rolling(20).mean())
        elif name == "downside_semivol60_neg":
            value = -np.sqrt(ret.clip(upper=0).pow(2).rolling(60).mean())
        elif name == "tail_loss60":
            value = ret.rolling(60).quantile(0.10)
        elif name == "gap_vol20_neg":
            gap = self.open / close.shift(1).replace(0, np.nan) - 1.0
            value = -gap.rolling(20).std()
        elif name == "amount_cv60_neg":
            mean = amount.rolling(60).mean()
            value = -(amount.rolling(60).std() / mean.replace(0, np.nan))
        elif name == "residual_downside60_neg":
            value = -np.sqrt(self.residual_return.clip(upper=0).pow(2).rolling(60).mean())
        elif name in ("size_neg", "turn20_neg", "ep_ttm", "bp"):
            frames = self.fundamental_frames
            if frames is None:
                return None
            if name == "size_neg":
                value = -np.log(frames["float_mcap"])
            elif name == "turn20_neg":
                value = -frames["turn"].rolling(20).mean()
            elif name == "ep_ttm":
                pe = frames["pe_ttm"]
                value = (1.0 / pe).where(pe > 0)
            else:
                pb = frames["pb"]
                value = (1.0 / pb).where(pb > 0)
        elif name in ("roe_stability", "profit_accel", "earnings_surprise_proxy"):
            frames = self.financial_candidate_frames
            if frames is None or name not in frames:
                return None
            value = frames[name].reindex(index=close.index, columns=close.columns)
        elif name in self.price_event_frames:
            value = self.price_event_frames[name]
        else:
            raise KeyError(f"unknown factor: {name}")

        if value is None:
            return None
        return value.replace([np.inf, -np.inf], np.nan).astype("float32")

    def _relative_industry_reversal(self) -> pd.DataFrame | None:
        try:
            from quart.strategy.industries import load_industry_series

            mapping = load_industry_series("first")
        except (FileNotFoundError, ValueError):
            return None
        relative_return = self.close.pct_change(20, fill_method=None)
        groups = pd.Series(
            [mapping.get(symbol, "UNKNOWN") for symbol in relative_return.columns],
            index=relative_return.columns,
        )
        industry_return = relative_return.T.groupby(groups).mean().T
        broadcast = industry_return.reindex(columns=groups.values)
        broadcast.columns = relative_return.columns
        return -(relative_return - broadcast)


def _sample_positions(
    dates: pd.DatetimeIndex,
    sample: str,
    horizon: int,
    warmup: int,
) -> list[int]:
    if sample == "monthly":
        positions = pd.Series(range(len(dates)), index=dates)
        candidates = positions.groupby([dates.year, dates.month]).last().tolist()
    elif sample == "weekly":
        candidates = list(range(warmup, len(dates), 5))
    else:
        raise ValueError("sample must be monthly or weekly")
    return [int(i) for i in candidates if i >= warmup and i + horizon + 1 < len(dates)]


def _pairwise_correlation(vectors: dict[str, np.ndarray]) -> pd.DataFrame:
    names = list(vectors)
    output = pd.DataFrame(np.nan, index=names, columns=names, dtype="float64")
    for left_index, left_name in enumerate(names):
        output.loc[left_name, left_name] = 1.0
        left = vectors[left_name]
        for right_name in names[left_index + 1 :]:
            right = vectors[right_name]
            valid = np.isfinite(left) & np.isfinite(right)
            if valid.sum() >= 100 and np.nanstd(left[valid]) > 0 and np.nanstd(right[valid]) > 0:
                corr = float(np.corrcoef(left[valid], right[valid])[0, 1])
            else:
                corr = np.nan
            output.loc[left_name, right_name] = corr
            output.loc[right_name, left_name] = corr
    return output


def _classify(row: pd.Series) -> str:
    stable = (
        row["ic"] >= 0.02
        and row["icir"] >= 0.40
        and row["fdr_qvalue"] <= 0.10
        and row["positive_rate"] >= 0.55
        and row["early_ic"] > 0
        and row["late_ic"] > 0
        and row["recent_ic"] > 0
        and row["coverage"] >= 0.50
        and row["n_samples"] >= 12
    )
    if stable:
        return "候选" if row["max_abs_corr"] < 0.85 else "冗余候选"
    if row["ic"] > 0 and row["late_ic"] > 0 and row["coverage"] >= 0.30:
        return "观察"
    return "淘汰"


def _benjamini_hochberg(pvalues: pd.Series) -> pd.Series:
    """Control false discoveries across the audited factor family."""
    output = pd.Series(np.nan, index=pvalues.index, dtype="float64")
    valid = pvalues.dropna().clip(0.0, 1.0)
    if valid.empty:
        return output
    ordered = valid.sort_values()
    ranks = np.arange(1, len(ordered) + 1, dtype="float64")
    adjusted = ordered.to_numpy() * len(ordered) / ranks
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1].clip(0.0, 1.0)
    output.loc[ordered.index] = adjusted
    return output


def _return_path_stats(returns_bp: pd.Series, periods_per_year: float) -> tuple[float, float]:
    """Annualized return and max drawdown for non-overlapping audit baskets.

    The factor-audit samples are at least one holding horizon apart.  This is a
    descriptive baseline for the label portfolio, not a simulated strategy: it
    intentionally excludes fees, position limits, and execution shortfall.
    """
    returns = pd.to_numeric(returns_bp, errors="coerce").dropna() / 10_000.0
    if returns.empty:
        return float("nan"), float("nan")
    curve = (1.0 + returns).cumprod()
    annualized = float(curve.iloc[-1] ** (periods_per_year / len(curve)) - 1.0)
    drawdown = curve.div(curve.cummax()).sub(1.0)
    return annualized, float(drawdown.min())


def _provisional_baseline(history: pd.DataFrame, sample: str) -> pd.DataFrame:
    """Summarize the current-snapshot factor baskets as explicitly provisional.

    Capacity is intentionally a conservative *liquidity proxy*: without a
    portfolio notional, historical free float, and an impact model it would be
    misleading to claim an investable capacity number.
    """
    if history.empty:
        return pd.DataFrame()
    periods_per_year = 12.0 if sample == "monthly" else 243.0 / 5.0
    rows: list[dict] = []
    for factor, frame in history.groupby("factor", sort=False):
        frame = frame.sort_values("date")
        top_cagr, top_mdd = _return_path_stats(frame["top_abs_bp"], periods_per_year)
        universe_cagr, universe_mdd = _return_path_stats(
            frame["eligible_equal_weight_bp"], periods_per_year
        )
        # Relative compounding is valid even when the arithmetic excess return
        # is below -100%; directly compounding ``top - equal_weight`` is not.
        top_returns = pd.to_numeric(frame["top_abs_bp"], errors="coerce") / 10_000.0
        universe_returns = pd.to_numeric(frame["eligible_equal_weight_bp"], errors="coerce") / 10_000.0
        relative_bp = ((1.0 + top_returns) / (1.0 + universe_returns) - 1.0) * 10_000.0
        relative_cagr, relative_mdd = _return_path_stats(relative_bp, periods_per_year)
        rows.append(
            {
                "factor": factor,
                "n_samples": len(frame),
                "top_label_cagr": top_cagr,
                "top_label_max_drawdown": top_mdd,
                "eligible_equal_weight_cagr": universe_cagr,
                "eligible_equal_weight_max_drawdown": universe_mdd,
                "top_vs_eligible_cagr": relative_cagr,
                "top_vs_eligible_max_drawdown": relative_mdd,
                "mean_long_only_bp": float(frame["long_only_bp"].mean()),
                "annualized_top_turnover": float(frame["top_turnover"].mean() * periods_per_year),
                "median_top_adv_m": float(frame["top_median_amount_m"].median()),
                "capacity_proxy_m": float(frame["top_median_amount_m"].median() * 0.10),
                "capacity_proxy_assumption": "10% of median top-basket 20d ADV; not investable capacity",
                "research_status": "provisional",
            }
        )
    return pd.DataFrame(rows)


def run_factor_audit(
    market: MarketData,
    *,
    sample: str = "monthly",
    horizon: int = 5,
    min_amount: float = 20_000_000,
    min_cross_section: int = 100,
    warmup: int = 260,
    factor_names: list[str] | None = None,
    evaluation_start: str | pd.Timestamp | None = None,
    evaluation_end: str | pd.Timestamp | None = None,
) -> FactorAuditResult:
    """Evaluate factors against a close-T to open-T+1 executable return label."""
    if horizon < 1:
        raise ValueError("horizon must be positive")
    positions = _sample_positions(market.dates, sample, horizon, warmup)
    if evaluation_start is not None:
        start_ts = pd.Timestamp(evaluation_start)
        positions = [i for i in positions if market.dates[i] >= start_ts]
    if evaluation_end is not None:
        end_ts = pd.Timestamp(evaluation_end)
        positions = [i for i in positions if market.dates[i] <= end_ts]
    if not positions:
        raise ValueError("not enough history for requested warmup/horizon/evaluation range")

    specs = [spec for spec in FACTOR_SPECS if factor_names is None or spec.name in factor_names]
    unknown = set(factor_names or []) - {spec.name for spec in FACTOR_SPECS}
    if unknown:
        raise KeyError(f"unknown factors: {sorted(unknown)}")

    entry_open = market.opens.shift(-1)
    exit_open = market.opens.shift(-(horizon + 1))
    label = exit_open / entry_open.replace(0, np.nan) - 1.0
    amount = market.amounts if market.amounts is not None else market.volumes * np.nan
    average_amount = amount.rolling(20).mean()
    eligible = average_amount >= float(min_amount)
    limit_pct = price_limit_panel(market.dates, market.symbols).astype("float64")
    entry_limit = market.close_val.mul(1.0 + limit_pct.shift(-1))
    exit_limit = market.close_val.shift(-horizon).mul(
        1.0 - limit_pct.shift(-(horizon + 1))
    )
    executable = (
        (market.volumes.shift(-1).fillna(0) > 0)
        & (market.volumes.shift(-(horizon + 1)).fillna(0) > 0)
        & entry_open.lt(entry_limit - LIMIT_TOLERANCE)
        & exit_open.gt(exit_limit + LIMIT_TOLERANCE)
    )
    inputs = FactorInputs(market)

    history_rows: list[dict] = []
    summary_rows: list[dict] = []
    rank_vectors: dict[str, np.ndarray] = {}

    for spec in specs:
        factor = inputs.compute(spec.name)
        if factor is None:
            continue
        factor = factor.reindex(index=market.dates, columns=market.symbols)
        sample_rank = factor.iloc[positions].where(eligible.iloc[positions]).rank(axis=1, pct=True)
        rank_vectors[spec.name] = sample_rank.to_numpy(dtype="float32", copy=False).reshape(-1)

        factor_history: list[dict] = []
        previous_top: set[str] | None = None
        for position in positions:
            eligible_row = (eligible.iloc[position] & executable.iloc[position]).fillna(False)
            eligible_count = int(eligible_row.sum())
            joined = pd.DataFrame(
                {"factor": factor.iloc[position], "forward": label.iloc[position]}
            ).loc[eligible_row].dropna()
            if len(joined) < min_cross_section:
                continue
            values = joined["factor"]
            returns = joined["forward"].clip(-0.5, 2.0)
            ic = rank_correlation(values, returns)
            bucket_size = max(1, math.ceil(len(values) * 0.10))
            ordered = values.sort_values()
            top_symbols = ordered.tail(bucket_size).index
            bottom_symbols = ordered.head(bucket_size).index
            top_return = float(returns.loc[top_symbols].mean())
            bottom_return = float(returns.loc[bottom_symbols].mean())
            equal_weight_return = float(returns.mean())
            top_set = set(map(str, top_symbols))
            turnover = (
                1.0 - len(top_set & previous_top) / max(len(top_set), 1)
                if previous_top is not None
                else np.nan
            )
            previous_top = top_set
            row = {
                "date": market.dates[position],
                "factor": spec.name,
                "ic": ic,
                "top_abs_bp": top_return * 10_000,
                "eligible_equal_weight_bp": equal_weight_return * 10_000,
                "long_only_bp": (top_return - equal_weight_return) * 10_000,
                "long_short_bp": (top_return - bottom_return) * 10_000,
                "top_turnover": turnover,
                "top_median_amount_m": float(
                    average_amount.iloc[position].reindex(top_symbols).median() / 1_000_000
                ),
                "coverage": len(joined) / eligible_count if eligible_count else 0.0,
                "n_stocks": len(joined),
            }
            factor_history.append(row)
            history_rows.append(row)

        if not factor_history:
            continue
        frame = pd.DataFrame(factor_history)
        ic_series = frame["ic"].dropna()
        if ic_series.empty:
            continue
        half = max(len(ic_series) // 2, 1)
        standard_deviation = float(ic_series.std(ddof=1))
        summary_rows.append(
            {
                "factor": spec.name,
                "category": spec.category,
                "description": spec.description,
                "is_new": spec.is_new,
                "in_strategy": spec.in_strategy,
                "ic": float(ic_series.mean()),
                "ic_std": standard_deviation,
                "icir": float(ic_series.mean() / standard_deviation) if standard_deviation > 0 else np.nan,
                "ic_tstat": (
                    float(ic_series.mean() / (standard_deviation / math.sqrt(len(ic_series))))
                    if standard_deviation > 0
                    else np.nan
                ),
                "positive_rate": float((ic_series > 0).mean()),
                "early_ic": float(ic_series.iloc[:half].mean()),
                "late_ic": float(ic_series.iloc[half:].mean()),
                "recent_ic": float(ic_series.tail(min(12, len(ic_series))).mean()),
                "top_abs_bp": float(frame["top_abs_bp"].mean()),
                "long_only_bp": float(frame["long_only_bp"].mean()),
                "long_short_bp": float(frame["long_short_bp"].mean()),
                "top_turnover": float(frame["top_turnover"].mean()),
                "top_median_amount_m": float(frame["top_median_amount_m"].median()),
                "coverage": float(frame["coverage"].mean()),
                "avg_stocks": float(frame["n_stocks"].mean()),
                "n_samples": len(ic_series),
            }
        )

    summary = pd.DataFrame(summary_rows)
    history = pd.DataFrame(history_rows)
    baseline = _provisional_baseline(history, sample)
    correlation = _pairwise_correlation(rank_vectors) if rank_vectors else pd.DataFrame()
    if not summary.empty:
        max_corr = {}
        corr_peer = {}
        for factor in summary["factor"]:
            peers = correlation.loc[factor].drop(index=factor, errors="ignore").abs().dropna()
            max_corr[factor] = float(peers.max()) if not peers.empty else 0.0
            corr_peer[factor] = str(peers.idxmax()) if not peers.empty else ""
        summary["max_abs_corr"] = summary["factor"].map(max_corr)
        summary["corr_peer"] = summary["factor"].map(corr_peer)
        summary["ic_pvalue"] = summary["ic_tstat"].map(
            lambda value: math.erfc(abs(float(value)) / math.sqrt(2.0))
            if pd.notna(value)
            else np.nan
        )
        summary["fdr_qvalue"] = _benjamini_hochberg(summary["ic_pvalue"])
        summary["status"] = summary.apply(_classify, axis=1)
        status_order = {"候选": 0, "冗余候选": 1, "观察": 2, "淘汰": 3}
        summary["_status_order"] = summary["status"].map(status_order).fillna(99)
        summary = (
            summary.sort_values(["_status_order", "icir", "ic"], ascending=[True, False, False])
            .drop(columns="_status_order")
            .reset_index(drop=True)
        )

    metadata = {
        "generated_at": datetime.now().isoformat(),
        "data_first_date": str(market.dates.min().date()),
        "data_last_date": str(market.dates.max().date()),
        "evaluation_first_date": str(market.dates[positions[0]].date()),
        "evaluation_last_date": str(market.dates[positions[-1]].date()),
        "evaluation_requested_start": (
            None if evaluation_start is None else str(pd.Timestamp(evaluation_start).date())
        ),
        "evaluation_requested_end": (
            None if evaluation_end is None else str(pd.Timestamp(evaluation_end).date())
        ),
        "symbols": len(market.symbols),
        "sample": sample,
        "sample_points": len(positions),
        "horizon": int(horizon),
        "label": f"signal close T; executable open T+1 to open T+{horizon + 1}",
        "execution_filter": "entry/exit volume > 0 and not opening at board price limit",
        "long_only_metric": "top decile return minus eligible equal-weight return",
        "min_amount": float(min_amount),
        "min_cross_section": int(min_cross_section),
        "factor_count": len(summary),
        "research_status": "provisional",
        "provisional_reason": (
            "PIT universe/security-status history and actual disclosure timestamps are not complete; "
            "this result must not change the live allowlist or admission status"
        ),
        "baseline_definition": (
            "non-overlapping top-decile label baskets versus eligible equal-weight labels; "
            "gross of fees and execution shortfall"
        ),
    }
    return FactorAuditResult(summary, history, correlation, metadata, baseline)

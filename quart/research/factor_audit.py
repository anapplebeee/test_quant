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
    FactorSpec("rel_ind_mom20", "行业相对", "20 日行业内相对收益动量（行业中性动量，N1 候选）", is_new=True),
    FactorSpec("rel_ind_rev60", "行业相对", "60 日行业内相对收益反转（长窗口持续性检验）", is_new=True),
    FactorSpec("overnight_rev10", "隔夜", "10 日隔夜收益反转（开盘相对前收的均值回复）", is_new=True),
    FactorSpec("intraday_rev10_neg", "日内", "10 日日内收益反转（收盘相对开盘，负向）", is_new=True),
    FactorSpec("close_pos20_neg", "量价", "20 日收盘在日内区间位置（负向，收盘偏高=超买）", is_new=True),
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
    FactorSpec(
        "director_sale_support_neg", "内部人减持",
        "董事/高管/实控人减持窗口内累计相对强度（负向——减持支撑撤走后回吐）",
        is_new=True,
    ),
    # ---- 三层特征（RESEARCH-009：大盘/板块/个股，横截面审计）----
    # 板块层：板块动量相对全市场强度，broadcast 回板块内个股。同一板块内个股
    # 同分（真实预测维度是“选对赛道”），与剔板块的 rel_ind_rev20（选板块内
    # 强者）正交分解——“板块特征”与“个股特征”。
    FactorSpec(
        "sector_mom20", "板块轮动",
        "板块 20 日动量相对全市场（broadcast 板块内个股，选对赛道）", is_new=True,
    ),
    # 板块层短期反转：板块 5 日动量过热易回吐（A 股行业轮动短期反转），broadcast。
    FactorSpec(
        "sector_mom5_neg", "板块轮动",
        "板块 5 日动量过热反向（broadcast，板块短期轮动反转）", is_new=True,
    ),
    # ---- R010 新因子挖掘（平台未覆盖的新维度，ICIR 强于现有因子）----
    # 挖掘审计（2021-2026 月度、65 样本、5 日 horizon）实测：
    #   gap_fill20 IC=-0.065/ICIR=-0.66、amount_concen20 -0.037/-0.64、
    #   vol_asym60 -0.062/-0.56，均反向（原值越高未来收益越低）。
    # 三者刻画“价格韧性/成交脉冲/波动不对称”，与既有低波因子低相关（新 alpha 源）。
    FactorSpec(
        "gap_fill20_neg", "价格韧性",
        "跳空回补强度反向（缺口不回补=趋势延续，回补频繁=弱势震荡）", is_new=True,
    ),
    FactorSpec(
        "amount_concen20_neg", "成交脉冲",
        "成交额集中度反向（成交脉冲式集中=投机，未来回吐）", is_new=True,
    ),
    FactorSpec(
        "vol_asym60_neg", "波动不对称",
        "上行/下行波动不对称反向（上行波动过大=过热）", is_new=True,
    ),
)


def _winsorize(frame: pd.DataFrame, lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    """横截面分位数去极值（R010 新因子必需）。

    比率类因子（如 gap_fill 回补幅度、波动不对称 up/down）对停牌、一字板、
    长期停牌复牌等特殊股票会产生极端病态值——未去极值时组合优化会想给这些
    股票超高权重，直接触发"单票上限被违反"而构建失败。按每日截面的 1%/99%
    分位裁剪，保留排序信息、消除量纲病态。
    """
    low_q = frame.quantile(lower, axis=1)
    high_q = frame.quantile(upper, axis=1)
    return frame.clip(lower=low_q, upper=high_q, axis=0).astype("float32")


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
    #: R010 挖掘的新 alpha 因子（已取负向，统一由 new_alpha_frames 提供）
    NEW_ALPHA_FACTORS = frozenset(
        {"gap_fill20_neg", "amount_concen20_neg", "vol_asym60_neg"}
    )

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

    @cached_property
    def new_alpha_frames(self) -> dict[str, pd.DataFrame]:
        """R010 挖掘的新 alpha 面板（已取负向，值越高越优）。

        三个因子均只用 ≤T 的量价数据（T 收盘形成、T+1 可执行，与平台不变量 1
        一致）。挖掘审计（2021-2026 月度、65 样本、5 日 horizon）显示三者原始
        值与未来收益**负相关**、且 ICIR 强于既有因子：
          gap_fill20 -0.66 / amount_concen20 -0.64 / vol_asym60 -0.56。
        故统一取负后作为正向选股因子（neg 后缀）。
        """
        close = self.close
        open_ = self.open
        ret = self.returns
        amount = self.amount

        # 1. 跳空回补强度：向下跳空当日，收盘相对开盘的回补幅度均值。
        #    无跳空的日子填中性值 0（"没有缺口可回补"=中性，而非缺失）——否则该
        #    因子只在少数有跳空的股票上有值（实测覆盖仅 1357/3264），会把候选池
        #    系统性扭曲，导致组合优化因候选不足而单票超限。
        gap = open_ - close.shift(1)
        intraday_move = (close - open_) / open_.replace(0, np.nan)
        gap_fill = (
            intraday_move.where(gap < 0).fillna(0.0).rolling(20, min_periods=10).mean()
        )

        # 2. 成交额集中度：20 日内最大单日成交额 / 20 日成交额之和
        amount_concen = amount.rolling(20).max() / amount.rolling(20).sum().replace(0, np.nan)

        # 3. 上下行波动不对称：上行收益波动 / 下行收益波动
        up_vol = ret.clip(lower=0).rolling(60).std()
        down_vol = (-ret.clip(upper=0)).rolling(60).std()
        vol_asym = up_vol / down_vol.replace(0, np.nan)

        return {
            "gap_fill20_neg": _winsorize(-gap_fill),
            "amount_concen20_neg": _winsorize(-amount_concen),
            "vol_asym60_neg": _winsorize(-vol_asym),
        }

    @cached_property
    def director_sale_frames(self) -> dict[str, pd.DataFrame] | None:
        """董事减持拉升因子面板；事件文件缺失时返回 None（fail，不合成）。

        PROVISIONAL：仅当 news.parquet（事件合同）存在时计算；active mask
        与拉升面板同序返回，供审计做事件活跃等权基线对比。
        """
        try:
            from quart.config import data_root
            from quart.data.announcements import build_event_frame
            from quart.research.event_factors import director_sale_support_panels

            path = data_root() / "events" / "news.parquet"
            if not path.exists():
                return None
            raw = pd.read_parquet(path)
            events = raw if "event_type" in raw else build_event_frame(raw)
            return director_sale_support_panels(
                events,
                self.close.index,
                self.close.columns,
                returns=self.returns,
            )
        except Exception:
            # 数据/映射缺失：审计管道对该因子返回不可用，不合成观测。
            return None

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
        elif name == "rel_ind_mom20":
            value = self._relative_industry_momentum()
        elif name == "rel_ind_rev60":
            value = self._relative_industry_reversal(window=60)
        elif name == "overnight_rev10":
            overnight = self.open / close.shift(1).replace(0, np.nan) - 1.0
            value = -overnight.rolling(10).mean()
        elif name == "intraday_rev10_neg":
            intraday = close / self.open.replace(0, np.nan) - 1.0
            value = -intraday.rolling(10).mean()
        elif name == "close_pos20_neg":
            span = (self.high - self.low).replace(0, np.nan)
            position = (close - self.low) / span
            value = -position.rolling(20).mean()
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
        elif name == "director_sale_support_neg":
            frames = self.director_sale_frames
            if frames is None:
                return None
            value = frames["director_sale_support_neg"]
        elif name == "sector_mom20":
            value = self._sector_momentum_relative(window=20)
        elif name == "sector_mom5_neg":
            value = self._sector_momentum_relative(window=5)
            if value is not None:
                value = -value
        elif name in self.NEW_ALPHA_FACTORS:
            value = self.new_alpha_frames[name]
        else:
            raise KeyError(f"unknown factor: {name}")

        if value is None:
            return None
        return value.replace([np.inf, -np.inf], np.nan).astype("float32")

    def _sector_momentum_relative(self, window: int = 20) -> pd.DataFrame | None:
        """板块动量相对强度（broadcast）：板块窗口收益 − 全市场窗口收益。

        “板块特征”层的横截面表达：把每只股票替换为所属板块（统计聚类/申万
        一级，随 industries 源）在 window 日的窗口收益，再减去当日全市场等权
        窗口收益做中性化。同一板块内个股得分相同——真实预测维度是“选对赛道”。

        时点安全：只用 ≤T 的收盘价算窗口收益，信号 T 收盘形成、T+1 才可执行，
        与 `_relative_industry_reversal` 同口径。映射缺失时返回 None（fail-closed，
        调用方明确跳过该因子，不静默替代）。
        """
        try:
            from quart.strategy.industries import load_industry_series

            mapping = load_industry_series("first")
        except (FileNotFoundError, ValueError):
            return None
        window_return = self.close.pct_change(window, fill_method=None)
        if window_return is None or window_return.empty:
            return None
        groups = pd.Series(
            [mapping.get(symbol, "UNKNOWN") for symbol in window_return.columns],
            index=window_return.columns,
        )
        sector_return = window_return.T.groupby(groups).mean().T
        market_return = window_return.mean(axis=1)
        broadcast = sector_return.sub(market_return, axis=0)
        out = broadcast.reindex(columns=groups.values)
        out.columns = window_return.columns
        return out

    def _relative_industry_reversal(self, window: int = 20) -> pd.DataFrame | None:
        try:
            from quart.strategy.industries import load_industry_series

            mapping = load_industry_series("first")
        except (FileNotFoundError, ValueError):
            return None
        relative_return = self.close.pct_change(window, fill_method=None)
        groups = pd.Series(
            [mapping.get(symbol, "UNKNOWN") for symbol in relative_return.columns],
            index=relative_return.columns,
        )
        industry_return = relative_return.T.groupby(groups).mean().T
        broadcast = industry_return.reindex(columns=groups.values)
        broadcast.columns = relative_return.columns
        return -(relative_return - broadcast)

    def _relative_industry_momentum(self, window: int = 20) -> pd.DataFrame | None:
        """行业中性动量：个股窗口收益减其所属行业（申万一级/统计聚类）
        等权平均窗口收益。与 ``rel_ind_rev{window}`` 互为反方向；A 股实证
        通常显示行业内反转更稳定（ICIR 更高），故本因子方向需以单因子检验定夺。
        """
        try:
            from quart.strategy.industries import load_industry_series

            mapping = load_industry_series("first")
        except (FileNotFoundError, ValueError):
            return None
        relative_return = self.close.pct_change(window, fill_method=None)
        groups = pd.Series(
            [mapping.get(symbol, "UNKNOWN") for symbol in relative_return.columns],
            index=relative_return.columns,
        )
        industry_return = relative_return.T.groupby(groups).mean().T
        broadcast = industry_return.reindex(columns=groups.values)
        broadcast.columns = relative_return.columns
        return relative_return - broadcast


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

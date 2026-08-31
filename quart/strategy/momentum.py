from __future__ import annotations

import pandas as pd

from quart.data.market import MarketData
from quart.execution.constraints import FLAT
from quart.research.momentum import compute_momentum_factor
from quart.strategy.base import BaseStrategy
from quart.strategy.filters import apply_liquidity, regime_flat_series


class MomentumRotationStrategy(BaseStrategy):
    """Rank by N-day momentum, hold top-k equally weighted, rebalance every k days.

    Optional regime filter: go to cash when benchmark close is below its MA
    (with hysteresis band to avoid whipsaw around the MA).
    """

    name = "momentum_rotation"
    required_history_days = 61

    PARAMS_SCHEMA = {
        "lookback_days": (int, 60, "动量回看窗口（交易日）"),
        "momentum_mode": (str, "simple", "动量口径 simple/rank/smooth/remove_limit_up"),
        "momentum_skip_days": (int, 0, "跳过最近几日，避免短期反转污染"),
        "limit_up_threshold": (float, 0.095, "剔除涨停日动量阈值"),
        "top_k": (int, 10, "持仓数量"),
        "rebalance_days": (int, 5, "调仓周期（交易日）"),
        "max_weight_pct": (float, 0.15, "单票权重上限"),
        "min_avg_amount": ((int, float, type(None)), None, "流动性门槛（N日均成交额）"),
        "liquidity_days": (int, 20, "流动性回看窗口"),
        "min_price": ((int, float, type(None)), None, "最低价过滤"),
        "industry_neutral": (bool, False, "是否行业中性化"),
        "industry_level": (str, "first", "行业层级 first/second/third"),
        "use_regime_filter": (bool, True, "是否启用指数择时"),
        "regime_mode": (str, "ma", "择时模式: ma=均线, score=R4多因子打分分级仓位"),
        "timing_levels": (int, 3, "score 模式档位数（2=全仓/空仓, 3=加半仓档）"),
        "regime_filter_days": (int, 20, "择时均线窗口"),
        "regime_band": (float, 0.0, "择时迟滞带宽度（动量策略默认关闭）"),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.required_history_days = max(
            int(self.params.get("lookback_days", 60))
            + int(self.params.get("momentum_skip_days", 0)),
            int(self.params.get("regime_filter_days", 20)),
        ) + 1

    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        p = self.params
        self.lookback = int(p.get("lookback_days", 60))
        self.momentum_mode = str(p.get("momentum_mode", "simple"))
        self.momentum_skip_days = int(p.get("momentum_skip_days", 0))
        self.limit_up_threshold = float(p.get("limit_up_threshold", 0.095))
        self.top_k = int(p.get("top_k", 10))
        self.rebalance_days = int(p.get("rebalance_days", 5))
        self.max_weight = float(p.get("max_weight_pct", 0.15))
        self.min_avg_amount = p.get("min_avg_amount")
        self.liquidity_days = int(p.get("liquidity_days", 20))
        self.min_price = p.get("min_price")
        self.industry_neutral = bool(p.get("industry_neutral", False))
        self.industry_level = str(p.get("industry_level", "first"))
        self.use_regime = bool(p.get("use_regime_filter", True))
        self.regime_mode = str(p.get("regime_mode", "ma"))
        self.timing_levels = int(p.get("timing_levels", 3))
        self.regime_days = int(p.get("regime_filter_days", 20))
        # 缓冲带对动量策略有害（实测 2020-2026: band=0.02 使收益 -56%→-82%，
        # 高波动持仓下延迟逃命的代价 > 节省的切换摩擦），默认 0 保持即时切换
        self.regime_band = float(p.get("regime_band", 0.0))
        self.warmup = max(
            self.lookback + self.momentum_skip_days,
            self.regime_days,
        ) + 1
        self.required_history_days = self.warmup
        # 路径动量统一从 research 层计算，保证因子研究和策略选股口径一致。
        # 停牌缺口不填充，避免把停牌期算成 0 收益歪曲动量。
        self.momentum = compute_momentum_factor(
            md,
            mode=self.momentum_mode,
            window=self.lookback,
            skip_days=self.momentum_skip_days,
            limit_up_threshold=self.limit_up_threshold,
        )
        self.regime_ma = (
            md.benchmark_close.rolling(self.regime_days).mean()
            if md.benchmark_close is not None
            else None
        )
        # 带缓冲带的择时序列：MA 附近穿越从 ~26 次/年降到 ~8 次/年，减少全清全建摩擦
        self.regime_flat = (
            regime_flat_series(md.benchmark_close, self.regime_ma, self.regime_band)
            if self.regime_ma is not None
            else None
        )
        # regime_mode="score"：R4 多因子打分分级仓位（广度/量能/价量/动量投票），
        # 0=空仓、中间档按比例缩放权重（余下留现金）、1=满仓；与 MA 模式互斥
        self.timing_exposure = None
        if self.use_regime and self.regime_mode == "score":
            from quart.strategy.timing import score_timing_exposure

            self.timing_exposure = score_timing_exposure(
                md, levels=self.timing_levels, breadth_ma_window=self.regime_days
            )
        self._next_rebalance = self.warmup

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if i < self.warmup or i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days

        if self.use_regime and self.regime_flat is not None:
            if bool(self.regime_flat.iloc[i]):
                return {FLAT: 1.0}

        exposure = 1.0
        if self.use_regime and self.timing_exposure is not None:
            exposure = float(self.timing_exposure.iloc[i])
            if exposure <= 0:
                return {FLAT: 1.0}

        scores = self.momentum.iloc[i].dropna()
        volume_row = md.volumes.iloc[i]
        tradable = volume_row[volume_row.fillna(0) > 0].index
        scores = scores.loc[scores.index.intersection(tradable)]
        scores = apply_liquidity(scores, md, i, self.min_avg_amount, self.liquidity_days, self.min_price)
        if self.industry_neutral:
            from quart.strategy.industries import industry_neutralize, load_industry_series
            try:
                industries = load_industry_series(self.industry_level)
            except FileNotFoundError:
                industries = pd.Series(dtype=object)
            scores = industry_neutralize(scores, industries)
        if len(scores) < self.top_k:
            return {}
        top = scores.nlargest(self.top_k)
        weight = min(1.0 / len(top), self.max_weight)
        # 分级仓位：0<exposure<1 时权重按比例缩减，余下留现金（R4 仓位管理）
        return {sym: weight * exposure for sym in top.index}

    def state_dict(self):
        return {"next_rebalance": int(self._next_rebalance)}

    def load_state_dict(self, state):
        super().load_state_dict(state)
        if state and "next_rebalance" in state:
            self._next_rebalance = int(state["next_rebalance"])


class PathMomentumStrategy(MomentumRotationStrategy):
    """研报路径动量的显式研究入口。

    与历史 ``momentum_rotation`` 基线分开注册，避免改变现有回测和实盘
    白名单；默认参数在 config/settings.yaml 的 overrides.momentum_path 中定义。
    """

    name = "momentum_path"

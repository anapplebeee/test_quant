from __future__ import annotations

from pathlib import Path

import pandas as pd

from quart.data.market import MarketData
from quart.config import PROJECT_ROOT
from quart.execution.constraints import FLAT
from quart.strategy.base import BaseStrategy
from quart.strategy.filters import apply_liquidity


class MLRankStrategy(BaseStrategy):
    """Hold top-k symbols by external model score (e.g. Alpha158+LGBM walk-forward preds).

    Scores are refreshed offline by scripts/train_ml.py; this strategy only reads them,
    so research and runtime stay decoupled.
    """

    name = "ml_rank"
    required_history_days = 21

    PARAMS_SCHEMA = {
        "top_k": (int, 10, "持仓数量"),
        "rebalance_days": (int, 5, "调仓周期（交易日）"),
        "max_weight_pct": (float, 0.15, "单票权重上限"),
        "min_score": ((int, float, type(None)), None, "分数下限"),
        "stale_days": (int, 10, "分数保鲜期（日），超期不再驱动选股"),
        "min_avg_amount": ((int, float, type(None)), None, "流动性门槛"),
        "liquidity_days": (int, 20, "流动性回看窗口"),
        "min_price": ((int, float, type(None)), None, "最低价过滤"),
        "use_regime_filter": (bool, True, "是否启用指数择时"),
        "regime_mode": (str, "ma", "择时模式: ma=均线, score=R4多因子打分分级仓位"),
        "timing_levels": (int, 3, "score 模式档位数（2=全仓/空仓, 3=加半仓档）"),
        "regime_filter_days": (int, 20, "择时均线窗口"),
        "regime_band": (float, 0.0, "择时迟滞带宽度"),
        "scores_path": ((str, type(None)), None, "ML 分数文件路径"),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.required_history_days = max(
            int(self.params.get("regime_filter_days", 20))
            if self.params.get("use_regime_filter", True) else 0,
            int(self.params.get("liquidity_days", 20))
            if self.params.get("min_avg_amount") else 0,
        ) + 1

    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        p = self.params
        self.top_k = int(p.get("top_k", 10))
        self.rebalance_days = int(p.get("rebalance_days", 5))
        self.max_weight = float(p.get("max_weight_pct", 0.15))
        self.min_score = p.get("min_score")
        # 预测分数保鲜期：5 日 horizon 模型的分数半衰期 <10 天（架构评审 4.4），
        # 旧默认 35 天会让一个月前的预测继续驱动选股
        self.stale_days = int(p.get("stale_days", 10))
        self.min_avg_amount = p.get("min_avg_amount")
        self.liquidity_days = int(p.get("liquidity_days", 20))
        self.min_price = p.get("min_price")
        self.use_regime = bool(p.get("use_regime_filter", True))
        self.regime_mode = str(p.get("regime_mode", "ma"))
        self.timing_levels = int(p.get("timing_levels", 3))
        self.regime_days = int(p.get("regime_filter_days", 20))
        self.regime_band = float(p.get("regime_band", 0.0))
        self.required_history_days = max(
            self.regime_days if self.use_regime else 0,
            self.liquidity_days if self.min_avg_amount else 0,
        ) + 1
        self.regime_ma = (
            md.benchmark_close.rolling(self.regime_days).mean()
            if md.benchmark_close is not None
            else None
        )
        # 带缓冲带的择时序列（hysteresis）：减少 MA 附近的反复全清全建
        from quart.strategy.filters import regime_flat_series

        self.regime_flat = (
            regime_flat_series(md.benchmark_close, self.regime_ma, self.regime_band)
            if self.regime_ma is not None
            else None
        )
        # regime_mode="score"：R4 多因子打分分级仓位，与 MA 模式互斥
        self.timing_exposure = None
        if self.use_regime and self.regime_mode == "score":
            from quart.strategy.timing import score_timing_exposure

            self.timing_exposure = score_timing_exposure(
                md, levels=self.timing_levels, breadth_ma_window=self.regime_days
            )

        path = Path(p.get("scores_path") or PROJECT_ROOT / "data" / "scores" / "preds.csv")
        if not path.exists():
            raise FileNotFoundError(f"scores file not found: {path}, run scripts/train_ml.py first")
        df = pd.read_csv(path, parse_dates=["datetime"], dtype={"instrument": str})
        wide = df.pivot_table(index="datetime", columns="instrument", values="score", aggfunc="last").sort_index()

        dates = md.dates
        aligned = wide.reindex(wide.index.union(dates)).sort_index().ffill(limit=self.stale_days)
        self.scores = aligned.reindex(dates)
        self._next_rebalance = 0

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days

        row = self.scores.iloc[i].dropna() if i < len(self.scores) else pd.Series(dtype=float)
        if row.empty:
            return {}

        if self.use_regime and self.regime_flat is not None:
            if bool(self.regime_flat.iloc[i]):
                return {FLAT: 1.0}

        exposure = 1.0
        if self.use_regime and self.timing_exposure is not None:
            exposure = float(self.timing_exposure.iloc[i])
            if exposure <= 0:
                return {FLAT: 1.0}

        if self.min_score is not None:
            row = row[row > float(self.min_score)]
            if row.empty:
                return {}

        volume_row = md.volumes.iloc[i]
        tradable = volume_row[volume_row.fillna(0) > 0].index
        row = row.loc[row.index.intersection(tradable)]
        row = apply_liquidity(row, md, i, self.min_avg_amount, self.liquidity_days, self.min_price)
        if row.empty:
            return {}

        top = row.nlargest(self.top_k)
        weight = min(1.0 / len(top), self.max_weight)
        # 分级仓位：0<exposure<1 时权重按比例缩减，余下留现金（R4 仓位管理）
        return {sym: weight * exposure for sym in top.index}

    def state_dict(self):
        return {"next_rebalance": int(self._next_rebalance)}

    def load_state_dict(self, state):
        super().load_state_dict(state)
        if state and "next_rebalance" in state:
            self._next_rebalance = int(state["next_rebalance"])

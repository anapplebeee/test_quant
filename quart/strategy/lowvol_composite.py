from __future__ import annotations

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.execution.constraints import FLAT
from quart.strategy.base import BaseStrategy
from quart.strategy.filters import apply_liquidity, regime_flat_series


class LowVolCompositeStrategy(BaseStrategy):
    """A-share low-anomaly composite: z(-vol20) + z(-amp20) + z(-lottery20).

    Research basis (scripts/factor_research.py, 2019-2026 full market):
    these three sibling factors hold |IC|~0.065 with stable halves both monthly and weekly.
    Optional short-reversal tilt via rev_weight.

    industry_z=True（策略名 lowvol_indz）：对复合分做行业内 z-score（statistical
    cluster 映射，组内样本 <5 只回退全市场分）。依据 R2 因子研究：行业内相对
    反转 rel_ind_mom20 的 ICIR(-0.38) 高于全市场反转，行业中性化使低波打分
    摆脱行业间波动率基数差异（如银行 vs 券商）。
    """

    name = "lowvol_composite"
    industry_z = False  # prepare() 中按 params 覆盖；类级默认供注册检查

    PARAMS_SCHEMA = {
        "top_k": (int, 10, "持仓数量"),
        "rebalance_days": (int, 5, "调仓周期（交易日）"),
        "max_weight_pct": (float, 0.15, "单票权重上限"),
        "min_avg_amount": ((int, float, type(None)), None, "流动性门槛"),
        "liquidity_days": (int, 20, "流动性回看窗口"),
        "min_price": ((int, float, type(None)), None, "最低价过滤"),
        "use_regime_filter": (bool, False, "是否启用指数择时"),
        "regime_filter_days": (int, 20, "择时均线窗口"),
        "regime_band": (float, 0.02, "择时迟滞带宽度"),
        "rev_weight": (float, 0.0, "反转因子权重"),
        "rank_buffer": (float, 0.0, "排名缓冲带（换手控制）"),
        "selection": (str, "composite", "选股模式 composite/bounce"),
        "industry_z": (bool, False, "是否行业内 z-score 中性化"),
    }

    @staticmethod
    def _buffer_select(ranked_syms: list[str], held: set[str], top_k: int, buffer: float) -> list[str]:
        """带排名缓冲带的选股（换手控制）：

        持仓只要仍位于 top_k*(1+buffer) 名内就继续保留，空出的槽位按当前排名补入新名字。
        buffer=0 时等价于纯 top_k（因持有者若在 top_k 内本就入选，补入者按名次取）。
        ranked_syms 必须已按分数降序排列且仅含当日可交易+流动性合格者。
        """
        keep_n = int(round(top_k * (1 + buffer)))
        # 关键：按原序列的排名位置判断（先过滤再切片会打乱位置导致出区持仓被误留）
        keep = [s for pos, s in enumerate(ranked_syms) if s in held and pos < keep_n]
        new = [s for s in ranked_syms if s not in held][: top_k - len(keep)]
        picks = keep + new
        return picks if len(picks) == top_k else ranked_syms[:top_k]

    def _z(self, df: pd.DataFrame) -> pd.DataFrame:
        mu = df.mean(axis=1)
        sd = df.std(axis=1).replace(0, np.nan)
        return df.sub(mu, axis=0).div(sd, axis=0).astype("float32")

    def _group_z(self, df: pd.DataFrame, min_group_size: int = 5) -> pd.DataFrame:
        """逐日行业内 z-score：z = (x - 行业均值) / 行业标准差。

        组内样本 < min_group_size 或标准差为 0 时回退为 NaN（当日剔除）。
        """
        from quart.strategy.industries import load_industry_series

        try:
            ind = load_industry_series("first")
        except FileNotFoundError:
            return df
        g = pd.Series([ind.get(s, "UNKNOWN") for s in df.columns], index=df.columns)
        grp = df.T.groupby(g)
        mu = grp.mean().T  # dates × industries
        cnt = grp.count().T
        # 样本口径（ddof=1）与 _z() 一致；弃用 sqrt(E[x²]-E[x]²) 形式
        # （总体口径 + 灾难性抵消风险，架构评审 4.2）
        sd = grp.std(ddof=1).T
        sd = sd.where(cnt >= min_group_size)
        sd = sd.replace(0, np.nan)

        mu_b = mu.reindex(columns=g.values)
        mu_b.columns = df.columns
        sd_b = sd.reindex(columns=g.values)
        sd_b.columns = df.columns
        return df.div(sd_b, axis=0).sub(mu_b.div(sd_b, axis=0), axis=0).astype("float32")

    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        p = self.params
        self.top_k = int(p.get("top_k", 10))
        self.rebalance_days = int(p.get("rebalance_days", 5))
        self.max_weight = float(p.get("max_weight_pct", 0.15))
        self.min_avg_amount = p.get("min_avg_amount")
        self.liquidity_days = int(p.get("liquidity_days", 20))
        self.min_price = p.get("min_price")
        self.use_regime = bool(p.get("use_regime_filter", False))
        self.regime_days = int(p.get("regime_filter_days", 20))
        self.rev_weight = float(p.get("rev_weight", 0.0))
        self.rank_buffer = float(p.get("rank_buffer", 0.0))
        self._held: set[str] = set()
        self.selection = str(p.get("selection", "composite"))
        self.industry_z = bool(p.get("industry_z", False))

        c = md.close_val.astype("float32")
        ret1 = c.pct_change(fill_method=None)

        vol20 = -ret1.rolling(20).std().astype("float32")
        amp20 = (-((md.highs - md.lows) / md.closes.shift(1).replace(0, np.nan)).rolling(20).mean()).astype("float32")
        lotto = (-ret1.rolling(20).max()).astype("float32")

        z_vol = self._z(vol20)
        z_amp = self._z(amp20)
        z_lot = self._z(lotto)

        # fillna(0) 是死代码：随后的 where(complete) 会把任一因子缺失的行整行置 NaN
        complete = z_vol.notna() & z_amp.notna() & z_lot.notna()
        comp = ((z_vol + z_amp + z_lot) / 3.0).where(complete).astype("float32")
        if self.industry_z:
            comp = self._group_z(comp)
        self.composite = comp
        del vol20, amp20, lotto, z_vol, z_amp, z_lot

        self.reversal = (-ret1.rolling(5).mean()).astype("float32")

        self.regime_ma = (
            md.benchmark_close.rolling(self.regime_days).mean() if md.benchmark_close is not None else None
        )
        # 带缓冲带的择时序列（hysteresis）：减少 MA 附近的反复全清全建
        self.regime_band = float(p.get("regime_band", 0.02))
        self.regime_flat = (
            regime_flat_series(md.benchmark_close, self.regime_ma, self.regime_band)
            if self.regime_ma is not None
            else None
        )
        self._next_rebalance = 0

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days

        if self.use_regime and self.regime_flat is not None:
            if bool(self.regime_flat.iloc[i]):
                self._held = set()  # FLAT 已清仓，同步清空持仓记忆
                return {FLAT: 1.0}

        scores = self.composite.iloc[i]
        if self.selection == "bounce":
            quiet = self.composite.iloc[i] >= self.composite.iloc[i].median(skipna=True)
            scores = scores.where(quiet)
            aligned = pd.DataFrame({"q": scores, "r": self.reversal.iloc[i]}).dropna()
            if aligned.empty:
                return {}
            scores = (aligned["q"] + aligned["r"]).astype("float32")
        elif self.reversal is not None:
            rev_z = self._z(self.reversal.iloc[i].to_frame().T).iloc[0]
            scores = scores.add(rev_z * self.rev_weight, fill_value=np.nan)

        scores = scores.dropna()
        volume_row = md.volumes.iloc[i]
        tradable = volume_row[volume_row.fillna(0) > 0].index
        scores = scores.loc[scores.index.intersection(tradable)]
        scores = apply_liquidity(scores, md, i, self.min_avg_amount, self.liquidity_days, self.min_price)
        if len(scores) < self.top_k:
            return {}

        ranked = scores.sort_values(ascending=False).index.tolist()
        picks = self._buffer_select(ranked, self._held, self.top_k, self.rank_buffer)
        self._held = set(picks)
        weight = min(1.0 / len(picks), self.max_weight)
        return {sym: weight for sym in picks}

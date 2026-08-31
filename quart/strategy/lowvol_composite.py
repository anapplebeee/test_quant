from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger

from quart.data.market import MarketData
from quart.execution.constraints import FLAT
from quart.strategy.base import BaseStrategy
from quart.strategy.filters import apply_liquidity, regime_flat_series


class LowVolCompositeStrategy(BaseStrategy):
    """A 股低风险复合：z(-vol20) + z(-amp20) + z(-lottery20)。

    Research basis (scripts/factor_research.py, 2019-2026 full market):
    these three sibling factors hold |IC|~0.065 with stable halves both monthly and weekly.
    Optional short-reversal tilt via rev_weight.

    industry_z=True（策略名 lowvol_indz）：对复合分做行业内 z-score（statistical
    cluster 映射，组内样本 <5 只回退全市场分）。依据 R2 因子研究：行业内相对
    反转 rel_ind_mom20 的 ICIR(-0.38) 高于全市场反转，行业中性化使低波打分
    摆脱行业间波动率基数差异（如银行 vs 券商）。

    多因子合成（vg_weight > 0）：叠加正交来源——PIT 价值成长因子（质量改善
    roe_improve/profit_yoy + 估值 ep/bp，见 quart/research/value_growth.py）。
    诊断依据：单因子纯多头 alpha 被结构截断（IC 0.065 但超额集中在空头端），
    抬组合 IC 的路径是正交因子合成。无财务数据的符号中性填充 0（financials
    仅覆盖沪深 300，缺失 = 质量中性，不改变其余股票的低波排序）。

    组合构造（weight_mode）：equal=等权（历史行为）；inv_vol=波动率倒数加权
    （单票风险预算均等）；zscore=因子分数加权（保留因子强度横截面信息）。
    非等权模式迭代 cap 至 max_weight_pct 并归一化。

    可选因子（长窗口风险、下行风险、尾损、成交额稳定性、规模、换手、价值）默认关闭，
    只有通过样本外审计后才应进入实盘配置。`industry_z=True` 时在行业内标准化；映射缺失
    或组内样本不足 5 只时回退全市场复合分，而不是静默剔除股票。
    """

    name = "lowvol_composite"
    required_history_days = 21
    industry_z = False  # prepare() 中按 params 覆盖；类级默认供注册检查

    PARAMS_SCHEMA = {
        "top_k": (int, 10, "持仓数量"),
        "rebalance_days": (int, 5, "调仓周期（交易日）"),
        "max_weight_pct": (float, 0.15, "单票权重上限"),
        "min_avg_amount": ((int, float, type(None)), None, "流动性门槛"),
        "liquidity_days": (int, 20, "流动性回看窗口"),
        "min_price": ((int, float, type(None)), None, "最低价过滤"),
        "use_regime_filter": (bool, False, "是否启用指数择时"),
        "regime_mode": (str, "ma", "择时模式: ma=均线, score=R4多因子打分分级仓位"),
        "timing_levels": (int, 3, "score 模式档位数（2=全仓/空仓, 3=加半仓档）"),
        "regime_filter_days": (int, 20, "择时均线窗口"),
        "regime_band": (float, 0.02, "择时迟滞带宽度"),
        "rev_weight": (float, 0.0, "反转因子权重"),
        "rank_buffer": (float, 0.0, "排名缓冲带（换手控制）"),
        "selection": (str, "composite", "选股模式 composite/bounce"),
        "industry_z": (bool, False, "是否行业内 z-score 中性化"),
        "weight_mode": (str, "equal", "权重模式: equal=等权, inv_vol=波动率倒数, zscore=因子分数"),
        "vg_weight": (float, 0.0, "价值成长因子合成权重（0=关闭，0~1）"),
        "winsor_z": (float, 0.0, "截面 z-score 截尾阈值（0=关闭）"),
        "risk_window_long": (int, 60, "长窗口风险因子窗口"),
        "long_vol_weight": (float, 0.0, "长窗口低波因子权重"),
        "downside_weight": (float, 0.0, "下行半方差因子权重"),
        "tail_weight": (float, 0.0, "尾部损失因子权重"),
        "amount_stability_weight": (float, 0.0, "成交额稳定性因子权重"),
        "size_weight": (float, 0.0, "小市值因子权重（z(-ln 流通市值)）"),
        "turnover_weight": (float, 0.0, "低换手率因子权重（z(-20 日换手)）"),
        "value_weight": (float, 0.0, "价值因子权重（z(1/PE_TTM)，仅盈利为正）"),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.weight_mode = str(self.params.get("weight_mode", "equal"))
        if self.weight_mode not in ("equal", "inv_vol", "zscore"):
            self.weight_mode = "equal"
        self.required_history_days = max(
            20,
            int(self.params.get("regime_filter_days", 20))
            if self.params.get("use_regime_filter", False) else 0,
            int(self.params.get("liquidity_days", 20))
            if self.params.get("min_avg_amount") else 0,
        ) + 1

    @staticmethod
    def _buffer_select(ranked_syms: list[str], held: set[str], top_k: int, buffer: float) -> list[str]:
        """带排名缓冲带的选股（换手控制）：

        持仓只要仍位于 top_k*(1+buffer) 名内就继续保留，空出的槽位按当前排名补入新名字。
        buffer=0 时等价于纯 top_k（因持有者若在 top_k 内本就入选，补入者按名次取）。
        ranked_syms 必须已按分数降序排列且仅含当日可交易+流动性合格者。
        """
        keep_n = round(top_k * (1 + buffer))
        # 关键：按原序列的排名位置判断（先过滤再切片会打乱位置导致出区持仓被误留）
        keep = [s for pos, s in enumerate(ranked_syms) if s in held and pos < keep_n]
        new = [s for s in ranked_syms if s not in held][: top_k - len(keep)]
        picks = keep + new
        return picks if len(picks) == top_k else ranked_syms[:top_k]

    def _z(self, df: pd.DataFrame) -> pd.DataFrame:
        mu = df.mean(axis=1)
        sd = df.std(axis=1).replace(0, np.nan)
        values = df.sub(mu, axis=0).div(sd, axis=0)
        if self.winsor_z > 0:
            values = values.clip(lower=-self.winsor_z, upper=self.winsor_z)
        return values.astype("float32")

    def _group_z(self, df: pd.DataFrame, min_group_size: int = 5) -> pd.DataFrame:
        """逐日行业内 z-score；小样本或零方差行业回退全市场分。"""
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
        grouped = df.sub(mu_b, axis=0).div(sd_b, axis=0)
        return grouped.where(sd_b.notna(), df).astype("float32")

    def _blend_fundamental(
        self,
        close: pd.DataFrame,
        weighted: pd.DataFrame,
        complete: pd.DataFrame,
        total_weight: float,
    ) -> tuple[pd.DataFrame, pd.DataFrame, float]:
        """混入基本面风格因子（小市值/低换手/价值）。

        数据为 baostock 回填的 PIT 口径（scripts/backfill_factor_data.py），
        缺失时告警并跳过——不能静默改变合成分的量纲。
        """
        try:
            from quart.data.fundamental import fundamental_wide
        except ImportError:
            logger.warning("fundamental 模块不可用，跳过风格因子混入")
            return weighted, complete, total_weight
        try:
            frames = {
                column: fundamental_wide(column).reindex(index=close.index, columns=close.columns)
                for column in ("float_mcap", "turn", "pe_ttm")
            }
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("基本面因子数据缺失，跳过风格因子混入: {}", exc)
            return weighted, complete, total_weight

        blends: list[tuple[float, pd.DataFrame]] = []
        if self.size_weight > 0:
            blends.append((self.size_weight, -np.log(frames["float_mcap"])))
        if self.turnover_weight > 0:
            blends.append((self.turnover_weight, -frames["turn"].rolling(20).mean()))
        if self.value_weight > 0:
            pe = frames["pe_ttm"]
            blends.append((self.value_weight, (1.0 / pe).where(pe > 0)))
        for weight, raw in blends:
            extra = self._z(raw.astype("float32"))
            weighted = weighted.add(extra * weight, fill_value=np.nan)
            complete &= extra.notna()
            total_weight += weight
            del extra
        return weighted, complete, total_weight

    def sync_positions(self, positions: dict[str, int]) -> None:
        """排名缓冲必须基于实际持仓，而不是上一期目标名单。"""
        self._held = {symbol for symbol, shares in positions.items() if int(shares) > 0}

    VG_FACTORS = ("roe_improve", "profit_yoy", "ep", "bp")

    def _build_vg_score(self, md: MarketData) -> pd.DataFrame | None:
        """PIT 价值成长合成分：各因子截面 z 等权均值（仅限有财务覆盖的符号）。"""
        import warnings

        from quart.config import PROJECT_ROOT

        fin_path = PROJECT_ROOT / "data" / "factors" / "financials.parquet"
        if not fin_path.exists():
            return None
        try:
            from quart.research.value_growth import pit_panels

            fin = pd.read_parquet(fin_path)
            panels = pit_panels(fin, md.close_val, self.VG_FACTORS)
        except Exception:  # 财务数据缺失/损坏时优雅降级为纯低波
            return None
        zs = [self._z(panels[f].reindex(columns=md.close_val.columns))
              for f in self.VG_FACTORS if f in panels]
        zs = [z for z in zs if bool(z.notna().to_numpy().any())]
        if not zs:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # 全 NaN 截面的 nanmean
            arr = np.nanmean(np.stack([z.to_numpy() for z in zs]), axis=0)
        return pd.DataFrame(arr, index=zs[0].index, columns=zs[0].columns).astype("float32")

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
        self.weight_mode = str(p.get("weight_mode", "equal"))
        if self.weight_mode not in ("equal", "inv_vol", "zscore"):
            self.weight_mode = "equal"
        self.winsor_z = max(0.0, float(p.get("winsor_z", 0.0)))
        self.risk_window_long = max(20, int(p.get("risk_window_long", 60)))
        self.long_vol_weight = max(0.0, float(p.get("long_vol_weight", 0.0)))
        self.downside_weight = max(0.0, float(p.get("downside_weight", 0.0)))
        self.tail_weight = max(0.0, float(p.get("tail_weight", 0.0)))
        self.amount_stability_weight = max(0.0, float(p.get("amount_stability_weight", 0.0)))
        self.size_weight = max(0.0, float(p.get("size_weight", 0.0)))
        self.turnover_weight = max(0.0, float(p.get("turnover_weight", 0.0)))
        self.value_weight = max(0.0, float(p.get("value_weight", 0.0)))

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
        weighted = z_vol + z_amp + z_lot
        total_weight = 3.0

        # 可选因子（默认关闭，仅样本外审计通过后启用）：
        # 长窗口低波 / 下行半方差 / 尾损 / 成交额稳定性
        optional_factors = (
            (
                self.long_vol_weight,
                lambda: -ret1.rolling(self.risk_window_long).std(),
            ),
            (
                self.downside_weight,
                lambda: -np.sqrt(
                    ret1.clip(upper=0).pow(2).rolling(self.risk_window_long).mean()
                ),
            ),
            (
                self.tail_weight,
                lambda: ret1.rolling(self.risk_window_long).quantile(0.10),
            ),
            (
                self.amount_stability_weight,
                lambda: -(
                    md.amounts.rolling(self.risk_window_long).std()
                    / md.amounts.rolling(self.risk_window_long).mean().replace(0, np.nan)
                ),
            ),
        )
        for weight, factory in optional_factors:
            if weight <= 0:
                continue
            extra = self._z(factory().astype("float32"))
            weighted = weighted.add(extra * weight, fill_value=np.nan)
            complete &= extra.notna()
            total_weight += weight
            del extra

        # 基本面风格因子（小市值/低换手/价值），PIT 口径
        if self.size_weight > 0 or self.turnover_weight > 0 or self.value_weight > 0:
            weighted, complete, total_weight = self._blend_fundamental(
                c, weighted, complete, total_weight
            )

        comp = (weighted / total_weight).where(complete).astype("float32")

        # 多因子合成：叠加 PIT 价值成长（正交来源），缺失财务数据 = 中性 0
        self.vg_weight = float(p.get("vg_weight", 0.0))
        if self.vg_weight > 0:
            self.vg_score = self._build_vg_score(md)
            if self.vg_score is not None:
                comp = (
                    (1.0 - self.vg_weight) * comp
                    + self.vg_weight * self.vg_score.reindex_like(comp).fillna(0.0)
                ).astype("float32")
        else:
            self.vg_score = None

        if self.industry_z:
            comp = self._group_z(comp)
        self.composite = comp
        del vol20, amp20, lotto, z_vol, z_amp, z_lot, weighted

        self.reversal = (-ret1.rolling(5).mean()).astype("float32")
        # inv_vol 加权所需的逐票波动率
        self.vol20 = ret1.rolling(20).std().astype("float32") if self.weight_mode == "inv_vol" else None

        self.regime_ma = (
            md.benchmark_close.rolling(self.regime_days).mean() if md.benchmark_close is not None else None
        )
        # 带缓冲带的择时序列（hysteresis）：减少 MA 附近的反复全清全建
        self.regime_band = float(p.get("regime_band", 0.02))
        self.required_history_days = max(
            20,
            self.regime_days if self.use_regime else 0,
            self.liquidity_days if self.min_avg_amount else 0,
        ) + 1
        self.regime_flat = (
            regime_flat_series(md.benchmark_close, self.regime_ma, self.regime_band)
            if self.regime_ma is not None
            else None
        )
        # regime_mode="score"：R4 多因子打分分级仓位，与 MA 模式互斥
        self.regime_mode = str(p.get("regime_mode", "ma"))
        self.timing_levels = int(p.get("timing_levels", 3))
        self.timing_exposure = None
        if self.use_regime and self.regime_mode == "score":
            from quart.strategy.timing import score_timing_exposure

            self.timing_exposure = score_timing_exposure(
                md, levels=self.timing_levels, breadth_ma_window=self.regime_days
            )
        self._next_rebalance = 0

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days

        exposure = 1.0
        if self.use_regime and self.regime_flat is not None and bool(self.regime_flat.iloc[i]):
            self._held = set()  # FLAT 已清仓，同步清空持仓记忆
            return {FLAT: 1.0}
        if self.use_regime and self.timing_exposure is not None:
            exposure = float(self.timing_exposure.iloc[i])
            if exposure <= 0:
                self._held = set()
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
        if self.weight_mode == "equal":
            # 等权保持历史行为：超上限不归一，余量留现金
            weight = min(1.0 / len(picks), self.max_weight)
            # 分级仓位：0<exposure<1 时权重按比例缩减，余下留现金（R4 仓位管理）
            return {sym: weight * exposure for sym in picks}
        weights = self._risk_weights(picks, scores, i)
        return {sym: float(w) * exposure for sym, w in weights.items()}

    def _risk_weights(self, picks: list[str], scores: pd.Series, i: int) -> pd.Series:
        """非等权组合构造：inv_vol / zscore，迭代截断至 max_weight 并归一化。"""
        n = len(picks)
        if n == 0:
            return pd.Series(dtype="float64")
        if self.weight_mode == "inv_vol" and self.vol20 is not None:
            v = self.vol20.iloc[i].reindex(picks)
            inv = 1.0 / v.where(v > 0)
            w = inv.fillna(inv.mean()) if inv.notna().any() else pd.Series(1.0 / n, index=picks)
        elif self.weight_mode == "zscore":
            s = scores.reindex(picks).astype("float64")
            # 平移到非负，保留因子强度横截面信息；全同分退化为等权
            w = (s - s.min() + 1e-6).fillna(1e-6)
        else:
            w = pd.Series(1.0 / n, index=picks)
        w = w / w.sum()
        for _ in range(3):  # 迭代截断：超上限部分按比例回填未超上限的票
            over = w > self.max_weight
            if not over.any():
                break
            excess = float((w[over] - self.max_weight).sum())
            w[over] = self.max_weight
            free_idx = w.index[~over]
            if len(free_idx) == 0:
                break
            free = w[free_idx]
            w[free_idx] = free + excess * free / free.sum()
        return w

    def state_dict(self):
        return {
            "next_rebalance": int(self._next_rebalance),
            "held": sorted(self._held),
        }

    def load_state_dict(self, state):
        super().load_state_dict(state)
        if state:
            if "next_rebalance" in state:
                self._next_rebalance = int(state["next_rebalance"])
            if "held" in state:
                self._held = {str(s) for s in state["held"]}

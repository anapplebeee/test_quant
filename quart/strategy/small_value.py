"""小市值多因子月度轮动策略（RESEARCH-015 胜出配置正式化）。

实证来源：scripts/backtest_small_val.py + diag/final 消融（2019-2026 全成本口径）。

选股（H5）
----
1. 基础池过滤（避雷）：流通市值 15~60 亿、股价 3~25 元、非 ST（含近 250 日曾 ST）、
   上市 > 2 年、20 日均成交额 ≥ 5000 万、近 60 日涨停 ≤ 3 次、近 20 日涨幅 ≤ 40%、
   近 60 日流通市值最低点 ≥ 12 亿、剔北交所/停牌；
2. PIT 财务过滤：最新已披露报告期（announcement_date ≤ 调仓日）营收 > 3 亿、净利 > 0；
3. 排序：use_score=True 时市值最小 60 只内按
   市值(升序)40% + 20日反转(升序)25% + 20日换手(升序)20% + ROE(降序)15% 综合分，
   否则纯市值升序；
4. 持有 Top-n=10 等权，每月第 reb_day 个交易日调仓。

被证伪组件（RESEARCH-015 G/H 消融，刻意不实现）
----
双均线/拥挤度/风格轮动择时、极端日清仓、组合回撤分级、波动率目标、
相对止损、错峰调仓——全部把 CAGR 30.9% 砍到 1-16%，仅日历空仓正贡献。

数据依赖
----
- data/factors/fundamental_daily.parquet（float_mcap/turn/is_st，PIT 时点值）
- data/factors/financials.parquet（PIT 营收/净利/ROE，按 announcement_date 对齐）
- data/meta/security_master.parquet（上市日）
数据加载集中在 :meth:`SmallValueStrategy._load_aux`，测试可 monkeypatch 注入。

红线披露：全成本 1x 实证 CAGR 30.9%/Sharpe 1.18 但 MDD -35%，不满足平台 -20%
回撤红线，status=研究，不入 paper/live allowlist。
"""
from __future__ import annotations

import gc
from pathlib import Path

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.execution.constraints import FLAT
from quart.strategy.base import BaseStrategy

#: 北交所/老三板前缀（92 覆盖 920 段）
NORTH_PREFIX = ("4", "8", "92")


def load_aux_data(dates: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """加载基本面/财务/上市日辅助数据（对齐 md.dates）。

    Returns
    -------
    dict:
        fmcap / turn: date×symbol 宽表（ffill 对齐 dates）
        st: date×symbol 0/1 宽表（ffill）
        fin: 按 announcement_date 排序的 PIT 财务长表
        listed_days: symbol → 上市日 Series
    """
    root = Path("data/factors")
    frames = []
    for name in ("fundamental_daily.parquet", "fundamental_daily.part-1.parquet",
                 "fundamental_daily.part-2.parquet", "fundamental_daily.part-3.parquet"):
        p = root / name
        if p.exists():
            frames.append(pd.read_parquet(
                p, columns=["date", "symbol", "float_mcap", "turn", "is_st"]))
    if not frames:
        raise FileNotFoundError(
            f"缺少 {root}/fundamental_daily*.parquet，请先运行 scripts/backfill_factor_data.py"
        )
    df = pd.concat(frames, ignore_index=True)
    del frames
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(["date", "symbol"], keep="last")

    out: dict[str, pd.DataFrame] = {}

    def _pivot(d: pd.DataFrame, col: str) -> pd.DataFrame:
        w = d.pivot(index="date", columns="symbol", values=col).sort_index()
        return w.reindex(dates).ffill()

    out["fmcap"] = _pivot(df, "float_mcap").astype("float32")
    out["turn"] = _pivot(df, "turn").astype("float32")
    out["st"] = _pivot(df, "is_st").fillna(False).astype("float32")

    fin = pd.read_parquet(
        root / "financials.parquet",
        columns=["symbol", "announcement_date", "revenue", "net_profit", "roe"],
    )
    fin = fin.dropna(subset=["announcement_date"])
    fin["announcement_date"] = pd.to_datetime(fin["announcement_date"])
    out["fin"] = fin.sort_values(["announcement_date", "symbol"]).reset_index(drop=True)
    del df
    gc.collect()

    sm = pd.read_parquet("data/meta/security_master.parquet",
                         columns=["symbol", "listed_at", "board"])
    sm = sm[~sm["board"].astype(str).str.contains("北交所|BJ", case=False, na=False)]
    sm = sm.drop_duplicates("symbol", keep="first")
    listed = pd.to_datetime(sm["listed_at"])
    out["listed_days"] = pd.Series(listed.values, index=sm["symbol"].astype(str).values)
    return out


class SmallValueStrategy(BaseStrategy):
    """小市值月度轮动：避雷池 → PIT 财务过滤 → 二级因子打分 → Top10 等权。"""

    name = "small_value"
    required_history_days = 70

    PARAMS_SCHEMA = {
        "n": (int, 10, "持仓股票数"),
        "score_top": (int, 60, "二级因子打分候选（市值最小 N 只）"),
        "use_score": (bool, True, "启用二级因子打分（关=纯市值升序）"),
        "mcap_min": (float, 15e8, "流通市值下限（元）"),
        "mcap_max": (float, 60e8, "流通市值上限（元）"),
        "price_min": (float, 3.0, "股价下限（元，面值退市缓冲）"),
        "price_max": (float, 25.0, "股价上限（元）"),
        "rev_min": (float, 3e8, "营业收入下限（PIT，元）"),
        "min_list_days": (int, 730, "最少上市自然日"),
        "min_avg_amount": (float, 5e7, "20日均成交额下限（元）"),
        "recency_st_days": (int, 250, "近N日曾ST剔除窗口（0=关闭）"),
        "max_limit_up60": (int, 3, "近60日涨停次数上限"),
        "max_ret20": (float, 0.40, "近20日涨幅上限（游资炒作剔除）"),
        "mcap_floor60": (float, 12e8, "近60日流通市值最低点下限（元，0=关闭）"),
        "use_calendar_flat": (bool, True, "1月与4月20日后空仓（唯一实证正贡献风控）"),
        "reb_day": (int, 1, "每月第N个交易日调仓"),
        "buf_mult": (float, 0.0, "卖出缓冲带倍数（0=严格按排名）"),
        "max_weight_pct": (float, 0.15, "单票权重上限"),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.required_history_days = 70

    # ---------------- prepare ----------------

    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        p = self.params
        dates = md.dates
        px = md.close_val
        self._warm = self.required_history_days

        aux = self._load_aux(dates)
        self.fmcap: pd.DataFrame = aux["fmcap"]
        self.turn = aux["turn"]
        self.st = aux["st"]
        self.fin: pd.DataFrame = aux["fin"]
        self.listed_days: pd.Series = aux["listed_days"]

        # 向量化筛选因子（全部只用 i 及之前数据）
        self.ret1 = px / px.shift(1) - 1
        self.ret20 = px / px.shift(20) - 1
        self.limitup60 = (self.ret1 >= 0.095).rolling(60, min_periods=30).sum()
        self.min_mcap60 = self.fmcap.rolling(60, min_periods=30).min()
        self.avg_amt20 = md.amounts.rolling(20, min_periods=10).mean()
        self.turn20 = self.turn.rolling(20, min_periods=10).mean()
        rs = int(p.get("recency_st_days", 250) or 0)
        self.st_recent = self.st.rolling(rs, min_periods=1).max() if rs else None

        # 调仓日标记：每月第 reb_day 个交易日
        dt = dates.to_series()
        ym = dt.dt.to_period("M")
        self.day_no = ym.groupby(ym).cumcount() + 1

        self._held: list[str] = []
        self._last_picks: list[str] = []
        self._snap: pd.DataFrame = pd.DataFrame()

    def _load_aux(self, dates: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
        """辅助数据加载（测试 monkeypatch 注入点）。"""
        return load_aux_data(dates)

    # ---------------- 选股 ----------------

    def _fin_snapshot(self, d: pd.Timestamp) -> pd.DataFrame:
        """调仓日最新已披露报告期快照（announcement_date ≤ d，每股取最新）。"""
        fin = self.fin
        cut = int(fin["announcement_date"].searchsorted(d, side="right"))
        if cut == 0:
            return pd.DataFrame()
        return (
            fin.iloc[:cut]
            .sort_values("symbol")
            .drop_duplicates("symbol", keep="last")
            .set_index("symbol")
        )

    def _select(self, i: int) -> list[str]:
        """i 日收盘过滤 + 排序，返回 ranked（越前越好）。"""
        p = self.params
        fm_row = self.fmcap.iloc[i]
        px_row = self._md.close_val.iloc[i]
        st_row = self.st.iloc[i]

        valid = fm_row.between(p.get("mcap_min", 15e8), p.get("mcap_max", 60e8))
        valid &= px_row.between(p.get("price_min", 3.0), p.get("price_max", 25.0))
        valid &= st_row.fillna(0) == 0
        valid &= ~(self.ret20.iloc[i] > p.get("max_ret20", 0.40)).fillna(False)
        valid &= ~(self.limitup60.iloc[i] >= p.get("max_limit_up60", 3)).fillna(False)
        floor = float(p.get("mcap_floor60", 12e8) or 0)
        if floor > 0:
            valid &= ~(self.min_mcap60.iloc[i] < floor).fillna(False)
        min_amt = float(p.get("min_avg_amount", 5e7) or 0)
        if min_amt > 0:
            valid &= self.avg_amt20.iloc[i].fillna(0) >= min_amt
        if self.st_recent is not None:
            valid &= self.st_recent.iloc[i].fillna(0) == 0
        age = (self._md.dates[i] - self.listed_days.reindex(valid.index)).dt.days
        valid &= (age > int(p.get("min_list_days", 730))).fillna(False)
        valid &= ~pd.Series(valid.index, index=valid.index).str.startswith(NORTH_PREFIX)

        cand = valid[valid.fillna(False)].index
        if len(cand) == 0:
            return []
        # PIT 财务过滤
        self._snap = self._fin_snapshot(self._md.dates[i])
        if not self._snap.empty:
            rev = self._snap["revenue"].reindex(cand)
            npf = self._snap["net_profit"].reindex(cand)
            cand = cand[(rev > p.get("rev_min", 3e8)).fillna(False)
                        & (npf > 0).fillna(False)]
            if len(cand) == 0:
                return []

        ranked = fm_row.reindex(cand).sort_values().index.tolist()
        if not bool(p.get("use_score", True)):
            return ranked
        # 二级因子：市值最小 score_top 只内综合打分
        cand2 = ranked[: int(p.get("score_top", 60))]
        cap_r = fm_row.reindex(cand2).rank(ascending=True, pct=True)
        ret_r = self.ret20.iloc[i].reindex(cand2).rank(ascending=True, pct=True)
        to_r = self.turn20.iloc[i].reindex(cand2).rank(ascending=True, pct=True)
        score = (0.40 * cap_r.fillna(cap_r.max()) + 0.25 * ret_r.fillna(ret_r.max())
                 + 0.20 * to_r.fillna(to_r.max()))
        if not self._snap.empty:
            roe = self._snap["roe"].reindex(cand2)
            score = score + 0.15 * (1.0 - roe.rank(ascending=True, pct=True)).fillna(0.85)
        return score.sort_values().index.tolist()

    def _calendar_flat(self, d: pd.Timestamp) -> bool:
        """日历空仓期：1 月整月、4 月 20 日~月末（唯一实证正贡献的风控项）。"""
        return bool(self.params.get("use_calendar_flat", True)) and (
            d.month == 1 or (d.month == 4 and d.day >= 20)
        )

    # ---------------- 每日目标 ----------------

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if i < self._warm:
            return {}
        d = md.dates[i]
        p = self.params
        n = int(p.get("n", 10))
        is_reb = int(self.day_no.iloc[i]) == int(p.get("reb_day", 1))

        if self._calendar_flat(d):
            if self._held:
                self._held, self._last_picks = [], []
                return {FLAT: 1.0}
            return {}

        if not is_reb:
            return {}  # H5 无非调仓日动作（止损/缩放均被证伪）

        ranked = self._select(i)
        buf = max(n, int(round(n * float(p.get("buf_mult", 0.0)))))
        held = [s for s in self._held if s in ranked[:buf]]
        need = n - len(held)
        buys = [s for s in ranked if s not in held][: max(need, 0)]
        picks = held + buys
        self._last_picks = list(picks)
        self._held = list(picks)
        if not picks:
            return {FLAT: 1.0} if self._held else {}
        w = min(1.0 / len(picks), float(p.get("max_weight_pct", 0.15)))
        return {s: round(w, 6) for s in picks}

    # ---------------- 可恢复状态 ----------------

    def state_dict(self) -> dict:
        return {"held": list(self._held), "last_picks": list(self._last_picks)}

    def load_state_dict(self, state) -> None:
        super().load_state_dict(state)
        if not state:
            return
        self._held = [str(s) for s in state.get("held", [])]
        self._last_picks = [str(s) for s in state.get("last_picks", [])]


__all__ = ["SmallValueStrategy", "load_aux_data"]

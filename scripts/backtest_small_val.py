"""RESEARCH-015：聚宽小市值策略平台复现 + small_val.md 优化叠加 A/B。

原版（quart/strategy/small_val.py，聚宽语义）：
月度调仓、市值 15-60 亿、营收>3亿/净利>0 过滤、中证2000 MA20 择时（跌破或下行→清仓）、
1月与 4月20日~30日 空仓、Top5 等权、价格<25 元、剔 ST/次新(>1年)/停牌/北交所。

平台口径差异（诚实披露）：
- 市值用 PIT 流通市值 float_mcap 近似聚宽总市值（平台无总市值序列）；
- 择时指数用中证1000 近似中证2000（平台无 932000）；
- 营收过滤用最新可得报告期累计值（PIT announcement_date 对齐），原版为单季值；
- 现金流>0 过滤无数据，不做；
- 平台统一收盘信号收盘撮合，原版 9:35/14:50 时点不可复现。

优化叠加（small_val.md 可实现子集，逐层消融）：
V1 避雷：价格>3、上市>2年、日均成交额>5000万、近250日曾ST剔除、
   近60日涨停≥3次或20日涨幅>40%剔除、近60日流通市值最低<12亿剔除；
V2 缓冲区：买前5、跌出前15才卖；
V3 择时升级：双均线(<MA20半仓/<MA60空仓) + 拥挤度(小盘成交占比750日分位>90%半仓/>95%空仓)
   + 风格轮动(中证1000/沪深300比值20日动量<0且<MA60→半仓) + 极端日(跌幅≤-9%家数>300清仓等MA20)
   + 日历精细化(4/15起空仓、12/15后半仓、6月最后一周半仓)；
V4 二级因子：市值最小60只内 市值40%+反转25%+换手20%+ROE15%(PIT)；
V5 执行：调仓错峰(每月第3个交易日) + 组合回撤分级(>15%半仓/>25%空仓等MA20)
   + 个股相对止损(跑输中证1000 12%) + 波动率目标(min(1, 25%/σ20))。
不可实现（平台无数据）：解禁/减持/业绩预告/问询函/质押/商誉/审计意见/扣非净利润/现金流过滤。

门禁口径：2019-2026、3万、主板+创业板+科创板（剔北交所/ST/退市）、全成本 1x
（含 slippage 0.1% + impact_coef 0.10）、质量黑名单 158 只、双基准（沪深300/同池等权）。

用法：python scripts/backtest_small_val.py
"""
from __future__ import annotations

import gc
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from quart.backtest.engine import BacktestEngine
from quart.config import load_config
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.execution.constraints import FLAT
from quart.execution.fees import Fees
from quart.strategy.base import BaseStrategy

try:  # 内存上限（机器内存紧张时防 OOM）
    import duckdb

    duckdb.default_connection.execute(
        "SET memory_limit='2GB'; SET threads=4; SET temp_directory='reports/.duckdb_tmp';"
    )
except Exception:
    pass

CAP = 30_000
START = pd.Timestamp("2019-01-01")
END = pd.Timestamp("2026-08-31")
LOAD_START = "2018-06-01"  # 预热（60 日滚动统计）
NORTH_PREFIX = ("4", "8", "92")  # 北交所/老三板前缀（92 覆盖 920）


# ----------------------------------------------------------------------------
# 数据加载
# ----------------------------------------------------------------------------
def load_md() -> MarketData:
    store = BarStore()
    frames = []
    keep = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]
    for year in range(2018, 2027):
        part = store.load(start=f"{year}-01-01", end=f"{year}-12-31", include_index=False)
        if part.empty:
            continue
        if year == 2018:  # 预热段
            part = part[part["date"] >= LOAD_START]
            if part.empty:
                continue
        keep_year = keep + (["name"] if "name" in part.columns else [])
        part = part[[c for c in keep_year if c in part.columns]].copy()
        num_cols = [c for c in keep if c in part.columns and c not in ("date", "symbol")]
        part[num_cols] = part[num_cols].astype("float32")
        part["symbol"] = part["symbol"].astype(str)
        part = filter_for_simulation(
            part, exclude_star=False, exclude_chinext=False, exclude_st=True
        )  # 原版语义：保留创业/科创，ST/退市过滤
        frames.append(part[keep])
        del part
    gc.collect()
    bars = pd.concat(frames, ignore_index=True)
    del frames
    gc.collect()
    from quart.data.quality import load_blocklist

    blocked = load_blocklist()
    if blocked:
        bars = bars[~bars["symbol"].astype(str).str.zfill(6).isin(
            {str(s).zfill(6) for s in blocked}
        )]
    cfg = load_config()
    md = MarketData.from_bars(bars, store.load_benchmark(cfg["benchmark"]))
    del bars
    gc.collect()
    return md


def load_fundamental_panel(dates: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """主文件 + 3 个 part 合并 → date×symbol 宽表（float_mcap/turn/is_st）。"""
    paths = ["fundamental_daily.parquet"] + [
        f"fundamental_daily.part-{k}.parquet" for k in (1, 2, 3)
    ]
    frames = []
    for name in paths:
        p = Path("data/factors") / name
        if p.exists():
            frames.append(pd.read_parquet(p, columns=["date", "symbol", "float_mcap", "turn", "is_st"]))
    df = pd.concat(frames, ignore_index=True)
    del frames
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(["date", "symbol"], keep="last")
    out = {}
    col = "float_mcap"
    w = df.pivot(index="date", columns="symbol", values=col).sort_index()
    out["fmcap"] = w.reindex(dates).ffill().astype("float32")
    w = df.pivot(index="date", columns="symbol", values="turn").sort_index()
    out["turn"] = w.reindex(dates).ffill().astype("float32")
    w = df.pivot(index="date", columns="symbol", values="is_st").sort_index()
    out["st"] = w.reindex(dates).ffill().fillna(False).astype("float32")
    del df
    gc.collect()
    return out


def load_listed_days(dates: pd.DatetimeIndex) -> pd.Series:
    sm = pd.read_parquet("data/meta/security_master.parquet",
                         columns=["symbol", "listed_at", "board"])
    sm = sm[~sm["board"].astype(str).str.contains("北交所|BJ", case=False, na=False)]
    sm = sm.drop_duplicates("symbol", keep="first")  # 同 symbol 多段状态历史取首行
    listed = pd.to_datetime(sm["listed_at"])
    return pd.Series(listed.values, index=sm["symbol"].astype(str).values)


class FinPIT:
    """调仓日 → 最新已披露报告期（announcement_date ≤ d）财务快照。"""

    def __init__(self):
        fin = pd.read_parquet(
            "data/factors/financials.parquet",
            columns=["symbol", "announcement_date", "revenue", "net_profit", "roe"],
        )
        fin = fin.dropna(subset=["announcement_date"])
        fin["announcement_date"] = pd.to_datetime(fin["announcement_date"])
        self.fin = fin.sort_values(["announcement_date", "symbol"]).reset_index(drop=True)
        self._cache: dict[pd.Timestamp, pd.DataFrame] = {}

    def snapshot(self, d: pd.Timestamp) -> pd.DataFrame:
        if d not in self._cache:
            cut = int(self.fin["announcement_date"].searchsorted(d, side="right"))
            snap = (
                self.fin.iloc[:cut]
                .sort_values("symbol")
                .drop_duplicates("symbol", keep="last")
                .set_index("symbol")
            )
            self._cache = {d: snap}  # 只保留最近一个调仓日快照（省内存）
        return self._cache[d]


# ----------------------------------------------------------------------------
# 策略
# ----------------------------------------------------------------------------
class SmallCapStrategy(BaseStrategy):
    """小市值月度轮动（原版 + small_val.md 优化开关）。"""

    def __init__(self, timing_idx_close: pd.Series, fin: FinPIT,
                 listed_days: pd.Series, fund: dict[str, pd.DataFrame], **params):
        self.p = dict(
            mcap_min=15e8, mcap_max=60e8, n=5, buf_mult=0.0,
            price_min=0.0, price_max=25.0, rev_min=3e8,
            min_list_days=365, min_avg_amount=0.0, recency_st_days=0,
            max_limit_up60=99, max_ret20=1.0, mcap_floor60=0.0,
            reb_day=1, use_score=False, score_top=60,
            timing="simple", calendar="simple", use_extreme=False, sma_main=20,
            port_dd_half=0.0, port_dd_flat=0.0, rel_stop=0.0, vol_target=False,
        )
        self.p.update(params)
        self.idx_close = timing_idx_close
        self.fin = fin
        self.listed_days = listed_days
        self.fund = fund

    # ---------- prepare ----------
    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        p = self.p
        dates = md.dates
        px = md.close_val
        self.dates = dates

        fm = self.fund["fmcap"]
        self.fmcap = fm
        self.st = self.fund["st"]
        self.turn20 = self.fund["turn"].rolling(20, min_periods=10).mean()

        self.ret1 = px / px.shift(1) - 1
        self.ret20 = px / px.shift(20) - 1
        self.limitup60 = (self.ret1 >= 0.095).rolling(60, min_periods=30).sum()
        self.min_mcap60 = fm.rolling(60, min_periods=30).min()
        self.avg_amt20 = md.amounts.rolling(20, min_periods=10).mean()
        st_recent_days = int(p["recency_st_days"])
        self.st_recent = (
            self.st.rolling(st_recent_days, min_periods=1).max() if st_recent_days else None
        )

        # 调仓日索引：每月第 reb_day 个交易日
        ym = pd.Series(dates, index=dates).dt.to_period("M")
        self.ym = ym
        day_no = ym.groupby(ym).cumcount() + 1
        month_len = ym.groupby(ym).transform("size")
        self.day_no = day_no
        self.month_len = month_len

        # 择时指数（中证1000）
        ic = self.idx_close.reindex(dates).ffill()
        s = int(p["sma_main"])
        self.ic = ic
        self.ic_ma_s = ic.rolling(s).mean()
        self.ic_ma20 = ic.rolling(20).mean()
        self.ic_ma60 = ic.rolling(60).mean()
        self.sigma20 = ic.pct_change().rolling(20).std() * np.sqrt(252)

        # 拥挤度：流通市值最低 1/3 股票成交额占全A 比重的 750 日分位
        fm_rank = fm.rank(axis=1, pct=True)
        small_amt = md.amounts.where(fm_rank <= 1 / 3).sum(axis=1)
        total_amt = md.amounts.sum(axis=1)
        crowd = (small_amt / total_amt.replace(0, np.nan)).fillna(0)
        self.crowd_pct = crowd.rolling(750, min_periods=250).rank(pct=True)

        # 风格轮动：中证1000 / 沪深300
        bench = pd.Series(md.benchmark_close, index=dates) if md.benchmark_close is not None else None
        if bench is not None:
            ratio = ic / bench.reindex(dates).ffill()
            self.style_on = (ratio.pct_change(20) < 0) & (ratio < ratio.rolling(60).mean())
        else:
            self.style_on = pd.Series(False, index=dates)

        # 极端日：跌幅 ≤ -9% 家数
        self.limit_dn_cnt = (self.ret1 <= -0.09).sum(axis=1)

        # 财务快照（调仓日按需取）
        self._snap: pd.DataFrame = self.fin.snapshot(dates[0])

        # 运行态
        self._held: dict[str, int] = {}
        self._w_now: dict[str, float] = {}
        self._entry: dict[str, float] = {}
        self._last_picks: list[str] = []
        self._flat_wait = False
        self._ref_nav, self._ref_peak = 1.0, 1.0

    # ---------- 组件 ----------
    def _calendar_mult_at(self, i: int, d: pd.Timestamp) -> float:
        p = self.p
        if p["calendar"] == "none":
            return 1.0
        if p["calendar"] == "simple":
            if d.month == 1 or (d.month == 4 and d.day >= 20):
                return 0.0
            return 1.0
        if d.month == 1 or (d.month == 4 and 15 <= d.day <= 30):
            return 0.0
        if d.month == 12 and d.day >= 15:
            return 0.5
        if d.month == 6 and int(self.day_no.iloc[i]) > int(self.month_len.iloc[i]) - 5:
            return 0.5  # 6 月最后一周
        return 1.0

    def _timing_mult(self, i: int) -> float:
        p = self.p
        if p["timing"] == "none":
            return 1.0
        ic, ma_s = self.ic.iloc[i], self.ic_ma_s.iloc[i]
        ma_s_prev = self.ic_ma_s.iloc[i - 1] if i > 0 else ma_s
        if p["timing"] == "simple":
            if pd.isna(ma_s) or pd.isna(ma_s_prev):
                return 0.0
            return 1.0 if (ic > ma_s and ma_s > ma_s_prev) else 0.0
        # upgraded：双均线
        ma60 = self.ic_ma60.iloc[i]
        if pd.isna(ma60):
            return 0.0
        mult = 1.0
        if ic < ma60:
            return 0.0
        if ic < ma_s:
            mult *= 0.5
        if not pd.isna(self.crowd_pct.iloc[i]):
            if self.crowd_pct.iloc[i] > 0.95:
                return 0.0
            if self.crowd_pct.iloc[i] > 0.90:
                mult *= 0.5
        if bool(self.style_on.iloc[i]):
            mult *= 0.5
        return mult

    def _fundamental_filter(self, snap: pd.DataFrame, syms: pd.Index) -> pd.Series:
        ok = pd.Series(True, index=syms)
        if snap.empty:
            return ok  # 无财报快照时不过滤（预热段）
        rev = snap["revenue"].reindex(syms)
        npf = snap["net_profit"].reindex(syms)
        return ok & (rev > self.p["rev_min"]) & (npf > 0)

    def _select(self, i: int) -> list[str]:
        """i 日收盘候选过滤 + 排序，返回 ranked（越前越好）。"""
        p = self.p
        fm_row = self.fmcap.iloc[i]
        px_row = self._md.close_val.iloc[i]
        st_row = self.st.iloc[i].fillna(0)

        valid = fm_row.between(p["mcap_min"], p["mcap_max"])
        valid &= px_row.between(p["price_min"], p["price_max"])
        valid &= st_row == 0
        valid &= ~(self.ret20.iloc[i] > p["max_ret20"]).fillna(False)
        valid &= ~(self.limitup60.iloc[i] >= p["max_limit_up60"]).fillna(False)
        if p["mcap_floor60"] > 0:
            valid &= ~(self.min_mcap60.iloc[i] < p["mcap_floor60"]).fillna(False)
        if p["min_avg_amount"] > 0:
            valid &= self.avg_amt20.iloc[i].fillna(0) >= p["min_avg_amount"]
        if p["recency_st_days"] > 0 and self.st_recent is not None:
            valid &= self.st_recent.iloc[i].fillna(0) == 0
        # 上市天数
        ld = self.listed_days.reindex(valid.index)
        age = (self.dates[i] - ld).dt.days
        valid &= (age > p["min_list_days"]).fillna(False)
        # 剔北交所前缀
        valid &= ~pd.Series(valid.index, index=valid.index).str.startswith(NORTH_PREFIX)

        cand = valid[valid.fillna(False)].index
        if len(cand) == 0:
            return []
        # 财务过滤（PIT）
        self._snap = self.fin.snapshot(self.dates[i])
        fok = self._fundamental_filter(self._snap, cand)
        cand = cand[fok.fillna(False)]
        if len(cand) == 0:
            return []

        ranked = fm_row.reindex(cand).sort_values().index.tolist()
        if not p["use_score"]:
            return ranked
        # 二级因子：市值最小 score_top 只内综合打分
        cand2 = ranked[: int(p["score_top"])]
        cap_r = fm_row.reindex(cand2).rank(ascending=True, pct=True)
        ret_r = self.ret20.iloc[i].reindex(cand2).rank(ascending=True, pct=True)
        to_r = self.turn20.iloc[i].reindex(cand2).rank(ascending=True, pct=True)
        roe = self._snap["roe"].reindex(cand2) if not self._snap.empty else None
        score = 0.40 * cap_r.fillna(cap_r.max()) + 0.25 * ret_r.fillna(ret_r.max()) \
            + 0.20 * to_r.fillna(to_r.max())
        if roe is not None:
            score = score + 0.15 * (1.0 - roe.rank(ascending=True, pct=True)).fillna(0.85)
        return score.sort_values().index.tolist()

    def _buffer_picks(self, ranked: list[str]) -> list[str]:
        p = self.p
        n = int(p["n"])
        buf = max(n, int(round(n * p["buf_mult"])))
        held = [s for s in self._held if s in ranked[:buf]]
        need = n - len(held)
        buys = [s for s in ranked if s not in held][:max(need, 0)]
        return held + buys

    # ---------- 每日 ----------
    def _update_ref_nav(self, i: int) -> None:
        if not self._w_now or i == 0:
            return
        r1 = self.ret1.iloc[i]
        port_ret = sum(w * (r1.get(s, 0.0) if pd.notna(r1.get(s, np.nan)) else 0.0)
                       for s, w in self._w_now.items())
        self._ref_nav *= (1 + port_ret)
        self._ref_peak = max(self._ref_peak, self._ref_nav)

    def _weights_for(self, picks: list[str], expo: float) -> dict[str, float]:
        if not picks or expo <= 0:
            return {}
        w = expo / len(picks)
        return {s: round(w, 6) for s in picks}

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        d = md.dates[i]
        if d < START or i < 70:
            return {}
        p = self.p
        self._update_ref_nav(i)

        expo = self._calendar_mult_at(i, d) * self._timing_mult(i)

        # 极端日 → 等待站回 MA20
        if p["use_extreme"] and self.limit_dn_cnt.iloc[i] > 300:
            self._flat_wait = True
        if self._flat_wait:
            if self.ic.iloc[i] > self.ic_ma20.iloc[i]:
                self._flat_wait = False
            else:
                expo = 0.0
        # 波动率目标
        if p["vol_target"] and expo > 0:
            sig = self.sigma20.iloc[i]
            if pd.notna(sig) and sig > 0:
                expo = min(expo, min(1.0, 0.25 / sig))
        # 组合回撤分级
        if self._ref_peak > 0:
            dd = self._ref_nav / self._ref_peak - 1
        else:
            dd = 0.0
        if p["port_dd_flat"] and dd <= -p["port_dd_flat"]:
            expo = 0.0
            self._flat_wait = True
        elif p["port_dd_half"] and dd <= -p["port_dd_half"]:
            expo = min(expo, 0.5)

        ym_i = self.ym.iloc[i]
        is_reb = int(self.day_no.iloc[i]) == int(p["reb_day"])

        if is_reb:
            ranked = self._select(i)
            picks = self._buffer_picks(ranked)
            self._last_picks = list(picks)
            if expo <= 0 or not picks:
                self._held, self._w_now, self._entry = {}, {}, {}
                return {FLAT: 1.0} if self._w_now or expo <= 0 else {}
            self._held = {s: i for s in picks}
            self._entry = {s: float(md.close_val.iloc[i].get(s, np.nan)) for s in picks}
            self._w_now = self._weights_for(picks, expo)
            return dict(self._w_now)

        # ---- 非调仓日 ----
        held = list(self._held)
        if not held:
            # 空仓且等待解除 → 用最近一次选股建仓（md：站回 MA20 重新进场）
            if expo > 0 and self._last_picks and not self._flat_wait:
                picks = list(self._last_picks)
                self._held = {s: i for s in picks}
                self._entry = {s: float(md.close_val.iloc[i].get(s, np.nan)) for s in picks}
                self._w_now = self._weights_for(picks, expo)
                return dict(self._w_now)
            return {}

        # 相对止损：跑输中证1000 12%
        if p["rel_stop"] and self._entry:
            base_i = self.ic
            drop = []
            for s in held:
                ei = self._held.get(s)
                ep = self._entry.get(s, np.nan)
                if ei is None or pd.isna(ep) or ei >= i:
                    continue
                px_now = md.close_val.iloc[i].get(s, np.nan)
                if pd.isna(px_now):
                    continue
                rel = (px_now / ep) / (base_i.iloc[i] / base_i.iloc[ei]) - 1
                if pd.notna(rel) and rel < -p["rel_stop"]:
                    drop.append(s)
            if drop:
                for s in drop:
                    self._held.pop(s, None)
                    self._entry.pop(s, None)
                    self._w_now.pop(s, None)
                if not self._w_now:
                    return {FLAT: 1.0}

        if expo <= 0:
            self._held, self._w_now, self._entry = {}, {}, {}
            return {FLAT: 1.0}
        if expo < 0.999:
            self._w_now = {s: w * expo for s, w in self._w_now.items()}
            return dict(self._w_now)
        return {}


# ----------------------------------------------------------------------------
# 回测与报告
# ----------------------------------------------------------------------------
def run_once(md, cfg_bt, strategy, label, cost_mult=1.0):
    fees = Fees(
        cfg_bt["commission_rate"] * cost_mult, cfg_bt["commission_min"] * cost_mult,
        cfg_bt["stamp_tax_rate"] * cost_mult, cfg_bt["transfer_fee_rate"] * cost_mult,
        cfg_bt["slippage_rate"] * cost_mult,
        float(cfg_bt.get("impact_coef", 0.0)) * cost_mult,
    )
    engine = BacktestEngine(md, strategy, fees=fees, initial_cash=CAP,
                            max_adv_participation=0.05)
    equity = engine.run()
    equity.index = md.dates[: len(equity)]
    eq = equity[(equity.index >= START) & (equity.index <= END)]
    yrs = len(eq) / 244.0
    cagr = (eq.iloc[-1] / CAP) ** (1 / yrs) - 1
    dd = eq / eq.cummax() - 1
    mdd = float(dd.min())
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(244)) if rets.std() > 0 else 0.0
    calmar = cagr / abs(mdd) if mdd else 0.0
    amount = sum(float(getattr(t, "amount", 0.0) or 0.0) for t in engine.trades)
    turnover = amount / 2 / eq.mean() / yrs if yrs else 0.0
    return {
        "label": label, "cagr": cagr, "mdd": mdd, "sharpe": sharpe, "calmar": calmar,
        "final": float(eq.iloc[-1]), "trades": len(engine.trades),
        "annual_turnover": turnover, "equity": eq,
    }


def yearly_returns(eq: pd.Series) -> dict[str, float]:
    y = eq.groupby(eq.index.year).agg(["first", "last"])
    out = {}
    prev_last = None
    for yr, row in y.iterrows():
        base = prev_last if prev_last else row["first"]
        out[str(yr)] = float(row["last"] / base - 1)
        prev_last = row["last"]
    return out


def seg_return(eq: pd.Series, s: str, e: str) -> float:
    seg = eq[(eq.index >= s) & (eq.index <= e)]
    if len(seg) < 2:
        return float("nan")
    return float(seg.iloc[-1] / seg.iloc[0] - 1)


ORIG = dict()
V1 = dict(price_min=3.0, min_list_days=730, min_avg_amount=5e7, recency_st_days=250,
          max_limit_up60=3, max_ret20=0.40, mcap_floor60=12e8)
V2 = {**V1, "buf_mult": 3.0}
V3 = {**V2, "timing": "upgraded", "calendar": "upgraded", "use_extreme": True}
V4 = {**V3, "use_score": True}
FULL = {**V4, "reb_day": 3, "port_dd_half": 0.15, "port_dd_flat": 0.25,
        "rel_stop": 0.12, "vol_target": True}

PERTS = [
    ("扰动-市值10~50亿", dict(mcap_min=10e8, mcap_max=50e8)),
    ("扰动-市值20~80亿", dict(mcap_min=20e8, mcap_max=80e8)),
    ("扰动-N=3", dict(n=3)),
    ("扰动-N=8", dict(n=8)),
    ("扰动-MA15", dict(sma_main=15)),
    ("扰动-MA30", dict(sma_main=30)),
]


def main():
    t0 = time.time()
    md = load_md()
    print(f"[data] bars loaded {md.dates[0].date()} ~ {md.dates[-1].date()} "
          f"({len(md.dates)} days, {md.close_val.shape[1]} symbols) {time.time()-t0:.0f}s")
    fund = load_fundamental_panel(md.dates)
    fin = FinPIT()
    listed = load_listed_days(md.dates)
    idx = BarStore().load_benchmark("000852")  # 中证1000（近似中证2000）
    idx_close = pd.Series(idx["close"].values, index=pd.to_datetime(idx["date"]))
    cfg = load_config()
    bt = cfg["backtest"] if "backtest" in cfg else cfg.get("bt", cfg)

    # 双基准
    bench = pd.Series(md.benchmark_close, index=md.dates).dropna()
    bench = bench / bench.iloc[0]
    equal = md.close_val.mean(axis=1, skipna=True)
    equal = equal / equal.iloc[0]
    bench = bench[(bench.index >= START) & (bench.index <= END)]
    equal = equal[(equal.index >= START) & (equal.index <= END)]
    bench_cagr = bench.iloc[-1] ** (1 / (len(bench) / 244.0)) - 1
    equal_cagr = equal.iloc[-1] ** (1 / (len(equal) / 244.0)) - 1

    results = []
    variants = [("原版复现", ORIG), ("V1+避雷", V1), ("V2+缓冲区", V2),
                ("V3+择时升级", V3), ("V4+二级因子", V4), ("V5全量优化", FULL)]
    for label, params in variants:
        strat = SmallCapStrategy(idx_close, fin, listed, fund, **params)
        r = run_once(md, bt, strat, label)
        results.append(r)
        print(f"{label:<12} CAGR {r['cagr']*100:7.2f}%  MDD {r['mdd']*100:7.2f}%  "
              f"Sharpe {r['sharpe']:5.2f}  Calmar {r['calmar']:5.2f}  "
              f"换手 {r['annual_turnover']*100:5.0f}%  终值 {r['final']:>10,.0f}")

    full = results[-1]
    orig = results[0]
    print("\n== 双基准 ==")
    print(f"沪深300: {bench_cagr*100:.2f}%/年   同池等权: {equal_cagr*100:.2f}%/年")
    print(f"全量优化 超额: vs 沪深300 {(full['cagr']-bench_cagr)*100:+.1f}pp  "
          f"vs 同池等权 {(full['cagr']-equal_cagr)*100:+.1f}pp")
    print(f"原版     超额: vs 沪深300 {(orig['cagr']-bench_cagr)*100:+.1f}pp  "
          f"vs 同池等权 {(orig['cagr']-equal_cagr)*100:+.1f}pp")

    print("\n== 分年度（原版 / 全量优化）==")
    yo, yf = yearly_returns(orig["equity"]), yearly_returns(full["equity"])
    for yr in sorted(set(yo) | set(yf)):
        print(f"  {yr}: 原版 {yo.get(yr, float('nan'))*100:7.2f}%   "
              f"全量 {yf.get(yr, float('nan'))*100:7.2f}%")

    print("\n== 压力段（原版 / 全量优化）==")
    for s, e, name in [("2024-01-02", "2024-02-08", "24年小微盘流动性危机"),
                       ("2024-04-12", "2024-06-30", "24年国九条退市恐慌")]:
        print(f"  {name}: 原版 {seg_return(orig['equity'], s, e)*100:7.2f}%   "
              f"全量 {seg_return(full['equity'], s, e)*100:7.2f}%")

    print("\n== 参数扰动（全量优化基础）==")
    pert_rows = []
    for label, dp in PERTS:
        strat = SmallCapStrategy(idx_close, fin, listed, fund, **{**FULL, **dp})
        r = run_once(md, bt, strat, label)
        pert_rows.append(r)
        print(f"{label:<16} CAGR {r['cagr']*100:7.2f}%  MDD {r['mdd']*100:7.2f}%  "
              f"Sharpe {r['sharpe']:5.2f}")

    out = {
        "variants": [{k: v for k, v in r.items() if k != "equity"} for r in results],
        "perturb": [{k: v for k, v in r.items() if k != "equity"} for r in pert_rows],
        "yearly_orig": yo, "yearly_full": yf,
        "bench": {"hs300_cagr": float(bench_cagr), "equal_weight_cagr": float(equal_cagr)},
        "stress": {name: {"orig": seg_return(orig["equity"], s, e),
                          "full": seg_return(full["equity"], s, e)}
                   for s, e, name in [("2024-01-02", "2024-02-08", "2024q1_liquidity"),
                                      ("2024-04-12", "2024-06-30", "2024q2_delisting")]},
    }
    Path("reports").mkdir(exist_ok=True)
    Path("reports/small_val_ab_2026-09-04.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=float)
    )
    print(f"\n[done] {time.time()-t0:.0f}s -> reports/small_val_ab_2026-09-04.json")


if __name__ == "__main__":
    main()

"""ht_backtest.py — 3万本金"热门板块轮动 + 板块内龙头"轻量模拟器。

定位：研究原型（非正式引擎）。独立于 quart/backtest 的形式化 gating 流程，
聚焦验证"板块轮动 + 龙头集中持仓"逻辑本身。自己实现 A股约束：
  - T+1：当日买入次日才可卖（用"买入日+1"标记持仓可卖）。
  - 整手：下单量向下取整到 100 股。
  - 费用：佣金(万2.5, 最低5元/笔) + 卖出印花税(万5) + 过户费忽略。
  - 3 万本金、集中 1~3 只、每月或每 N 日再平衡、随热门板块切换换仓。

数据输入（PIT、无前视）：
  bars   : (date, symbol, open, high, low, close, amount) 长表，已按模拟规则过滤可购。
  pool   : (date, symbol) 每交易日可购/活跃集合。
  sector : symbol -> cluster 映射。
  score  : (date, symbol) 龙头/强弱分数，None 则退化为板块内动量排序。

板块轮动规则（可参数化）：
  1) 每交易日算板块热度 heat(cluster,date) = 板块20日等权动量 + 涨停密度 + 成交额增速。
  2) 每 rebalance 期（默认月末）选 heat 平滑后 Top-hot 板块（默认取第 1）。
  3) 在该板块内选"可购 + 分数 Top"的龙头，凑足预算/仓位数（默认 2-3 只）。
  4) 若当前无热门板块信号或板块内买得起不足，则空仓（不硬买）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# A股费用参数
COMMISSION_RATE = 0.00025     # 佣金万2.5
COMMISSION_MIN = 5.0          # 最低5元/笔
STAMP_DUTY = 0.0005           # 印花税万5（仅卖出）
LOT = 100                     # 整手 100 股


def sector_heat_daily(bars: pd.DataFrame, sector: pd.Series) -> pd.DataFrame:
    """逐日每板块热度。bars 需含 ret_1(日收益) 或由 close 计算。
    返回 (date, cluster) 的 heat 日表（含涨停密度、动量、成交额增速、成分数）。"""
    b = bars.copy()
    b["symbol"] = b["symbol"].astype(str)
    b = b.merge(sector.rename("cluster").reset_index(), on="symbol", how="left")
    b = b.dropna(subset=["cluster"])
    b = b.sort_values(["symbol", "date"])
    # 个股日收益 / 5日 / 20日
    g = b.groupby("symbol")
    b["ret_1"] = g["close"].pct_change()
    b["ret_5"] = g["close"].pct_change(5)
    b["ret_20"] = g["close"].pct_change(20)
    b["is_limit_up"] = (b["ret_1"] >= 0.098).astype(float)
    amt20 = g["amount"].transform(lambda s: s.rolling(20).mean())
    b["amt_ratio"] = b["amount"] / amt20.replace(0, np.nan)
    sg = b.groupby(["date", "cluster"])
    heat = sg.agg(
        n=("symbol", "nunique"),
        avg_ret5=("ret_5", "mean"),
        avg_ret20=("ret_20", "mean"),
        n_limit=("is_limit_up", "sum"),
        amt_ratio_mean=("amt_ratio", "mean"),
    ).reset_index()
    heat["limit_density"] = heat["n_limit"] / heat["n"].replace(0, np.nan)
    # 平滑(5日)
    heat = heat.sort_values(["cluster", "date"])
    heat["avg_ret5_s"] = heat.groupby("cluster")["avg_ret5"].transform(lambda s: s.rolling(5, min_periods=1).mean())
    heat["avg_ret20_s"] = heat.groupby("cluster")["avg_ret20"].transform(lambda s: s.rolling(5, min_periods=1).mean())
    heat["density_s"] = heat.groupby("cluster")["limit_density"].transform(lambda s: s.rolling(5, min_periods=1).mean())
    # 热度分：动量(0.5) + 涨停密度(0.4) + 成交额增速(0.1)
    heat["heat"] = (0.5 * heat["avg_ret5_s"].clip(-0.05, 0.15)
                    + 0.4 * heat["density_s"].clip(0, 0.6)
                    + 0.1 * (heat["amt_ratio_mean"].clip(0.8, 1.5) - 1.0))
    return heat


def rebalance_dates(calendar: pd.Series, freq: str = "ME") -> list[pd.Timestamp]:
    """给定交易日历，返回每个 rebalance 期最后一个交易日。freq: ME/40D/20D。"""
    s = pd.Series(calendar, dtype="datetime64[ns]")
    if freq.endswith("ME"):
        idx = s.groupby(s.dt.to_period("M")).idxmax()
    elif freq.endswith("QE"):
        idx = s.groupby(s.dt.to_period("Q")).idxmax()
    else:
        # 固定交易日间隔
        step = int(freq.rstrip("D")) if freq.endswith("D") else 20
        idx = list(range(step - 1, len(s), step))
        return [s.iloc[i] for i in idx]
    return [s.iloc[i] for i in idx]


def run(
    bars: pd.DataFrame,
    pool: pd.DataFrame,
    sector: pd.Series,
    score: pd.DataFrame | None = None,
    capital: float = 30_000.0,
    n_leaders: int = 2,
    freq: str = "ME",
    hot_rank: int = 1,
    start: str | None = None,
    stop_loss: float | None = None,
    trail_stop: float | None = None,
    max_pos_weight: float | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """主模拟。返回逐日账户净值表：date, cash, market_value, equity, holdings, hot_sector。

    执行模型：在每个 rebalance 日 T（收盘决策），以 T 收盘价买入目标龙头；
    持仓在持有期间逐日盯市值；到下一 rebalance 日换仓。当日买入不可卖(T+1)由
    rebalance 间隔>1 天自然满足。换仓时先卖旧再买新，卖收印花税。只做多。

    风控（可关闭）：
      stop_loss : 硬止损比例（0~1）。持仓价从买入成本回撤超过该比例则次日触发卖出。
      trail_stop: 移动止损比例（0~1）。持仓从持有期最高市值回撤超过该比例则卖出。
      max_pos_weight: 单票市值上限（占当期总资产比例）。>0 时把资金在更多标的上摊薄，
                  降低集中度；用于实验"降集中度收敛回撤"。
    所有止损只在"买入后次日及以后"(满足 T+1) 触发。
    """
    bars = bars.copy()
    bars["date"] = pd.to_datetime(bars["date"])
    bars["symbol"] = bars["symbol"].astype(str)
    pool = pool.copy()
    pool["symbol"] = pool["symbol"].astype(str)
    pool["date"] = pd.to_datetime(pool["date"])

    calendar = pd.DatetimeIndex(sorted(bars["date"].unique()))
    if end is not None:
        calendar = calendar[calendar <= pd.Timestamp(end)]
    rb = [d for d in rebalance_dates(calendar, freq) if start is None or d >= pd.Timestamp(start)]
    if not rb:
        return pd.DataFrame(columns=["date", "cash", "market_value", "equity", "holdings", "hot_sector"])

    heat = sector_heat_daily(bars, sector)

    # 日期->符号收盘价。交易用当日实价(raw)；市值用前收 ffill 估值（停牌持仓仍计市值）
    closes_raw = bars.pivot_table(index="date", columns="symbol", values="close")
    closes = closes_raw                    # 用于买卖价格（仅当日真实有价才成交）
    closes_val = closes_raw.ffill()        # 用于每日市值（停牌用最后可得价）
    # score 表：若给，用于板块内排序
    sc_piv = None
    if score is not None and len(score):
        sc = score.copy()
        sc["date"] = pd.to_datetime(sc["date"])
        sc["symbol"] = sc["symbol"].astype(str)
        sc_piv = sc.pivot_table(index="date", columns="symbol", values="score")

    sim_start = pd.Timestamp(start) if start else calendar.min()
    # 进入模拟区间前的数据仅用于 warmup（板块热度/动量），不回测净值
    cash = capital
    positions: dict[str, float] = {}      # symbol -> shares(100股整手)
    buy_price: dict[str, float] = {}      # symbol -> 成交均价（成本，止损基准）
    buy_date: dict[str, object] = {}      # symbol -> 买入日
    peak_val: dict[str, float] = {}       # symbol -> 持仓期最高市值（移动止损基准）
    rows = []
    hot_at = None
    began = False

    def _sell(s, px):
        """按价卖出 symbol 全部，更新现金并清仓；返回回收现金(扣费后)。"""
        nonlocal cash
        q = positions[s]
        proceeds = q * px
        fee = max(proceeds * COMMISSION_RATE, COMMISSION_MIN) + proceeds * STAMP_DUTY
        cash += proceeds - fee
        del positions[s]
        buy_price.pop(s, None)
        buy_date.pop(s, None)
        peak_val.pop(s, None)

    for d in calendar:
        dstr = pd.Timestamp(d)
        if dstr < sim_start:
            # 预热期内仍允许在首个 rebalance 日前的板块热度被计算，但不下单不记账
            continue
        began = True
        # 当日是否再平衡日
        if dstr in rb:
            # 1) 选热门板块
            day_heat = heat[heat["date"] == dstr].sort_values("heat", ascending=False)
            if not day_heat.empty:
                ranked = day_heat.sort_values("heat", ascending=False)
                if len(ranked) >= hot_rank:
                    hot = ranked.iloc[hot_rank - 1]["cluster"]
                    hot_at = hot
                    # 2) 板块内可购标的
                    in_sector = pool.merge(sector.rename("cluster").reset_index(), on="symbol", how="left")
                    in_sector = in_sector[(in_sector["date"] == dstr) & (in_sector["cluster"] == hot)]
                    cand = sorted(in_sector["symbol"].unique())
                    # 3) 用分数(或动量)排序选龙头
                    priced = {s: closes.loc[dstr, s] for s in cand if pd.notna(closes.loc[dstr, s])}
                    if score is not None and sc_piv is not None:
                        sc_day = sc_piv.loc[dstr] if dstr in sc_piv.index else None
                        def keyfun(s):
                            p = priced.get(s)
                            if p is None:
                                return -np.inf
                            sv = sc_day.get(s) if sc_day is not None else np.nan
                            # 分数优先，缺失分数降级为动量(用 ret_20 在 closes 上近似? 简化用分数)
                            return float(sv) if pd.notna(sv) else -np.inf
                        cand_sorted = sorted(cand, key=keyfun, reverse=True)
                    else:
                        # 动量排序：用 5 日收益
                        ret5 = (closes.loc[dstr] / closes.shift(5).loc[dstr] - 1.0) if dstr in closes.index else pd.Series(dtype=float)
                        def keyfun2(s):
                            p = priced.get(s)
                            if p is None:
                                return -np.inf
                            r = ret5.get(s) if ret5 is not None else np.nan
                            return float(r) if pd.notna(r) else -np.inf
                        cand_sorted = sorted(cand, key=keyfun2, reverse=True)
                    # 4) 卖旧持仓（换仓）：仅当拿到有效价格才卖出并销仓；
                    #    停牌/无报价时保留持仓（否则会凭空消失导致净值崩坏）。
                    keep = set(cand_sorted[:max(n_leaders, 1)])
                    for s in list(positions):
                        if s in keep:
                            continue
                        px = priced.get(s)
                        if px is None:
                            cval = closes.loc[dstr, s] if dstr in closes.index and s in closes.columns else np.nan
                            px = float(cval) if pd.notna(cval) else None
                        if px is not None:
                            _sell(s, px)
                        # px is None => 停牌，保留持仓，等下个有价日再处理
                    # 5) 买目标龙头。单只预算上限 = min(现金/n_leaders, 若设 max_pos_weight 则当期总资产×max_pos_weight)
                    equity_now = cash + sum(q * (closes_val.loc[dstr, s] if dstr in closes_val.index and s in closes_val.columns else np.nan)
                                            for s, q in positions.items() if pd.notna(closes_val.loc[dstr, s]))
                    budget_per = capital / max(n_leaders, 1)
                    if max_pos_weight is not None and max_pos_weight > 0:
                        budget_per = min(budget_per, equity_now * max_pos_weight)
                    for s in cand_sorted[:n_leaders]:
                        if len(positions) >= n_leaders:
                            break
                        p = priced.get(s)
                        if p is None or s in positions:
                            continue
                        # 可买整手 = floor(可支配/ (p*100))，但单只预算不超 budget_per
                        affordable = int(cash / (p * LOT))
                        cap_by_budget = int(budget_per / (p * LOT))
                        q = max(0, min(affordable, cap_by_budget))
                        if q <= 0:
                            continue
                        cost = q * p * LOT
                        fee = max(cost * COMMISSION_RATE, COMMISSION_MIN)
                        if cost + fee > cash:
                            q = int((cash - fee) / (p * LOT))
                            if q <= 0:
                                continue
                            cost = q * p * LOT
                        cash -= cost + fee
                        positions[s] = positions.get(s, 0) + q * LOT
                        buy_price[s] = p
                        buy_date[s] = dstr
                        peak_val[s] = q * p * LOT

        # 每日风控止损（买入后次日及以后才允许触发，满足 T+1）
        if positions and (stop_loss is not None or trail_stop is not None):
            for s in list(positions):
                q = positions[s]
                if buy_price.get(s) is None or buy_date.get(s) == dstr:
                    continue  # 当日新买，T+1 不可卖
                # 当前可用价（原始收盘；停牌则用估值价）——仅作触发判断
                if dstr in closes_val.index and s in closes_val.columns and pd.notna(closes_val.loc[dstr, s]):
                    cur_px = float(closes_val.loc[dstr, s])
                else:
                    continue
                cur_val = q * cur_px
                if cur_px > peak_val.get(s, 0.0):
                    peak_val[s] = cur_px
                cur_peak = peak_val.get(s, cur_px)
                hit = False
                if stop_loss is not None and buy_price[s] > 0:
                    if cur_px <= buy_price[s] * (1.0 - stop_loss):
                        hit = True
                if (not hit) and trail_stop is not None and cur_peak > 0:
                    if cur_px <= cur_peak * (1.0 - trail_stop):
                        hit = True
                if hit:
                    # 实际以当日可成交价（原始收盘）卖出；停牌无法成交则保留
                    sell_px = float(closes.loc[dstr, s]) if dstr in closes.index and s in closes.columns and pd.notna(closes.loc[dstr, s]) else None
                    if sell_px is not None:
                        _sell(s, sell_px)

        # 每日盯市值（停牌持仓用前收 ffill 估值）
        mv = 0.0
        hl = []
        for s, q in positions.items():
            if dstr in closes_val.index and s in closes_val.columns and pd.notna(closes_val.loc[dstr, s]):
                mv += q * closes_val.loc[dstr, s]
                hl.append(s)
        rows.append({"date": dstr, "cash": cash, "market_value": mv,
                     "equity": cash + mv, "holdings": sorted(hl), "hot_sector": hot_at})
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame) -> dict:
    """净值表摘要：收益/年化/回撤/换手信息。"""
    if df.empty or len(df) < 2:
        return {}
    eq = df.set_index("date")["equity"]
    ret = eq.pct_change().dropna()
    total = eq.iloc[-1] / eq.iloc[0] - 1.0
    days = (eq.index[-1] - eq.index[0]).days
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (365.0 / max(days, 1)) - 1.0 if eq.iloc[0] > 0 else np.nan
    cummax = eq.cummax()
    mdd = float(((eq - cummax) / cummax).min())
    sharpe = float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() else np.nan
    return {"start": str(eq.index[0].date()), "end": str(eq.index[-1].date()),
            "total_ret": float(total), "cagr": float(cagr), "max_drawdown": mdd,
            "sharpe": sharpe, "days": days}

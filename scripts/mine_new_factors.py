"""新因子挖掘（R010）：构造平台未覆盖的量价/结构/高阶矩因子并审计 IC/ICIR。

目标：为"高年化+低回撤"寻找**新 alpha 源**。现有 43 个因子已覆盖低波/尾部/
反转/动量/量价/隔夜/日内/行业相对/流动性/价值/财报/事件拥挤/板块，本脚本挖掘
**真正的新维度**：

1. vol_of_vol60_neg       波动率之波动（波动稳定的股票风险更可控）
2. residual_mom60         残差动量（剔除市场 beta 后的纯个股动量）
3. vol_surprise20         量能突变（成交量相对自身历史的意外放大=资金进场）
4. turn_change20          换手率变化率（资金关注度加速）
5. vol_asym60             上下行波动不对称（新形式：上行波动/下行波动）
6. amount_concen20        成交额集中度（成交是否集中在少数日=脉冲式）
7. gap_fill20             跳空回补强度（缺口被回补的能力=价格韧性）
8. path_efficiency20      价格路径效率（趋势的"干净"程度，分形维思想）

时点安全：所有因子只用 ≤T 的数据（T 收盘形成，T+1 可执行），与平台不变量 1 一致。
评估：复用 quart.research.factor_audit.rank_correlation（无 scipy 依赖的 Spearman）。

用法
----
    python scripts/mine_new_factors.py --start 2019-01-01 --end 2026-08-31 --horizon 5
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from loguru import logger

from quart.config import load_config
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.research.factor_audit import rank_correlation


def _zscore(frame: pd.DataFrame) -> pd.DataFrame:
    mean = frame.mean(axis=1)
    std = frame.std(axis=1).replace(0, np.nan)
    return frame.sub(mean, axis=0).div(std, axis=0)


def build_candidate_panels(close: pd.DataFrame, open_: pd.DataFrame, high: pd.DataFrame,
                           low: pd.DataFrame, volume: pd.DataFrame, amount: pd.DataFrame,
                           turnover: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    """构造候选因子面板。全部只用 ≤T 数据。"""
    panels: dict[str, pd.DataFrame] = {}
    ret = close.pct_change(fill_method=None)
    vol20 = ret.rolling(20).std()
    vol60 = ret.rolling(60).std()

    # 1. 波动率之波动：波动率的变动（取负，波动稳定的更好）
    vol_of_vol = vol20.rolling(60).std()
    panels["vol_of_vol60_neg"] = -vol_of_vol

    # 2. 残差动量：剔除市场收益后的个股累计收益（市场=全截面等权日收益）
    market_ret = ret.mean(axis=1)
    # 用 60 日滚动 beta（个股 vs 市场）剔除系统性部分
    cov = ret.rolling(60).cov(market_ret)
    var = market_ret.rolling(60).var()
    beta = cov.div(var, axis=0)
    resid = ret - beta.mul(market_ret, axis=0)
    panels["residual_mom60"] = resid.rolling(60).sum()

    # 3. 量能突变：成交量 / 过去 60 日成交量中位数（意外放大）
    vol_med60 = volume.rolling(60, min_periods=40).median()
    panels["vol_surprise20"] = (volume / vol_med60.replace(0, np.nan)).rolling(20).mean()

    # 4. 换手率变化率（turnover 或 volume/流通股本代理）
    if turnover is not None and not turnover.empty:
        turn = turnover
    else:
        turn = volume  # 缺 turnover 时用成交量代理
    turn_ma20 = turn.rolling(20).mean()
    turn_ma60 = turn.rolling(60).mean()
    panels["turn_change20"] = (turn_ma20 / turn_ma60.replace(0, np.nan)) - 1.0

    # 5. 上下行波动不对称：上行波动 / 下行波动（上行波动大=更受追捧但也可能过热）
    up = ret.clip(lower=0)
    dn = (-ret.clip(upper=0))
    up_vol = up.rolling(60).std()
    dn_vol = dn.rolling(60).std()
    panels["vol_asym60"] = up_vol / dn_vol.replace(0, np.nan)

    # 6. 成交额集中度：20 日内最大单日成交额占比（脉冲式成交）
    amt_max20 = amount.rolling(20).max()
    amt_sum20 = amount.rolling(20).sum()
    panels["amount_concen20"] = amt_max20 / amt_sum20.replace(0, np.nan)

    # 7. 跳空回补强度：跳空缺口在当日被回补的比例（价格韧性）
    gap = open_ - close.shift(1)
    # 向下跳空后，当日收盘相对开盘的回补幅度（正=回补）
    gap_down = gap < 0
    intraday_move = (close - open_) / open_.replace(0, np.nan)
    fill = intraday_move.where(gap_down)
    panels["gap_fill20"] = fill.rolling(20, min_periods=10).mean()

    # 8. 价格路径效率：|累计收益| / 路径长度（1=直线趋势，越小=震荡）
    path_len = ret.abs().rolling(20).sum()
    total_move = (close / close.shift(20) - 1).abs()
    panels["path_efficiency20"] = total_move / path_len.replace(0, np.nan)

    return {k: v.astype("float32") for k, v in panels.items()}


def evaluate_panel(panel: pd.DataFrame, fwd_ret: pd.DataFrame,
                   dates: list[pd.Timestamp], sample: str) -> dict:
    """对单个因子面板算 IC 序列与 ICIR（与 factor_audit 同口径）。"""
    if sample == "monthly":
        step = 21
    elif sample == "weekly":
        step = 5
    else:
        step = 1
    ics: list[float] = []
    eval_dates: list[pd.Timestamp] = []
    for i in range(0, len(dates) - step, step):
        date = dates[i]
        fac = panel.loc[date].dropna() if date in panel.index else pd.Series(dtype=float)
        fwd = fwd_ret.loc[date].dropna() if date in fwd_ret.index else pd.Series(dtype=float)
        common = fac.index.intersection(fwd.index)
        if len(common) < 20:
            continue
        ic = rank_correlation(fac[common], fwd[common])
        if np.isfinite(ic):
            ics.append(ic)
            eval_dates.append(date)
    if len(ics) < 6:
        return {"ic": float("nan"), "icir": float("nan"), "n": len(ics)}
    arr = np.array(ics, dtype=float)
    ic_mean = float(arr.mean())
    ic_std = float(arr.std(ddof=0))
    return {
        "ic": ic_mean,
        "icir": ic_mean / ic_std if ic_std > 0 else float("nan"),
        "n": len(ics),
        "pos_rate": float((arr > 0).mean()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--horizon", type=int, default=5, help="前瞻收益窗口（交易日）")
    ap.add_argument("--sample", default="monthly")
    ap.add_argument("--eval-start", default="2021-01-01")
    args = ap.parse_args()

    cfg = load_config()
    store = BarStore()
    bars = store.load(start=args.start, end=args.end, include_index=False)
    bars = filter_for_simulation(bars, exclude_star=True, exclude_chinext=True,
                                 exclude_st=True, min_list_days=120)
    bench = store.load_benchmark(cfg["benchmark"])
    md = MarketData.from_bars(bars, bench)
    logger.info("loaded {} symbols / {} dates", len(md.symbols), len(md.dates))

    close = md.closes.astype("float64")
    open_ = md.opens.astype("float64")
    high = md.highs.astype("float64")
    low = md.lows.astype("float64")
    volume = md.volumes.astype("float64")
    amount = md.amounts.astype("float64")
    turnover = getattr(md, "turnover", None)

    panels = build_candidate_panels(close, open_, high, low, volume, amount, turnover)
    # 前瞻收益：T 收盘形成因子 → T+1 买入 → 持有 horizon 日
    fwd_ret = close.shift(-(1 + args.horizon)) / close.shift(-1) - 1

    eval_dates = [d for d in md.dates if d >= pd.Timestamp(args.eval_start)]
    rows = []
    for name, panel in panels.items():
        stats = evaluate_panel(panel, fwd_ret, eval_dates, args.sample)
        rows.append({"factor": name, **stats})
    out = pd.DataFrame(rows).sort_values("ic", key=abs, ascending=False)
    pd.set_option("display.width", 200)
    print("\n===== 新因子 IC 审计 ({} 样本, horizon={}) =====".format(args.sample, args.horizon))
    print(out.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    out.to_csv("reports/mined_factors_ic.csv", index=False)
    logger.info("saved -> reports/mined_factors_ic.csv")


if __name__ == "__main__":
    main()

"""ht_train.py — 热点/龙头 LightGBM 滚动训练（复用 domain 技术因子）。

目标（3 万本金追热点、规则优先 + ML 辅助）：
- 复用 quart.research.technicals.compute_panels 的研报技术因子（宽表）做特征底座；
- 训练池(宽)内做"未来 N 日横截面相对强弱"标签，LightGBM 学板块内/全池谁更强；
- 逐月 expanding-window 滚动：每月用此前全部数据拟合，预测下一月；样本外算 Rank IC；
- 输出逐月 IC / ICIR / 正样本占比，并把每月预测分数落盘供回测/板块引擎消费。

无前视：
- 特征全部用 T 日及之前（compute_panels 保证）；
- 标签 = T 日后 N 个交易日的截面相对收益（信号偏移由回测层 T+1 执行）；
- 训练/测试切分严格按日期，train_mask = 日期 < 预测起点。

为控制内存，特征在训练前先按池过滤、dropna、去常数列。
"""
from __future__ import annotations

from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.research.ht_universe import build_pools, training_pool
from quart.research.technicals import compute_panels, stack_panels

LGB_PARAMS = {
    "objective": "regression",
    "num_leaves": 48,
    "learning_rate": 0.05,
    "n_estimators": 300,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.7,
    "min_child_samples": 50,
    "reg_alpha": 0.05,
    "reg_lambda": 0.1,
    "verbosity": -1,
    "random_state": 42,
}


def _forward_ret_wide(closes: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """未来 horizon 交易日收益（无前视：T 日值=close_{T+horizon}/close_T-1，
    需保证只含 <= 该收益窗口。用于 T 日收盘后预测。"""
    return closes.shift(-horizon) / closes - 1.0


def compute_feature_long(md: MarketData, tech_names: list[str] | None = None,
                         warm_days: int = 65) -> pd.DataFrame:
    """把技术因子面板 stack 为长表特征 (date, symbol, fea_*)。"""
    panels = compute_panels(md, names=tech_names)
    fea = stack_panels(panels)
    fea = fea.rename(columns={c: f"fea_{c}" for c in fea.columns})
    fea = fea.reset_index()
    # stack_panels 索引名为 datetime, instrument -> 统一为 date, symbol
    fea = fea.rename(columns={
        "datetime": "date",
        "instrument": "symbol",
        "level_0": "date",
    })
    if "symbol" not in fea.columns:
        fea = fea.rename(columns={"level_1": "symbol"})
    return fea


def add_sector_hot_features(fea: pd.DataFrame, industry: pd.Series) -> pd.DataFrame:
    """拼接板块归属 cluster（symbol -> cluster）。"""
    ind_map = pd.DataFrame({
        "symbol": industry.index.astype(str),
        "cluster": industry.to_numpy(),
    })
    out = fea.copy()
    out["symbol"] = out["symbol"].astype(str)
    out = out.merge(ind_map, on="symbol", how="left")
    return out


def label_and_filter(
    fea: pd.DataFrame,
    closes: pd.DataFrame,
    horizon: int,
    training: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series]:
    """为 fea 打上"未来 horizon 横截面相对强弱"标签，并只保留训练池内样本。

    fea : (date, symbol, fea_*) 特征长表。
    closes : date x symbol 面板。
    training : 训练池长表(date, symbol) -> 用于限定横截面空间。
    标签 = 未来 horizon 收益在该日训练池内的横截面 z-score（越大=相对越强）。
    返回 (X_with_date, y, symbols)，三者行对齐。
    """
    fwd = _forward_ret_wide(closes, horizon)  # date x symbol
    fwd_long = fwd.stack(future_stack=True).rename("fwd_ret").reset_index()
    fwd_long.columns = ["date", "symbol", "fwd_ret"]

    # 限定训练池（动态集合）内的横截面
    pool = training[["date", "symbol"]].copy()
    pool["symbol"] = pool["symbol"].astype(str)
    fwd_long["symbol"] = fwd_long["symbol"].astype(str)
    fwd_long = fwd_long.merge(pool, on=["date", "symbol"], how="inner")

    # 截面 z 化标签（组内 mean/std）
    g = fwd_long.groupby("date")["fwd_ret"]
    fwd_long["label"] = (fwd_long["fwd_ret"] - g.transform("mean")) / g.transform("std")

    # 与特征对齐（fea 已含 date, symbol）
    fea = fea.copy()
    fea["symbol"] = fea["symbol"].astype(str)
    m = fea.merge(fwd_long[["date", "symbol", "label"]], on=["date", "symbol"], how="inner")
    m = m.dropna(subset=["label"])
    # 只保留特征列 + date + label
    y = m["label"].reset_index(drop=True)
    syms = m["symbol"].reset_index(drop=True)
    X = m[[c for c in m.columns if c.startswith("fea_")]].copy().reset_index(drop=True)
    X = X.replace([np.inf, -np.inf], np.nan)
    keep = X.columns[X.notna().mean() > 0.5]  # 剔除 >50% NaN 的特征列
    X = X[keep]
    dates = m["date"].reset_index(drop=True)
    return pd.concat([dates, X], axis=1), y, syms


def rolling_ic(
    Xy_dates: pd.Series,
    X: pd.DataFrame,
    y: pd.Series,
    min_train_bars: int = 120,
) -> tuple[list[dict], list[pd.Series]]:
    """逐月 expanding rolling：每月末预测下月，样本外算 Rank IC。
    返回 (ic_rows, score_series 列表)。"""
    dates = Xy_dates.sort_values().unique()
    months = pd.PeriodIndex(pd.to_datetime(dates), freq="M").unique()
    ic_rows: list[dict] = []
    score_list: list[pd.Series] = []
    # 需要按日期排序后的训练起点：第一个被预测的月份之前至少有 min_train_bars 天
    sorted_d = pd.Series(dates)
    # 定位第一个可作为"预测起点"的月份
    dts = pd.to_datetime(Xy_dates)
    last_train_day = dts.min() + pd.Timedelta(days=min_train_bars * 1.5)
    for i, m in enumerate(months):
        m_start = m.to_timestamp()
        if m_start <= last_train_day:
            continue
        m_end = (m + 1).to_timestamp()
        train_mask = (dts < m_start).to_numpy()
        test_mask = ((dts >= m_start) & (dts < m_end)).to_numpy()
        if train_mask.sum() < 500 or test_mask.sum() < 50:
            continue
        Xtr, ytr = X[train_mask], y[train_mask]
        Xte, yte = X[test_mask], y[test_mask]
        valid = ytr.notna().to_numpy()
        Xtr, ytr = Xtr[valid], ytr[valid]
        if len(Xtr) < 1000 or len(ytr) < 100:
            continue
        # 划分内部验证集
        n = len(Xtr)
        n_val = max(int(n * 0.1), 50)
        model = lgb.LGBMRegressor(**LGB_PARAMS)
        model.fit(
            Xtr.iloc[: n - n_val], ytr.iloc[: n - n_val],
            eval_set=[(Xtr.iloc[n - n_val:], ytr.iloc[n - n_val:])],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        pred = pd.Series(model.predict(Xte), index=Xte.index, name="score")
        ic = _rank_ic(pred, yte)
        ic_rows.append({"month": str(m), "ic": ic, "n_test": int(len(pred))})
        score_list.append(pd.concat([pred, yte.rename("label")], axis=1))
    return ic_rows, score_list


def _rank_ic(pred: pd.Series, realized: pd.Series) -> float:
    j = pd.concat([pred, realized], axis=1).dropna()
    if len(j) < 10:
        return float("nan")
    r = j.rank()
    return float(r.iloc[:, 0].corr(r.iloc[:, 1]))


def predict_rebalance_scores(
    dates: pd.Series,
    symbols: pd.Series,
    X: pd.DataFrame,
    y: pd.Series,
    calendar: pd.Series,
    min_train_bars: int = 120,
) -> tuple[pd.DataFrame, list[dict]]:
    """逐月 expanding 训练，输出"每月最后一个交易日"再平衡日的预测分数。

    用于回测：在 T 日（该月末）用"仅含 T 之前数据"训练的模型，预测 T 当日
    候选股未来 horizon 的横截面强弱，作为选龙头的分数。无前视。
    dates/symbols 与 X/y 行对齐。

    返回 (score_df, ic_rows)。score_df 列: date, symbol, score。
    """
    dts = pd.to_datetime(dates.to_numpy())
    syms = symbols.to_numpy().astype(str)
    months = pd.PeriodIndex(dts, freq="M").unique()
    cal = pd.Series(pd.to_datetime(calendar)).sort_values()
    # 每个自然月的最后一个交易日（回测再平衡日）
    rb_index = cal.groupby(cal.dt.to_period("M")).idxmax()
    rb_days = {pd.Timestamp(cal.iloc[i]) for i in rb_index}

    last_train_day = dts.min() + pd.Timedelta(days=min_train_bars * 1.5)
    score_rows: list[pd.DataFrame] = []
    ic_rows: list[dict] = []
    idx_arr = X.index.to_numpy()
    for m in months:
        m_start = m.to_timestamp()
        m_next = (m + 1).to_timestamp()
        if m_start <= last_train_day:
            continue
        train_mask = (dts < m_start)
        if int(train_mask.sum()) < 500:
            continue
        Xtr, ytr = X[train_mask], y[train_mask]
        valid = ytr.notna().to_numpy()
        Xtr, ytr = Xtr[valid], ytr[valid]
        if len(Xtr) < 1000:
            continue
        n = len(Xtr)
        n_val = max(int(n * 0.1), 50)
        model = lgb.LGBMRegressor(**LGB_PARAMS)
        model.fit(
            Xtr.iloc[: n - n_val], ytr.iloc[: n - n_val],
            eval_set=[(Xtr.iloc[n - n_val:], ytr.iloc[n - n_val:])],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        for target in sorted(rb_days):
            if m_start <= target < m_next:
                mask = (dts == target)
                if int(mask.sum()) < 5:
                    continue
                pred = model.predict(X[mask])
                df = pd.DataFrame({
                    "date": pd.Timestamp(target),
                    "symbol": syms[mask],
                    "score": pred,
                })
                score_rows.append(df)
                yte = y[mask].to_numpy()
                ic = _rank_ic(pd.Series(pred), pd.Series(yte))
                ic_rows.append({"date": str(target.date()), "ic": ic, "n": int(len(pred))})
    if not score_rows:
        return pd.DataFrame(columns=["date", "symbol", "score"]), ic_rows
    return pd.concat(score_rows, ignore_index=True), ic_rows

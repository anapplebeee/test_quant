"""RESEARCH-003 方向五评估：LambdaRank 排序学习合成（轻量原型）。

用法:
    uv run python scripts/eval_lambdarank.py

思路（西南证券 2025-12 研报的落地检验）：用现有因子特征（涨停拥挤三因子 +
龙虎榜净买衰减 + 公告情绪衰减 + 财报稳定 + 波动/换手），以 LightGBM
LambdaRank 拟合"排序一致性"而非精确收益。每个交易日作为一个 query group，
label 为 fwd5d 收益的五分位等级。

OOS 合规：
- 训练窗 2020-01-01 ~ 2022-12-31（含 TimeSeriesSplit CV 防过拟合），
- 2023-01-01+ 仅做**一次**诊断评估（RankIC/多空），不得据此回改特征与超参。

诚实声明：特征都是公开量价/事件/财报因子，无日内数据；该原型验证"非线性
合成管线是否可运行、是否显著优于线性 RankIC"，不追求华泰/国泰海通的增强水平。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger
from rich.console import Console
from rich.table import Table

from quart.config import data_root, load_config
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.research.event_factors import (
    dragon_tiger_panels,
    event_sentiment_panels,
    limit_event_panels,
)
from quart.research.factor_audit import rank_correlation
from scripts.mine_factors import build_financial_factors

console = Console()

HORIZON = 5
TRAIN_END = pd.Timestamp("2023-01-01")
SAMPLING = 5  # 每 5 个交易日取一个截面，控制训练集规模


def build_feature_matrix(md, factors: dict[str, pd.DataFrame], dates: list[pd.Timestamp]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """在评估日截面上堆叠特征（date,symbol）长表 + fwd5d label 长表。"""
    fw = {name: f.reindex(index=md.dates, columns=md.symbols) for name, f in factors.items()}
    frames = []
    labels = []
    for i in dates:
        rows = pd.DataFrame({name: f.iloc[i] for name, f in fw.items()})
        rows["date"] = md.dates[i]
        rows["symbol"] = md.symbols
        # 弹性过滤：非空特征数>=3 才入样本
        rows = rows.dropna(thresh=max(3, len(fw) - 2))
        if rows.empty:
            continue
        frames.append(rows)
        lab = md.opens.shift(-(HORIZON + 1)).iloc[i] / md.opens.shift(-1).iloc[i].replace(0, np.nan) - 1.0
        labels.append(lab.reindex(rows["symbol"]).rename("label"))
    feats = pd.concat(frames, ignore_index=True)
    labs = pd.concat(labels)
    return feats, labs


def main() -> None:
    import lightgbm as lgb

    cfg = load_config()
    store = BarStore()
    bars = store.load(start="2019-06-01")
    bench = store.load_benchmark(cfg["benchmark"])
    if bars.empty:
        raise SystemExit("本地数据为空")
    dc = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=dc.get("exclude_star", True),
        exclude_chinext=dc.get("exclude_chinext", True),
        exclude_st=dc.get("exclude_st", True),
        min_list_days=int(dc.get("min_list_days", 0)),
    )
    md = MarketData.from_bars(bars, benchmark=bench)

    # ---- 特征面板 ----
    factors: dict[str, pd.DataFrame] = {}
    factors.update(limit_event_panels(md))
    factors["vol20"] = md.close_val.pct_change().rolling(20).std()
    factors["turnover20"] = md.volumes.div(md.volumes.shift(1).rolling(20).mean()).rolling(20).mean()
    factors["amount_log"] = np.log1p(md.amounts)
    fin_path = data_root() / "factors" / "financials.parquet"
    if fin_path.exists():
        factors.update(build_financial_factors(pd.read_parquet(fin_path), md.close_val))
    for name, path in (
        ("dragon", data_root() / "events" / "dragon_tiger.parquet"),
        ("news", data_root() / "events" / "news.parquet"),
    ):
        if path.exists():
            ev = pd.read_parquet(path)
            panels = dragon_tiger_panels(ev, md.dates, md.symbols) if name == "dragon" else event_sentiment_panels(ev, md.dates, md.symbols)
            key = "dragon_tiger_net_buy_decay" if name == "dragon" else "event_sentiment_decay"
            factors[key] = panels[key]

    dates_all = list(range(100, len(md.dates) - HORIZON, SAMPLING))
    feats_all, labs_all = build_feature_matrix(md, factors, dates_all)
    feats_all = feats_all.assign(label=labs_all.to_numpy())
    date_of = pd.DatetimeIndex(feats_all["date"])
    train_mask = date_of < TRAIN_END
    console.print(f"样本 {len(feats_all):,} | 训练 {int(train_mask.sum()):,} | "
                  f"诊断 {int((~train_mask).sum()):,} | 特征 {len(feats_all.columns) - 3}")

    feat_cols = [c for c in feats_all.columns if c not in ("date", "symbol", "label")]

    # ---- 训练（TimeSeriesSplit CV + 全量重训）----
    def _make_lgbds(df: pd.DataFrame) -> tuple:
        df = df.dropna(subset=["label"]).replace([np.inf, -np.inf], np.nan).dropna(subset=["label"])
        y = pd.cut(
            df["label"].rank(pct=True), bins=5, labels=False, include_lowest=True
        ).astype(int)
        groups = df.groupby("date").size().to_numpy()
        d = lgb.Dataset(df[feat_cols], label=y, group=groups)
        return d

    params = dict(
        objective="lambdarank",
        metric="ndcg",
        label_gain=[0, 1, 3, 7, 15],
        learning_rate=0.05,
        num_leaves=31,
        min_data_in_leaf=50,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=1,
        verbosity=-1,
    )
    console.print("\n[bold]TimeSeriesSplit 交叉验证（训练窗内）[/bold]")
    cvs = feats_all[train_mask]
    n_folds = 3
    fold_dates = pd.DatetimeIndex(cvs["date"]).unique()
    edges = np.array_split(np.arange(len(fold_dates)), n_folds)
    cv_ics = []
    for k in range(n_folds):
        val_idx = edges[k]
        val_dates = fold_dates[val_idx]
        tr = cvs[~cvs["date"].isin(val_dates)]
        va = cvs[cvs["date"].isin(val_dates)]
        dtr, dva = _make_lgbds(tr), _make_lgbds(va)
        bst = lgb.train(params, dtr, num_boost_round=200, valid_sets=[dva])
        preds = bst.predict(va[feat_cols])
        va_df = va.assign(score=preds)
        ics = []
        for _, g in va_df.groupby("date"):
            if len(g) >= 300:
                ics.append(rank_correlation(g["score"], g["label"]))
        ics = np.asarray(ics, dtype="float64")
        ics = ics[~np.isnan(ics)]
        cv_ics.append((tr["date"].max(), float(ics.mean())) if ics.size else (tr["date"].max(), np.nan))
        console.print(f"  fold {k}: train_end={tr['date'].max().date()} cv_ic={ics.mean():.4f} (n={ics.size})")
    console.print(f"  CV 平均 RankIC: {np.nanmean([c[1] for c in cv_ics]):.4f}")

    # ---- 全量重训 + 诊断段单次评估 ----
    dtr_full = _make_lgbds(cvs)
    bst = lgb.train(params, dtr_full, num_boost_round=200)
    diag = feats_all[~train_mask]
    preds = bst.predict(diag[feat_cols])
    diag_df = diag.assign(score=preds)
    ics, spreads = [], []
    for _, g in diag_df.groupby("date"):
        if len(g) < 300:
            continue
        ics.append(rank_correlation(g["score"], g["label"]))
        dec = len(g) // 10
        top = g.nlargest(dec, "score")["label"].clip(-0.3, 1.0).mean()
        bot = g.nsmallest(dec, "score")["label"].clip(-0.3, 1.0).mean()
        spreads.append(top - bot)
    ics = np.asarray(ics, dtype="float64")
    ics = ics[~np.isnan(ics)]
    table = Table(title="诊断段（2023-01+，单次评估）LambdaRank 合成 vs 基准")
    table.add_column("metric", justify="right")
    table.add_column("value", justify="right")
    for row in (
        ("RankIC", f"{ics.mean():.4f}" if ics.size else "-"),
        ("ICIR", f"{ics.mean() / ics.std() if ics.size and ics.std() else float('nan'):.2f}"),
        ("正率", f"{(ics > 0).mean():.0%}" if ics.size else "-"),
        ("截面数", str(ics.size)),
        ("多空(前/后十分位均 vs 均)", f"{(np.nanmean(spreads)) * 1e4:.1f} bp" if spreads else "-"),
    ):
        table.add_row(*row)
    console.print(table)
    imp = pd.Series(bst.feature_importance("gain"), index=feat_cols).sort_values(ascending=False)
    console.print("\n[bold]特征重要性（gain）[/bold]")
    for name, v in imp.head(10).items():
        console.print(f"  {name:36s} {v:,.0f}")
    console.print(
        "\n判读:诊断段为正且 ICIR>=0.2 才算初步可行;否则结论为"
        "'非线性合成在当前特征集上无增益'。训练窗内 CV 用于防过拟合,诊断段只观察一次。"
    )


if __name__ == "__main__":
    logger.remove()
    main()
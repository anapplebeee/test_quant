from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.config import PROJECT_ROOT

console = Console()

DEFAULT_PARAMS = {
    "objective": "regression",
    "num_leaves": 64,
    "learning_rate": 0.05,
    "n_estimators": 400,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "min_child_samples": 100,
    "reg_alpha": 0.1,
    "reg_lambda": 0.1,
    "verbosity": -1,
}


def build_handler(start_time: str, end_time: str):
    import qlib
    from qlib.contrib.data.handler import Alpha158

    qlib.init(
        provider_uri=str(PROJECT_ROOT / "data" / "qlib"),
        region="cn",
        logging_level=logging.WARNING,
        kernels=1,
    )
    return Alpha158(
        instruments="all",
        start_time=start_time,
        end_time=end_time,
        fit_start_time=start_time,
        fit_end_time=end_time,
        label=["Ref($close,-2) / Ref($close,-1) - 1"],
    )


def fetch_feature_label(handler) -> tuple[pd.DataFrame, pd.Series]:
    data = handler.fetch(col_set=["feature", "label"])
    if isinstance(data.columns, pd.MultiIndex):
        features = data.loc[:, data.columns.get_level_values(0) == "feature"]
        features.columns = features.columns.get_level_values(1)
        label_col = data.loc[:, data.columns.get_level_values(0) == "label"]
        label = label_col.iloc[:, 0]
    else:
        features = data
        label = data.iloc[:, -1]
    return features, label


def month_starts(dates: pd.DatetimeIndex, min_train_bars: int) -> list[pd.Timestamp]:
    dates = dates.sort_values().unique()
    cutoff = dates[min(min_train_bars, len(dates) - 1)]
    months = pd.PeriodIndex(pd.to_datetime(dates[dates > cutoff]), freq="M").unique().sort_values()
    return [p.to_timestamp() for p in months]


def rank_ic(pred: pd.Series, realized: pd.Series) -> float:
    joined = pd.concat([pred, realized], axis=1, join="inner").dropna()
    if len(joined) < 10:
        return float("nan")
    return float(joined.corr(method="spearman").iloc[0, 1])


def sanitize(X: pd.DataFrame, y: pd.Series | None = None) -> tuple[pd.DataFrame, pd.Series | None]:
    X = X.replace([np.inf, -np.inf], np.nan)
    if y is not None:
        y = y.replace([np.inf, -np.inf], np.nan)
    return X, y


def augment_research_factors(features: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """在 Alpha158 特征上追加研报因子：技术指标面板（R3/R4）+ PIT 价值成长（R2）。

    - 技术因子：本地 BarStore 日线 → quart.research.technicals.compute_panels
      → stack 后与 qlib instrument 代码（原始 symbol）直接 join；
    - 价值成长：data/factors/financials.parquet → pit_features，
      内置 120 天披露时滞，训练期无前视；
    - 任一步骤失败降级为纯 Alpha158（警告不中断训练）。
    """
    try:
        from quart.data.market import MarketData
        from quart.data.store import BarStore
        from quart.config import load_config
        from quart.research.technicals import compute_panels, stack_panels

        console.print("[blue]computing research factors (technical + value-growth)...[/blue]")
        # 指标预热：多加载约 180 个自然日的日线，避免前 60 个交易日的技术因子全 NaN
        warm_start = (pd.Timestamp(start) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
        store = BarStore()
        bars = store.load(start=warm_start, end=end)
        if bars.empty:
            console.print("[yellow]no bars available, skip research factors[/yellow]")
            return features
        bench = store.load_benchmark(load_config()["benchmark"])
        bench = bench[
            (bench["date"] >= warm_start) & ((end is None) | (bench["date"] <= end))
        ]
        md = MarketData.from_bars(bars, benchmark=bench)
        tech = stack_panels(compute_panels(md))
        out = features.join(tech, how="left")
        n_tech = tech.shape[1]

        n_vg = 0
        fin_path = PROJECT_ROOT / "data" / "factors" / "financials.parquet"
        if fin_path.exists():
            from quart.research.value_growth import pit_features

            fin = pd.read_parquet(fin_path)
            vg = pit_features(fin, features.index)
            if not vg.empty:
                out = out.join(vg, how="left")
                n_vg = vg.shape[1]
        else:
            console.print("[yellow]financials.parquet not found, skip value-growth factors[/yellow]")
        console.print(
            f"[green]features augmented[/green]: +{n_tech} technical, "
            f"+{n_vg} value-growth -> {out.shape[1]} cols"
        )
        return out
    except Exception as exc:
        console.print(
            f"[yellow]research factor augmentation failed, using Alpha158 only: {exc}[/yellow]"
        )
        return features


def main() -> None:
    parser = argparse.ArgumentParser(description="Rolling walk-forward Alpha158+LGBM training")
    parser.add_argument("--start", default=None, help="data start (default: earliest)")
    parser.add_argument("--end", default=None, help="data end (default: latest)")
    parser.add_argument("--min-train-bars", type=int, default=190, help="trading bars required before first prediction month")
    parser.add_argument("--out", default=None, help="scores output path")
    args = parser.parse_args()

    handler = build_handler(args.start or "2024-01-01", args.end or "2099-12-31")
    console.print("[blue]computing Alpha158 features...[/blue]")
    features, label = fetch_feature_label(handler)
    features = augment_research_factors(features, args.start or "2024-01-01", args.end)

    idx = features.index.get_level_values(0)
    dates = pd.DatetimeIndex(idx.unique())
    months = month_starts(dates, args.min_train_bars)
    if not months:
        raise SystemExit(
            f"训练窗口不足：可用日期 {len(dates)} 个，"
            f"需至少 min_train_bars={args.min_train_bars} 个预热月份。"
            f"请扩大 --start 范围或降低 --min-train-bars。"
        )
    console.print(f"features {features.shape}, rolling over {len(months)} months: {months[0].date()} ~ {months[-1].date()}")

    preds_frames = []
    ic_rows = []
    model_params_used = None

    for i, m_start in enumerate(months):
        m_end = months[i + 1] if i + 1 < len(months) else dates.max() + pd.Timedelta(days=31)
        train_mask = idx < m_start
        test_mask = (idx >= m_start) & (idx < m_end)

        X_tr = features[train_mask]
        y_tr = label[train_mask]
        X_tr, y_tr = sanitize(X_tr, y_tr)
        valid_rows = y_tr.notna()
        X_tr, y_tr = X_tr[valid_rows], y_tr[valid_rows]

        X_te = features[test_mask]
        X_te, _ = sanitize(X_te)
        y_te = label[test_mask]

        n_valid_end = max(int(len(X_tr) * 0.9), 1)
        model = lgb.LGBMRegressor(**DEFAULT_PARAMS)
        model.fit(
            X_tr.iloc[:n_valid_end], y_tr.iloc[:n_valid_end],
            eval_set=[(X_tr.iloc[n_valid_end:], y_tr.iloc[n_valid_end:])],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        model_params_used = model.get_params()

        pred = pd.Series(model.predict(X_te), index=X_te.index, name="score")
        ic = rank_ic(pred, y_te)
        ic_rows.append({"month": str(m_start.date()), "ic": ic, "n_test": int(len(pred)), "best_iter": model.best_iteration_ or DEFAULT_PARAMS["n_estimators"]})
        preds_frames.append(pred.reset_index().rename(columns={"level_0": "datetime", "instrument": "instrument"}))
        console.print(f"  {m_start.date()}  IC={ic:+.3f}  n={len(pred)}")

    preds = pd.concat(preds_frames, ignore_index=True)[["datetime", "instrument", "score"]]
    out_path = Path(args.out) if args.out else PROJECT_ROOT / "data" / "scores" / "preds.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    preds.to_csv(out_path, index=False)

    ic_df = pd.DataFrame(ic_rows)
    ic_mean, ic_std = ic_df["ic"].mean(), ic_df["ic"].std()
    positive_ratio = (ic_df["ic"] > 0).mean()

    table = Table(title=f"Rank IC ({len(ic_df)} months)")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("IC mean", f"{ic_mean:+.4f}")
    table.add_row("IC std", f"{ic_std:.4f}")
    table.add_row("ICIR", f"{ic_mean / ic_std:.2f}" if ic_std else "-")
    table.add_row("positive months", f"{positive_ratio:.0%}")
    console.print(table)

    meta = {
        "params": {k: v for k, v in (model_params_used or {}).items()},
        "months": [r["month"] for r in ic_rows],
        "ic_mean": float(ic_mean),
        "icir": float(ic_mean / ic_std) if ic_std else None,
        "scores_path": str(out_path),
    }
    meta_path = out_path.parent / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    console.print(f"[green]scores saved: {out_path}[/green]")


if __name__ == "__main__":
    main()

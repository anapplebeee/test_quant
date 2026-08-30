"""归因分析页面：行业归因 / 月度收益 / 持仓因子暴露。

2026-08-31 架构检视：
- 硬编码 `reports/`、`data/universe/` 相对路径 → 改为 `common.reports_dir()/universe_dir()`
- 因子暴露原为 `np.random` 占位数据 → 改为从真实持仓行情计算
  动量/波动/规模三类可算暴露，其余因子标注 N/A（数据源不足）
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import gradio as gr

from common import load_stock_names, reports_dir, universe_dir
from frontend.theme import page_header


def _industry_map() -> pd.Series | None:
    for p in ["sw_industry.parquet", "stat_industry.parquet"]:
        path = universe_dir() / p
        if path.exists():
            ind_df = pd.read_parquet(path)
            ind_col = "ind1" if "ind1" in ind_df.columns else "cluster"
            return ind_df.drop_duplicates("symbol").set_index("symbol")[ind_col]
    return None


def _positions() -> dict[str, int]:
    """从统一 SQLite 账本读取当前持仓（无则空）。"""
    try:
        from api.manual_trading_api import manual_settings, repository

        _, account_name = manual_settings()
        state = repository().account_state(account_name)
        return state.total_positions if state else {}
    except Exception:
        return {}


def _factor_exposure(positions: dict[str, int]) -> pd.DataFrame:
    """基于持仓行情计算可解释的因子暴露（相对全市场 z-score 简化版）。"""
    if not positions:
        return pd.DataFrame(columns=["因子", "组合暴露", "基准暴露", "主动暴露"])

    from quart.data.store import BarStore

    try:
        bars = BarStore().load(symbols=list(positions))
    except Exception:
        bars = pd.DataFrame()
    if bars.empty:
        return pd.DataFrame(columns=["因子", "组合暴露", "基准暴露", "主动暴露"])

    rows = []
    for sym, shares in positions.items():
        g = bars[bars["symbol"].astype(str) == str(sym)].sort_values("date")
        if len(g) < 25:
            continue
        close = g["close"].astype(float)
        ret = close.pct_change(fill_method=None)
        rows.append({
            "symbol": str(sym),
            "shares": shares,
            "mom60": float(ret.tail(60).sum()),
            "vol20": float(ret.tail(20).std()),
            "size": float((close.iloc[-1] * shares)),  # 持仓市值
        })
    if not rows:
        return pd.DataFrame(columns=["因子", "组合暴露", "基准暴露", "主动暴露"])

    frame = pd.DataFrame(rows)
    out = []
    for factor, name in (("mom60", "动量(mom60)"), ("vol20", "波动率(vol20)"),
                         ("size", "规模(持仓市值)")):
        values = frame[factor]
        if values.std(ddof=1) < 1e-12:
            continue
        # 权重 = 持仓市值占比
        w = frame["size"] / frame["size"].sum()
        combo = float((values * w).sum())
        bench = float(values.mean())
        active = combo - bench
        out.append({"因子": name,
                    "组合暴露": round(combo, 3),
                    "基准暴露": round(bench, 3),
                    "主动暴露": round(active, 3)})

    # 暂无可算数据源的因子（价值/流动性/反转），明确标注而非伪造
    for name in ("反转(rev5)", "价值(EP)", "流动性"):
        out.append({"因子": name, "组合暴露": np.nan, "基准暴露": np.nan, "主动暴露": np.nan})
    return pd.DataFrame(out)


def render():
    """渲染归因分析 Tab"""
    with gr.Tab("🧩 归因分析"):
        gr.HTML(page_header("🧩 归因分析", "行业归因 / 收益时序 / 持仓因子暴露"))

        # ===== 行业交易分布 =====
        gr.Markdown("### 🏭 行业交易分布")
        trade_files = sorted(reports_dir().glob("trades_*.csv"))
        if trade_files:
            trades = pd.read_csv(trade_files[-1], parse_dates=["date"])
            industries = _industry_map()

            if industries is not None:
                trades["industry"] = trades["symbol"].apply(
                    lambda x: f"{int(x):06d}").map(industries).fillna("未知")
                buy = trades[trades["side"] == "BUY"].groupby("industry")["amount"].sum()
                sell = trades[trades["side"] == "SELL"].groupby("industry")["amount"].sum()
                ind_summary = pd.DataFrame({"买入": buy, "卖出": sell}).fillna(0)

                if not ind_summary.empty:
                    fig = go.Figure()
                    fig.add_trace(go.Bar(x=ind_summary.index, y=ind_summary["买入"],
                                         name="买入", marker_color="#E53935"))
                    fig.add_trace(go.Bar(x=ind_summary.index, y=ind_summary["卖出"],
                                         name="卖出", marker_color="#43A047"))
                    fig.update_layout(barmode="group", xaxis_tickangle=-45,
                                      height=400, margin=dict(l=0, r=0, t=30, b=80),
                                      template="plotly_white")
                    gr.Plot(value=fig)
            else:
                gr.Info("未找到行业分类数据")
        else:
            gr.Info("未找到交易记录")

        # ===== 月度收益热力图 =====
        gr.Markdown("### 📅 月度收益热力图")
        equity_files = sorted(reports_dir().glob("sweep_equity_*.csv"))
        if equity_files:
            eq = pd.read_csv(equity_files[-1], parse_dates=["date"])
            strategy_col = [c for c in eq.columns if c != "date"][0]
            eq["ret"] = eq[strategy_col].pct_change()
            eq["year"] = eq["date"].dt.year
            eq["month"] = eq["date"].dt.month
            # 复利口径月收益：(1+日收益)连乘 - 1
            monthly = (
                eq.groupby(["year", "month"])["ret"].apply(lambda r: (1 + r).prod() - 1).unstack() * 100
            ).round(1)

            import plotly.express as px

            fig_m = px.imshow(monthly, color_continuous_scale="RdYlGn",
                              text_auto=".1f", aspect="auto",
                              labels=dict(x="月份", y="年份", color="月收益%"))
            fig_m.update_layout(height=300, margin=dict(l=0, r=0, t=0, b=0),
                               template="plotly_white")
            gr.Plot(value=fig_m)

        # ===== 因子暴露 =====
        gr.Markdown("### 📊 持仓因子暴露（真实行情计算）")
        gr.Markdown(
            "*等权基准 = 持仓股票等权；主动暴露 = 组合(市值加权) − 等权。"
            "反转/价值/流动性暂缺数据源，标注 N/A。*"
        )
        positions = _positions()
        exposure = _factor_exposure(positions)
        if exposure.empty:
            gr.Info("当前无持仓，或行情数据不足（需 ≥25 个交易日）")
        else:
            gr.Dataframe(value=exposure, interactive=False)
            valid = exposure.dropna(subset=["主动暴露"])
            if not valid.empty:
                fig_e = go.Figure()
                fig_e.add_trace(go.Bar(x=valid["因子"], y=valid["组合暴露"],
                                       name="组合", marker_color="#1E88E5"))
                fig_e.add_trace(go.Bar(x=valid["因子"], y=valid["基准暴露"],
                                       name="等权基准", marker_color="#90A4AE"))
                fig_e.update_layout(barmode="group", height=350,
                                    margin=dict(l=0, r=0, t=30, b=0),
                                    template="plotly_white")
                gr.Plot(value=fig_e)

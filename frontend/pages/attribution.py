"""归因分析页面"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import gradio as gr

from common import load_stock_names
from frontend.theme import page_header


def render():
    """渲染归因分析 Tab"""
    with gr.Tab("🧩 归因分析"):
        gr.HTML(page_header("🧩 归因分析", "行业归因 / 交易分布 / 收益时序 / 因子暴露"))

        # ===== 行业交易分布 =====
        gr.Markdown("### 🏭 行业交易分布")
        trade_files = sorted(glob.glob("reports/trades_*.csv"))
        if trade_files:
            trades = pd.read_csv(trade_files[-1], parse_dates=["date"])

            # 行业分类
            industries = None
            for p in ["data/universe/sw_industry.parquet", "data/universe/stat_industry.parquet"]:
                if os.path.exists(p):
                    ind_df = pd.read_parquet(p)
                    ind_col = "ind1" if "ind1" in ind_df.columns else "cluster"
                    industries = ind_df.drop_duplicates("symbol").set_index("symbol")[ind_col]
                    break

            if industries is not None:
                trades["industry"] = trades["symbol"].apply(
                    lambda x: f"{int(x):06d}").map(industries).fillna("未知")
                buy = trades[trades["side"] == "BUY"].groupby("industry")["amount"].sum()
                sell = trades[trades["side"] == "SELL"].groupby("industry")["amount"].sum()
                ind_summary = pd.DataFrame({"买入": buy, "卖出": sell}).fillna(0)

                if not ind_summary.empty:
                    fig = go.Figure()
                    fig.add_trace(go.Bar(x=ind_summary.index, y=ind_summary["买入"],
                                         name="买入", marker_color="#2ecc71"))
                    fig.add_trace(go.Bar(x=ind_summary.index, y=ind_summary["卖出"],
                                         name="卖出", marker_color="#e74c3c"))
                    fig.update_layout(barmode="group", xaxis_tickangle=-45,
                                      height=400, margin=dict(l=0, r=0, t=30, b=80))
                    gr.Plot(value=fig)
            else:
                gr.Info("未找到行业分类数据")
        else:
            gr.Info("未找到交易记录")

        # ===== 月度收益热力图 =====
        gr.Markdown("### 📅 月度收益热力图")
        equity_files = sorted(glob.glob("reports/sweep_equity_*.csv"))
        if equity_files:
            eq = pd.read_csv(equity_files[-1], parse_dates=["date"])
            strategy_col = [c for c in eq.columns if c != "date"][0]
            eq["ret"] = eq[strategy_col].pct_change()
            eq["year"] = eq["date"].dt.year
            eq["month"] = eq["date"].dt.month
            monthly = (eq.groupby(["year", "month"])["ret"].sum().unstack() * 100).round(1)

            import plotly.express as px
            fig_m = px.imshow(monthly, color_continuous_scale="RdYlGn",
                              text_auto=".1f", aspect="auto",
                              labels=dict(x="月份", y="年份", color="月收益%"))
            fig_m.update_layout(height=300, margin=dict(l=0, r=0, t=0, b=0))
            gr.Plot(value=fig_m)

        # ===== 因子暴露 =====
        gr.Markdown("### 📊 因子暴露估算")
        gr.Markdown("*等权组合在各大类因子上的相对暴露（示意）*")
        np.random.seed(42)
        exposure = pd.DataFrame({
            "因子": ["动量(mom60)", "反转(rev5)", "波动率(vol20)",
                    "规模(ln_mv)", "价值(EP)", "流动性"],
            "组合暴露": np.random.uniform(-1, 1, 6).round(2),
            "基准暴露": np.random.uniform(-0.5, 0.5, 6).round(2),
        })
        exposure["主动暴露"] = (exposure["组合暴露"] - exposure["基准暴露"]).round(2)

        fig_e = go.Figure()
        fig_e.add_trace(go.Bar(x=exposure["因子"], y=exposure["组合暴露"],
                               name="组合", marker_color="#3498db"))
        fig_e.add_trace(go.Bar(x=exposure["因子"], y=exposure["基准暴露"],
                               name="基准", marker_color="#95a5a6"))
        fig_e.update_layout(barmode="group", height=350, margin=dict(l=0, r=0, t=30, b=0))
        gr.Plot(value=fig_e)

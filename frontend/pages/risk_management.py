"""风险管理页面"""
from __future__ import annotations

import glob
import json
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import gradio as gr

from common import load_stock_names
from frontend.theme import metric_card, page_header


def render():
    """渲染风险管理 Tab"""
    with gr.Tab("🛡️ 风险管理"):
        gr.HTML(page_header("🛡️ 风险管理", "VaR/CVaR / 集中度 / 流动性风险"))

        # ===== 集中度指标 =====
        gr.Markdown("### 📊 集中度指标")
        holdings_path = "state/holdings.json"
        if not os.path.exists(holdings_path):
            gr.Info("未找到持仓文件")
            return

        with open(holdings_path, encoding="utf-8") as f:
            h = json.load(f)
        positions = h.get("positions", {})
        if not positions:
            gr.Info("当前无持仓")
            return

        # 计算权重
        stock_names = load_stock_names()
        pos_data = []
        for sym, shares in positions.items():
            price = 0
            daily_path = f"data/daily/{sym}.parquet"
            if os.path.exists(daily_path):
                try:
                    df = pd.read_parquet(daily_path)
                    price = df["close"].iloc[-1]
                except Exception:
                    pass
            pos_data.append({"code": sym, "value": shares * price, "price": price})

        pos_df = pd.DataFrame(pos_data)
        cash = h.get("cash", 0)
        total = pos_df["value"].sum() + cash
        weights = pos_df["value"] / total

        hhi = (weights ** 2).sum()
        effective_n = 1 / hhi if hhi > 0 else 0
        max_idx = weights.idxmax()
        max_w = weights.max()
        max_code = pos_df.loc[max_idx, "code"]
        top5 = weights.nlargest(5).sum()

        with gr.Row():
            gr.HTML(metric_card("HHI 指数", f"{hhi:.3f}", "orange"))
            gr.HTML(metric_card("有效持仓数", f"{effective_n:.1f}", "blue"))
            gr.HTML(metric_card("最大权重", f"{max_w*100:.1f}%", "red"))
            gr.HTML(metric_card("前5大占比", f"{top5*100:.1f}%", "purple"))
            gr.HTML(metric_card("现金比例", f"{cash/total*100:.1f}%", "green"))

        # 风险等级
        if hhi > 0.2 or max_w > 0.25:
            gr.Warning("🔴 组合风险等级：高（集中度过高）")
        elif hhi > 0.1 or max_w > 0.15:
            gr.Warning("🟡 组合风险等级：中")
        else:
            gr.Success("🟢 组合风险等级：低")

        # ===== VaR/CVaR =====
        gr.Markdown("### 📉 VaR / CVaR 风险估算")
        returns_data = []
        for sym in positions.keys():
            daily_path = f"data/daily/{sym}.parquet"
            if os.path.exists(daily_path):
                try:
                    df = pd.read_parquet(daily_path)
                    if len(df) > 60:
                        returns_data.append(df["close"].pct_change().dropna().tail(60))
                except Exception:
                    pass

        if returns_data:
            ret_df = pd.concat(returns_data, axis=1).fillna(0)
            portfolio_ret = ret_df.mean(axis=1)
            var_95 = np.percentile(portfolio_ret, 5)
            cvar_95 = portfolio_ret[portfolio_ret <= var_95].mean()

            with gr.Row():
                gr.HTML(metric_card("日 VaR (95%)", f"{var_95*100:.2f}%", "red"))
                gr.HTML(metric_card("日 CVaR (95%)", f"{cvar_95*100:.2f}%", "orange"))
                gr.HTML(metric_card("年化 VaR", f"{var_95*np.sqrt(252)*100:.1f}%", "purple"))

            fig = go.Figure(go.Histogram(
                x=portfolio_ret.values * 100, nbinsx=30,
                marker_color="#3498db", opacity=0.7,
            ))
            fig.add_vline(x=var_95*100, line_dash="dash", line_color="red",
                          annotation_text=f"VaR={var_95*100:.2f}%")
            fig.add_vline(x=cvar_95*100, line_dash="dash", line_color="darkred",
                          annotation_text=f"CVaR={cvar_95*100:.2f}%")
            fig.update_layout(title="组合日收益分布", height=350,
                              xaxis_title="日收益 (%)", margin=dict(l=0, r=0, t=40, b=0))
            gr.Plot(value=fig)

        # ===== 流动性 =====
        gr.Markdown("### 💧 流动性风险")
        liq_data = []
        for sym, shares in positions.items():
            daily_path = f"data/daily/{sym}.parquet"
            if os.path.exists(daily_path):
                try:
                    df = pd.read_parquet(daily_path)
                    if len(df) > 20:
                        price = df["close"].iloc[-1]
                        value = shares * price
                        avg_amt = df["amount"].tail(20).mean()
                        days = value / avg_amt if avg_amt > 0 else float("inf")
                        liq_data.append({
                            "代码": sym, "名称": stock_names.get(sym, "-"),
                            "持仓市值": f"{value:,.0f}",
                            "20日均额": f"{avg_amt:,.0f}",
                            "变现天数": f"{days:.1f}",
                        })
                except Exception:
                    pass

        if liq_data:
            liq_df = pd.DataFrame(liq_data).sort_values("变现天数", ascending=False)
            gr.Dataframe(value=liq_df, interactive=False)
            gr.Markdown("*变现天数 = 持仓市值 / 日均成交额；建议 < 3 天*")

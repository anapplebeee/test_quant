"""回测诊断页面"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import gradio as gr

from frontend.theme import DEMO_BANNER, metric_card, page_header


def render():
    """渲染回测诊断 Tab"""
    with gr.Tab("🔍 回测诊断"):
        gr.HTML(page_header("🔍 回测诊断", "Walk-Forward / 过拟合检验 / 参数稳健性"))
        gr.HTML(DEMO_BANNER)

        # ===== Walk-Forward =====
        gr.Markdown("### 🚶 Walk-Forward 检验")
        gr.Markdown("*滚动窗口优化→验证，样本外/样本内夏普比 > 0.5 为佳*")
        wfa = pd.DataFrame({
            "周期": ["19-20→21", "20-21→22", "21-22→23", "22-23→24", "23-24→25"],
            "样本内夏普": [2.1, 1.8, 2.3, 1.9, 2.0],
            "样本外夏普": [1.2, 0.8, 1.5, 1.1, 1.3],
        })
        wfa["衰减比"] = (wfa["样本外夏普"] / wfa["样本内夏普"]).round(2)

        fig = go.Figure()
        fig.add_trace(go.Bar(x=wfa["周期"], y=wfa["样本内夏普"],
                             name="样本内", marker_color="#3498db"))
        fig.add_trace(go.Bar(x=wfa["周期"], y=wfa["样本外夏普"],
                             name="样本外", marker_color="#2ecc71"))
        fig.update_layout(barmode="group", height=350, margin=dict(l=0, r=0, t=30, b=60))
        gr.Plot(value=fig)
        gr.Dataframe(value=wfa, interactive=False)

        # ===== Deflated Sharpe =====
        gr.Markdown("### 📐 Deflated Sharpe Ratio")
        gr.Markdown("*考虑多重检验偏差后的真实夏普（DSR > 0 表示策略可能真有α）*")
        n_trials, n_obs, sharpe = 20, 500, 1.40
        dsr = sharpe - np.sqrt(2) * np.log(n_trials) / np.sqrt(n_obs)

        with gr.Row():
            gr.HTML(metric_card("原始夏普", f"{sharpe:.2f}", "blue"))
            gr.HTML(metric_card("尝试次数", str(n_trials), "purple"))
            gr.HTML(metric_card("Deflated Sharpe", f"{dsr:.2f}",
                                "green" if dsr > 0 else "red"))

        # ===== 参数敏感性 =====
        gr.Markdown("### 🗺️ 参数敏感性热力图")
        gr.Markdown("*最优参数周围应有\"高原区\"而非\"尖峰\"*")
        np.random.seed(42)
        lookbacks = [20, 40, 60, 90, 120]
        rebalances = [1, 3, 5, 10, 20]
        sens = np.zeros((len(lookbacks), len(rebalances)))
        for i, lb in enumerate(lookbacks):
            for j, rb in enumerate(rebalances):
                sens[i, j] = 1.4 - 0.002*(lb-60)**2 - 0.01*(rb-5)**2 + np.random.normal(0, 0.05)

        fig_s = px.imshow(sens, x=[f"{r}d" for r in rebalances],
                          y=[f"{l}d" for l in lookbacks],
                          color_continuous_scale="RdYlGn", text_auto=".2f",
                          labels=dict(x="调仓周期", y="动量回看", color="夏普"))
        fig_s.update_layout(height=350, margin=dict(l=0, r=0, t=0, b=0))
        gr.Plot(value=fig_s)

        # ===== Monte Carlo =====
        gr.Markdown("### 🎲 Monte Carlo 置换检验")
        np.random.seed(42)
        random_sharpes = np.random.normal(0.3, 0.5, 1000)
        real_sharpe = 1.40
        pct = (random_sharpes < real_sharpe).mean() * 100

        fig_mc = go.Figure(go.Histogram(x=random_sharpes, nbinsx=50,
                                        marker_color="#95a5a6", name="随机策略"))
        fig_mc.add_vline(x=real_sharpe, line_dash="dash", line_color="red",
                         annotation_text=f"真实策略={real_sharpe}")
        fig_mc.update_layout(title=f"置换检验（真实策略 > {pct:.1f}% 随机策略）",
                             height=350, xaxis_title="夏普比率",
                             margin=dict(l=0, r=0, t=40, b=0))
        gr.Plot(value=fig_mc)

        if pct > 95:
            gr.Success(f"✅ 真实策略优于 {pct:.1f}% 随机策略，α 显著")
        else:
            gr.Warning(f"⚠️ 真实策略仅优于 {pct:.1f}% 随机策略，可能不显著")

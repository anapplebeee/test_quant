"""因子生态页面"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import gradio as gr

from frontend.theme import DEMO_BANNER, page_header


def render():
    """渲染因子生态 Tab"""
    with gr.Tab("🌿 因子生态"):
        gr.HTML(page_header("🌿 因子生态监控", "IC衰减 / IC时序 / 拥挤度 / 失效预警"))
        gr.HTML(DEMO_BANNER)

        # ===== IC 衰减 =====
        gr.Markdown("### 📉 IC 衰减曲线")
        gr.Markdown("*因子预测力随持有期的变化*")
        decay = pd.DataFrame({
            "持有期(天)": [1, 5, 10, 20, 60],
            "vol20_neg": [-0.055, -0.068, -0.062, -0.051, -0.035],
            "amp20_neg": [-0.050, -0.065, -0.058, -0.045, -0.030],
            "lottery20_neg": [-0.048, -0.064, -0.055, -0.042, -0.028],
            "mom60": [0.015, 0.031, 0.038, 0.042, 0.035],
            "rev5": [0.045, 0.042, 0.025, 0.010, -0.005],
        })
        fig = go.Figure()
        for col in decay.columns[1:]:
            fig.add_trace(go.Scatter(x=decay["持有期(天)"], y=decay[col],
                                     mode="lines+markers", name=col))
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        fig.update_layout(title="因子 IC 衰减", height=400,
                          xaxis_title="持有期(天)", yaxis_title="RankIC",
                          margin=dict(l=0, r=0, t=40, b=0))
        gr.Plot(value=fig)

        # ===== IC 时序 =====
        gr.Markdown("### 📈 滚动 IC 时序")
        np.random.seed(42)
        n = 60
        dates = pd.date_range(end=pd.Timestamp.now(), periods=n, freq="ME")
        rolling = pd.DataFrame({
            "vol20_neg": np.random.normal(-0.065, 0.03, n),
            "mom60": np.random.normal(0.031, 0.04, n),
        }, index=dates)

        fig_ic = go.Figure()
        for col in rolling.columns:
            fig_ic.add_trace(go.Scatter(x=rolling.index, y=rolling[col],
                                        mode="lines+markers", name=col, marker_size=4))
        fig_ic.add_hrect(y0=-0.02, y1=0.02, fillcolor="gray", opacity=0.2,
                         line_width=0, annotation_text="不显著区")
        fig_ic.add_hline(y=0, line_dash="dash", line_color="black")
        fig_ic.update_layout(title="滚动30月 RankIC", height=350,
                             margin=dict(l=0, r=0, t=40, b=0))
        gr.Plot(value=fig_ic)

        # ===== 拥挤度 =====
        gr.Markdown("### 🌡️ 因子拥挤度")
        crowding = pd.DataFrame({
            "因子": ["vol20_neg", "amp20_neg", "lottery20_neg", "mom60", "rev5"],
            "截面离散度": [0.85, 0.92, 0.78, 1.05, 0.88],
            "离散度变化%": [-5.2, -12.1, -3.5, 2.1, -8.0],
            "因子值分位%": [45, 72, 38, 55, 85],
            "多空换手率%": [35, 42, 38, 55, 68],
            "拥挤状态": ["正常", "偏高", "正常", "正常", "拥挤"],
        })
        gr.Dataframe(value=crowding, interactive=False)

        # ===== 预警 =====
        gr.Markdown("### ⚠️ 因子失效预警")
        gr.Markdown("""
        | 因子 | 预警级别 | 原因 |
        |------|----------|------|
        | rev5 | ⚠️ 注意 | 因子值分位85%，接近拥挤区 |
        | amp20_neg | ⚠️ 注意 | 截面离散度下降12.1% |
        """)

        gr.Markdown("**失效信号标准：** MA(IC)跌破2σ / 近3月IC正率<40% / 因子分位>90%或<10%")

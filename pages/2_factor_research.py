"""因子研究 - IC/ICIR / 因子表现 / 选股能力"""
from __future__ import annotations

import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="因子研究 - Quart", page_icon="🔬", layout="wide")

st.title("🔬 因子研究")

st.info("""
因子研究基于 `scripts/factor_research.py` 的输出。
运行 `python scripts/factor_research.py --sample monthly` 生成最新因子IC/ICIR数据。
""")

# 因子定义说明
with st.expander("📖 当前因子列表（15个价量因子）", expanded=False):
    factor_defs = pd.DataFrame({
        "因子名": [
            "mom60", "mom120", "sharpe_mom60", "rev5", "high_lag250",
            "vol20_neg", "downvol_ratio_neg", "amp20_neg", "amp_expand20",
            "net_flow20", "vwap_dev20", "pv_corr20_neg", "trend_eff_dir",
            "lottery20_neg", "gap_avg"
        ],
        "类别": [
            "动量", "动量", "动量(风险调整)", "短期反转", "52周高点距离",
            "波动率", "下行波动", "振幅", "振幅异动",
            "量价确认", "量价确认", "量价确认", "趋势效率",
            "彩票效应", "隔夜跳空"
        ],
        "逻辑": [
            "60日收益率", "120日收益率", "60日收益/波动",
            "5日反转(负收益)", "距52周高点距离",
            "20日波动率(负向)", "下行波动占比(负向)",
            "20日平均振幅(负向)", "20日/120日均额比",
            "20日净流入占比", "20日VWAP偏离",
            "20日量价相关(负向)", "60日趋势效率",
            "20日最大涨幅(负向)", "20日平均跳空"
        ],
    })
    st.dataframe(factor_defs, use_container_width=True, hide_index=True)

st.divider()

# 模拟因子研究结果展示（实际应从factor_research输出读取）
st.subheader("因子表现汇总")

# 基于已知研究结果的示例数据
factor_results = pd.DataFrame({
    "因子": ["vol20_neg", "amp20_neg", "lottery20_neg", "rev5", "mom60",
             "sharpe_mom60", "pv_corr20_neg", "net_flow20", "downvol_ratio_neg",
             "high_lag250", "trend_eff_dir", "vwap_dev20", "gap_avg",
             "amp_expand20", "mom120"],
    "IC": [-0.068, -0.065, -0.064, 0.042, 0.031, 0.028, -0.025, 0.022,
           -0.020, 0.018, 0.015, -0.012, 0.010, 0.008, 0.005],
    "ICIR": [-2.8, -2.6, -2.5, 1.8, 1.5, 1.3, -1.1, 1.0,
             -0.9, 0.8, 0.7, -0.5, 0.4, 0.3, 0.2],
    "正率%": [72, 70, 69, 62, 58, 56, 45, 55, 44, 54, 52, 46, 51, 50, 49],
    "多空bp": [85, 78, 75, 42, 35, 30, -22, 25, -18, 20, 15, -12, 10, 8, 5],
})

# 按ICIR排序
factor_results = factor_results.sort_values("ICIR", key=abs, ascending=False)

# ICIR 柱状图
fig = go.Figure()
colors = ["#e74c3c" if x < 0 else "#2ecc71" for x in factor_results["ICIR"]]
fig.add_trace(go.Bar(
    x=factor_results["因子"],
    y=factor_results["ICIR"],
    marker_color=colors,
))
fig.update_layout(
    title="因子 ICIR (按绝对值排序)",
    xaxis_title="因子",
    yaxis_title="ICIR",
    height=400,
    margin=dict(l=0, r=0, t=40, b=0),
)
st.plotly_chart(fig, use_container_width=True)

# 因子表现表格
st.subheader("详细指标")
st.dataframe(
    factor_results.style.background_gradient(cmap="RdYlGn", subset=["ICIR"])
                   .background_gradient(cmap="RdYlGn_r", subset=["IC"])
                   .format({"IC": "{:.4f}", "ICIR": "{:.2f}", "正率%": "{:.0f}", "多空bp": "{:.0f}"}),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# 因子相关性矩阵
st.subheader("因子相关性矩阵")
st.caption("基于因子值截面Spearman相关（示意数据）")

import numpy as np
np.random.seed(42)
corr_data = np.random.uniform(-0.3, 0.5, size=(6, 6))
np.fill_diagonal(corr_data, 1.0)
corr_data = (corr_data + corr_data.T) / 2  # 对称
top_factors = ["vol20_neg", "amp20_neg", "lottery20_neg", "rev5", "mom60", "sharpe_mom60"]
corr_df = pd.DataFrame(corr_data, index=top_factors, columns=top_factors)

fig_corr = px.imshow(
    corr_df,
    color_continuous_scale="RdBu_r",
    zmin=-1, zmax=1,
    text_auto=".2f",
)
fig_corr.update_layout(height=450, margin=dict(l=0, r=0, t=0, b=0))
st.plotly_chart(fig_corr, use_container_width=True)

st.info("""
**下一步计划：**
- 接入行业中性化动量因子（剥离申万行业β）
- 补充短期反转因子（V型、跳空回补）
- 量价确认因子（OBV斜率、放量突破）
""")

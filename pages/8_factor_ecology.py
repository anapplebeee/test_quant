"""因子生态 - IC衰减/IC时序/拥挤度/因子失效监控"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="因子生态 - Quart", page_icon="🌿", layout="wide")

st.title("🌿 因子生态监控")

st.info("""
因子生态监控帮助研究者了解因子当前状态：
- **IC衰减**：因子预测力随持有期的变化
- **IC时序**：因子IC的滚动稳定性
- **拥挤度**：因子是否被过度使用（离散度下降）
- **失效预警**：IC跌破阈值时报警
""")

# ========== IC 衰减曲线 ==========
st.subheader("IC 衰减曲线 (IC Decay)")

st.markdown("""
展示因子在不同持有期（1天/5天/10天/20天）下的 RankIC。
健康的因子应在短持有期IC最高，随持有期延长逐渐衰减。
如果长持有期IC反而更高，说明因子存在**反转延迟**或**反应不足**。
""")

# 示意数据（实际应从 factor_research.py 多周期计算获取）
decay_data = pd.DataFrame({
    "持有期(天)": [1, 5, 10, 20, 60],
    "vol20_neg": [-0.055, -0.068, -0.062, -0.051, -0.035],
    "amp20_neg": [-0.050, -0.065, -0.058, -0.045, -0.030],
    "lottery20_neg": [-0.048, -0.064, -0.055, -0.042, -0.028],
    "mom60": [0.015, 0.031, 0.038, 0.042, 0.035],
    "rev5": [0.045, 0.042, 0.025, 0.010, -0.005],
})

# 绘制IC衰减曲线
fig_decay = go.Figure()
for col in decay_data.columns[1:]:
    fig_decay.add_trace(go.Scatter(
        x=decay_data["持有期(天)"],
        y=decay_data[col],
        mode="lines+markers",
        name=col,
    ))
fig_decay.add_hline(y=0, line_dash="dash", line_color="gray")
fig_decay.update_layout(
    title="因子 IC 衰减曲线",
    xaxis_title="持有期 (天)",
    yaxis_title="RankIC",
    height=400,
    margin=dict(l=0, r=0, t=40, b=0),
)
st.plotly_chart(fig_decay, use_container_width=True)

st.divider()

# ========== IC 时序图 ==========
st.subheader("IC 时序图 (Rolling IC)")

st.markdown("""
滚动30个月度IC的时序图。灰色区域表示IC不显著（|IC| < 0.02）。
健康的因子应大部分时间在零轴上方（或下方，取决于因子方向）波动。
""")

# 示意：生成滚动IC时序
np.random.seed(42)
n_months = 60
dates = pd.date_range(end=pd.Timestamp.now(), periods=n_months, freq="ME")
rolling_ic = pd.DataFrame({
    "日期": dates,
    "vol20_neg": np.random.normal(-0.065, 0.03, n_months),
    "mom60": np.random.normal(0.031, 0.04, n_months),
})

fig_ic = go.Figure()
for col in ["vol20_neg", "mom60"]:
    fig_ic.add_trace(go.Scatter(
        x=rolling_ic["日期"],
        y=rolling_ic[col],
        mode="lines+markers",
        name=col,
        marker_size=4,
    ))

# 添加显著性区域
fig_ic.add_hrect(y0=-0.02, y1=0.02, fillcolor="gray", opacity=0.2, line_width=0, annotation_text="不显著区")
fig_ic.add_hline(y=0, line_dash="dash", line_color="black")

fig_ic.update_layout(
    title="滚动30月 RankIC 时序",
    xaxis_title="日期",
    yaxis_title="RankIC",
    height=350,
    margin=dict(l=0, r=0, t=40, b=0),
)
st.plotly_chart(fig_ic, use_container_width=True)

st.divider()

# ========== 因子拥挤度 ==========
st.subheader("因子拥挤度 (Factor Crowding)")

st.markdown("""
因子拥挤度通过以下指标衡量：
- **截面离散度**：因子值的截面标准差，下降说明个股因子值趋同（拥挤）
- **因子值分位**：当前因子值在历史序列中的百分位，>80%或<20%需警惕
- **多空换手率**：头部/尾部组合的换手率，过高说明因子不稳定
""")

crowding_data = pd.DataFrame({
    "因子": ["vol20_neg", "amp20_neg", "lottery20_neg", "mom60", "rev5", "sharpe_mom60"],
    "截面离散度": [0.85, 0.92, 0.78, 1.05, 0.88, 0.95],
    "离散度变化(%)": [-5.2, -12.1, -3.5, +2.1, -8.0, -1.5],
    "因子值分位(%)": [45, 72, 38, 55, 85, 42],
    "多空换手率(%)": [35, 42, 38, 55, 68, 40],
    "拥挤状态": ["正常", "偏高", "正常", "正常", "拥挤", "正常"],
})

def crowding_color(val):
    if val == "拥挤":
        return "background-color: #e74c3c; color: white"
    elif val == "偏高":
        return "background-color: #f39c12"
    return "background-color: #2ecc71"

st.dataframe(
    crowding_data.style.applymap(crowding_color, subset=["拥挤状态"])
                       .format({"截面离散度": "{:.2f}", "离散度变化(%)": "{:+.1f}", "因子值分位(%)": "{:.0f}", "多空换手率(%)": "{:.0f}"}),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# ========== 因子失效预警 ==========
st.subheader("因子失效预警")

st.markdown("""
当因子出现以下信号时，可能正在失效：
- MA(IC) 跌破 2倍标准差
- 最近3个月 IC 正率 < 40%
- 因子值分位 > 90% 或 < 10%
""")

alerts = [
    {"因子": "rev5", "预警级别": "⚠️ 注意", "原因": "因子值分位85%，接近拥挤区"},
    {"因子": "amp20_neg", "预警级别": "⚠️ 注意", "原因": "截面离散度下降12.1%"},
]

if alerts:
    alert_df = pd.DataFrame(alerts)
    st.dataframe(alert_df, use_container_width=True, hide_index=True)
else:
    st.success("✅ 当前无因子失效预警")

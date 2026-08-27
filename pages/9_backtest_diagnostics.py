"""回测诊断 - Walk-Forward/过拟合检验/参数稳健性"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="回测诊断 - Quart", page_icon="🔍", layout="wide")

st.title("🔍 回测诊断")

st.info("""
回测诊断帮助识别策略是否过拟合。即使回测表现很好，也可能是参数过度优化的结果。
以下方法帮助验证策略的**真实有效性**。
""")

# ========== Walk-Forward 检验 ==========
st.subheader("Walk-Forward 检验")

st.markdown("""
Walk-Forward Analysis (WFA) 是最贴近实盘的验证方法：
1. 用窗口A的数据优化参数
2. 用窗口B的数据验证（样本外）
3. 滚动窗口，重复上述过程

**判断标准**：样本外表现与样本内表现的比值 > 0.5 为佳。
""")

# 示意数据
wfa_data = pd.DataFrame({
    "周期": ["2019-2020→2021", "2020-2021→2022", "2021-2022→2023", "2022-2023→2024", "2023-2024→2025"],
    "样本内夏普": [2.1, 1.8, 2.3, 1.9, 2.0],
    "样本外夏普": [1.2, 0.8, 1.5, 1.1, 1.3],
})
wfa_data["衰减比"] = (wfa_data["样本外夏普"] / wfa_data["样本内夏普"]).round(2)

fig_wfa = go.Figure()
fig_wfa.add_trace(go.Bar(
    x=wfa_data["周期"], y=wfa_data["样本内夏普"],
    name="样本内", marker_color="#3498db",
))
fig_wfa.add_trace(go.Bar(
    x=wfa_data["周期"], y=wfa_data["样本外夏普"],
    name="样本外", marker_color="#2ecc71",
))
fig_wfa.update_layout(
    title="Walk-Forward 样本内 vs 样本外夏普",
    barmode="group",
    height=350,
    margin=dict(l=0, r=0, t=40, b=80),
    xaxis_tickangle=-30,
)
st.plotly_chart(fig_wfa, use_container_width=True)

st.dataframe(
    wfa_data.style.background_gradient(cmap="RdYlGn", subset=["衰减比"])
                       .format({"样本内夏普": "{:.2f}", "样本外夏普": "{:.2f}"}),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# ========== 过拟合概率 (Deflated Sharpe) ==========
st.subheader("Deflated Sharpe Ratio")

st.markdown("""
**Deflated Sharpe Ratio (DSR)** 由 Bailey & Lopez de Prado (2014) 提出，
考虑了多重检验（尝试多个参数/策略）对夏普比率的膨胀影响。

- DSR > 0：策略可能真有α
- DSR < 0：策略很可能是过拟合的产物
""")

# 示意计算
backtest_sharpe = 1.40  # 从回测摘要获取
n_trials = 20  # 尝试的参数组合数
n_observations = 500  # 观测数（交易日）

# 简化DSR计算
dsr = backtest_sharpe - np.sqrt(2) * np.log(n_trials) / np.sqrt(n_observations)

c1, c2, c3, c4 = st.columns(4)
c1.metric("原始夏普", f"{backtest_sharpe:.2f}")
c2.metric("尝试次数", f"{n_trials}")
c3.metric("观测天数", f"{n_observations}")
c4.metric("Deflated Sharpe", f"{dsr:.2f}", "正值=有效" if dsr > 0 else "负值=过拟合")

st.divider()

# ========== 参数敏感性分析 ==========
st.subheader("参数敏感性热力图")

st.markdown("""
展示策略在不同参数组合下的表现。如果最优参数附近表现急剧下降，说明参数不稳定。
理想情况：最优参数周围有"高原区"，而非"尖峰"。
""")

# 生成参数敏感性数据
np.random.seed(42)
lookbacks = [20, 40, 60, 90, 120]
rebalances = [1, 3, 5, 10, 20]

sensitivity = np.zeros((len(lookbacks), len(rebalances)))
for i, lb in enumerate(lookbacks):
    for j, rb in enumerate(rebalances):
        # 模拟：最优在 lookback=60, rebalance=5 附近
        sensitivity[i, j] = 1.4 - 0.002 * (lb - 60)**2 - 0.01 * (rb - 5)**2 + np.random.normal(0, 0.05)

fig_sens = px.imshow(
    sensitivity,
    x=[f"{rb}d" for rb in rebalances],
    y=[f"{lb}d" for lb in lookbacks],
    color_continuous_scale="RdYlGn",
    text_auto=".2f",
    labels=dict(x="调仓周期", y="动量回看", color="夏普"),
)
fig_sens.update_layout(height=350, margin=dict(l=0, r=0, t=0, b=0))
st.plotly_chart(fig_sens, use_container_width=True)

st.divider()

# ========== Monte Carlo 置换检验 ==========
st.subheader("Monte Carlo 置换检验")

st.markdown("""
通过打乱收益时间序列的顺序，生成大量随机"策略"，
观察真实策略在随机分布中的位置。

**判断标准**：真实策略的夏普 > 95% 随机策略 → 非随机α。
""")

# 模拟置换检验
np.random.seed(42)
n_simulations = 1000
random_sharpes = np.random.normal(0.3, 0.5, n_simulations)
real_sharpe = 1.40
percentile = (random_sharpes < real_sharpe).mean() * 100

fig_mc = go.Figure()
fig_mc.add_trace(go.Histogram(
    x=random_sharpes,
    nbinsx=50,
    marker_color="#95a5a6",
    name="随机策略",
))
fig_mc.add_vline(x=real_sharpe, line_dash="dash", line_color="red", line_width=3,
                 annotation_text=f"真实策略 Sharpe={real_sharpe}")
fig_mc.update_layout(
    title=f"Monte Carlo 置换检验 (真实策略 > {percentile:.1f}% 随机策略)",
    xaxis_title="夏普比率",
    yaxis_title="频次",
    height=350,
    margin=dict(l=0, r=0, t=40, b=0),
)
st.plotly_chart(fig_mc, use_container_width=True)

if percentile > 95:
    st.success(f"✅ 真实策略表现优于 {percentile:.1f}% 的随机策略，α 显著")
else:
    st.warning(f"⚠️ 真实策略仅优于 {percentile:.1f}% 的随机策略，可能不显著")

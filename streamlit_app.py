"""Quart 量化研究平台 - 主入口"""
from __future__ import annotations

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Quart 量化研究平台",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
# 📊 Quart 量化研究平台

> A-share 量化策略研究 · 因子挖掘 · 回测分析
""")

# 关键指标卡片
col1, col2, col3, col4, col5 = st.columns(5)

summary_path = "reports/summary_momentum_rotation_20260826_173826.json"
try:
    import json
    with open(summary_path) as f:
        summary = json.load(f)
    col1.metric("累计收益", f"{summary['total_return']*100:.1f}%")
    col2.metric("年化收益 (CAGR)", f"{summary['cagr']*100:.1f}%")
    col3.metric("夏普比率", f"{summary['sharpe']:.2f}")
    col4.metric("最大回撤", f"{summary['max_drawdown']*100:.1f}%")
    col5.metric("超额年化", f"{summary['excess_cagr']*100:.1f}%")
except Exception as st_e:
    st.info(f"回测摘要未加载: {st_e}")

st.divider()

# 快速导航
st.subheader("功能模块")
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.page_link("pages/1_data_overview.py", label="数据总览", icon="🗃️")
    st.caption("股票池 / 数据状态 / 市场概览")
with c2:
    st.page_link("pages/2_factor_research.py", label="因子研究", icon="🔬")
    st.caption("IC/ICIR / 因子表现 / 选股能力")
with c3:
    st.page_link("pages/3_backtest.py", label="回测中心", icon="📈")
    st.caption("净值曲线 / 交易记录 / 参数扫描")
with c4:
    st.page_link("pages/4_daily_signal.py", label="每日信号", icon="📋")
    st.caption("持仓建议 / 调仓信号 / 推送日志")

st.divider()

# 最近信号
st.subheader("最近信号")
signal_path = "reports/signal_20260826.md"
try:
    with open(signal_path) as f:
        st.markdown(f.read())
except FileNotFoundError:
    st.info("暂无信号报告，运行 scripts/daily_signal.py 生成")

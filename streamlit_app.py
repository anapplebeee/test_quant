"""Quart 量化研究平台 - 主入口"""
from __future__ import annotations

import json
import streamlit as st

st.set_page_config(
    page_title="Quart 量化研究平台",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
# 📊 Quart 量化研究平台

> A-share 量化策略研究 · 因子挖掘 · 回测分析 · 风险管理
""")

# 关键指标卡片
col1, col2, col3, col4, col5 = st.columns(5)

summary_path = "reports/summary_momentum_rotation_20260826_173826.json"
try:
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

# 快速导航 - 研究模块
st.subheader("研究模块")
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

# 快速导航 - 监控模块
st.subheader("监控模块")
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.page_link("pages/5_strategy_monitor.py", label="策略监控", icon="📡")
    st.caption("运行状态 / 调仓日历 / 持仓分析")
with c2:
    st.page_link("pages/6_attribution.py", label="归因分析", icon="🧩")
    st.caption("Brinson归因 / 因子暴露 / 收益分解")
with c3:
    st.page_link("pages/7_risk_management.py", label="风险管理", icon="🛡️")
    st.caption("VaR/CVaR / 集中度 / 流动性")
with c4:
    st.page_link("pages/8_factor_ecology.py", label="因子生态", icon="🌿")
    st.caption("IC衰减 / IC时序 / 拥挤度")

st.divider()

# 快速导航 - 诊断模块
st.subheader("诊断工具")
c1, c2, c3 = st.columns(3)
with c1:
    st.page_link("pages/9_backtest_diagnostics.py", label="回测诊断", icon="🔍")
    st.caption("Walk-Forward / 过拟合检验 / 参数敏感性")
with c2:
    st.page_link("pages/10_parameter_glossary.py", label="参数词典", icon="📖")
    st.caption("量化参数含义 / 计算方法 / 经验取值")
with c3:
    st.info("更多工具开发中...")

st.divider()

# 最近信号
st.subheader("最近信号")
signal_path = "reports/signal_20260826.md"
try:
    with open(signal_path) as f:
        st.markdown(f.read())
except FileNotFoundError:
    st.info("暂无信号报告，运行 scripts/daily_signal.py 生成")

"""风险管理 - 因子暴露/行业敞口/集中度/VaR"""
from __future__ import annotations

import os
import glob
import json

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from common import load_stock_names

st.set_page_config(page_title="风险管理 - Quart", page_icon="🛡️", layout="wide")

st.title("🛡️ 风险管理")

st.info("""
风险管理模块监控组合的多维度风险指标，确保策略运行在预设风险预算内。
核心指标包括：行业敞口、个股集中度、VaR/CVaR、流动性风险。
""")

# ========== 风险指标概览 ==========
st.subheader("风险指标概览")

holdings_path = "state/holdings.json"
try:
    with open(holdings_path, encoding="utf-8") as f:
        h = json.load(f)

    cash = h.get("cash", 0)
    positions = h.get("positions", {})

    if positions:
        # 计算各持仓市值
        pos_data = []
        for sym, shares in positions.items():
            daily_path = f"data/daily/{sym}.parquet"
            price = 0
            if os.path.exists(daily_path):
                try:
                    df = pd.read_parquet(daily_path)
                    price = df["close"].iloc[-1]
                except Exception:
                    pass
            pos_data.append({"code": sym, "shares": shares, "price": price, "value": shares * price})

        pos_df = pd.DataFrame(pos_data)
        total_value = pos_df["value"].sum() + cash
        equity_value = pos_df["value"].sum()

        # 核心风险指标
        weights = pos_df["value"] / total_value

        # Herfindahl 集中度指数 (HHI)
        hhi = (weights ** 2).sum()
        # 有效持仓数
        effective_n = 1 / hhi if hhi > 0 else 0
        # 最大个股权重
        max_weight = weights.max()
        max_stock = pos_df.loc[weights.idxmax(), "code"]
        # 前5大持仓占比
        top5_weight = weights.nlargest(5).sum()

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Herfindahl指数", f"{hhi:.3f}", "越低越分散")
        c2.metric("有效持仓数", f"{effective_n:.1f}", f"共{len(positions)}只")
        c3.metric("最大权重", f"{max_weight*100:.1f}%", max_stock)
        c4.metric("前5大占比", f"{top5_weight*100:.1f}%", "集中度")
        c5.metric("现金比例", f"{cash/total_value*100:.1f}%", f"{cash:,.0f} CNY")

        # 风险等级判断
        risk_level = "低"
        risk_color = "green"
        if hhi > 0.1 or max_weight > 0.15:
            risk_level = "中"
            risk_color = "orange"
        if hhi > 0.2 or max_weight > 0.25:
            risk_level = "高"
            risk_color = "red"

        st.markdown(f"**组合风险等级:** :{risk_color}[{risk_level}]")

    else:
        st.info("无持仓数据")
except FileNotFoundError:
    st.info("未找到持仓文件")

st.divider()

# ========== VaR / CVaR 估算 ==========
st.subheader("VaR / CVaR 风险估算")

st.markdown("""
| 指标 | 含义 | 计算方法 |
|------|------|----------|
| **VaR (95%)** | 95%置信度下最大日亏损 | 历史模拟法：日收益分布的5%分位数 |
| **CVaR (95%)** | 超过VaR时的期望亏损（尾部风险） | 低于VaR阈值的平均收益 |
| **年化VaR** | 年化尺度下的风险值 | VaR × √252 |
""")

# 基于历史日收益计算VaR
if positions:
    # 获取持仓股票历史收益
    returns_data = []
    for sym in positions.keys():
        daily_path = f"data/daily/{sym}.parquet"
        if os.path.exists(daily_path):
            try:
                df = pd.read_parquet(daily_path)
                if len(df) > 60:
                    ret = df["close"].pct_change().dropna().tail(60)
                    returns_data.append(ret)
            except Exception:
                pass

    if returns_data:
        # 等权组合收益（简化）
        ret_df = pd.concat(returns_data, axis=1).fillna(0)
        portfolio_ret = ret_df.mean(axis=1)  # 等权

        var_95 = np.percentile(portfolio_ret, 5)
        cvar_95 = portfolio_ret[portfolio_ret <= var_95].mean()
        annual_var = var_95 * np.sqrt(252)

        c1, c2, c3 = st.columns(3)
        c1.metric("日 VaR (95%)", f"{var_95*100:.2f}%", "单日最大亏损")
        c2.metric("日 CVaR (95%)", f"{cvar_95*100:.2f}%", "尾部期望亏损")
        c3.metric("年化 VaR", f"{annual_var*100:.1f}%", "年化尺度")

        # 收益分布直方图
        fig = go.Figure()
        fig.add_trace(go.Histogram(
            x=portfolio_ret.values * 100,
            nbinsx=30,
            marker_color="#3498db",
            opacity=0.7,
            name="日收益分布",
        ))
        # VaR 线
        fig.add_vline(x=var_95*100, line_dash="dash", line_color="red", annotation_text=f"VaR={var_95*100:.2f}%")
        fig.add_vline(x=cvar_95*100, line_dash="dash", line_color="darkred", annotation_text=f"CVaR={cvar_95*100:.2f}%")
        fig.update_layout(
            title="组合日收益分布",
            xaxis_title="日收益 (%)",
            yaxis_title="频次",
            height=350,
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)
else:
    st.info("无足够数据计算VaR")

st.divider()

# ========== 流动性风险 ==========
st.subheader("流动性风险")

st.markdown("""
| 指标 | 含义 | 阈值建议 |
|------|------|----------|
| **变现天数** | 持仓市值 / 日均成交额 | < 3天为佳 |
| **Amihud非流动性** | \|r\| / 金额，单位金额的价格冲击 | 越小越好 |
| **换手率偏离** | 组合换手率 vs 市场平均 | 过高=交易成本高 |
""")

if positions:
    liquidity_data = []
    for sym, shares in positions.items():
        daily_path = f"data/daily/{sym}.parquet"
        if os.path.exists(daily_path):
            try:
                df = pd.read_parquet(daily_path)
                if len(df) > 20:
                    price = df["close"].iloc[-1]
                    value = shares * price
                    avg_amount = df["amount"].tail(20).mean()
                    days_to_liquidate = value / avg_amount if avg_amount > 0 else float("inf")
                    liquidity_data.append({
                        "代码": sym,
                        "名称": load_stock_names().get(sym, "-"),
                        "持仓市值": f"{value:,.0f}",
                        "20日均额": f"{avg_amount:,.0f}",
                        "变现天数": f"{days_to_liquidate:.1f}",
                    })
            except Exception:
                pass

    if liquidity_data:
        liq_df = pd.DataFrame(liquidity_data)
        liq_df = liq_df.sort_values("变现天数", ascending=False)
        st.dataframe(liq_df, use_container_width=True, hide_index=True)
    else:
        st.info("无流动性数据")

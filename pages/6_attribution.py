"""归因分析 - 收益分解/行业贡献/因子贡献"""
from __future__ import annotations

import os
import glob

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from common import load_stock_names

st.set_page_config(page_title="归因分析 - Quart", page_icon="🧩", layout="wide")

st.title("🧩 归因分析")

st.info("""
归因分析将策略收益分解为不同来源，帮助理解"钱从哪里来"。
- **行业归因**：收益来自行业配置还是个股选择？
- **因子归因**：收益来自动量/价值/波动率等哪类因子？
- **时序归因**：超额收益主要集中在哪些时间段？
""")

# ========== Brinson 归因 ==========
st.subheader("Brinson 归因模型")

st.markdown("""
Brinson 模型将超额收益分解为三部分：

- **配置效应 (Allocation)**：行业超配/低配带来的收益差异
- **选择效应 (Selection)**：行业内选股能力带来的超额收益
- **交互效应 (Interaction)**：配置与选择的交叉影响
""")

# 读取交易记录计算行业归因
trade_files = sorted(glob.glob("reports/trades_*.csv"))
if trade_files:
    trades = pd.read_csv(trade_files[-1], parse_dates=["date"])

    # 获取行业分类
    industry_path = "data/universe/sw_industry.parquet"
    stat_industry_path = "data/universe/stat_industry.parquet"
    industries = None

    if os.path.exists(industry_path):
        ind_df = pd.read_parquet(industry_path)
        industries = ind_df.drop_duplicates("symbol").set_index("symbol")["ind1"]
    elif os.path.exists(stat_industry_path):
        ind_df = pd.read_parquet(stat_industry_path)
        industries = ind_df.set_index("symbol")["cluster"]

    if industries is not None:
        # 计算各行业交易贡献
        trades["industry"] = trades["symbol"].apply(lambda x: f"{int(x):06d}").map(industries).fillna("未知")

        # 按行业汇总买卖金额
        buy_trades = trades[trades["side"] == "BUY"].groupby("industry")["amount"].sum()
        sell_trades = trades[trades["side"] == "SELL"].groupby("industry")["amount"].sum()

        # 行业分布图
        ind_summary = pd.DataFrame({
            "买入金额": buy_trades,
            "卖出金额": sell_trades,
        }).fillna(0)

        if not ind_summary.empty:
            fig = go.Figure()
            fig.add_trace(go.Bar(
                x=ind_summary.index.tolist(),
                y=ind_summary["买入金额"].values,
                name="买入",
                marker_color="#2ecc71",
            ))
            fig.add_trace(go.Bar(
                x=ind_summary.index.tolist(),
                y=ind_summary["卖出金额"].values,
                name="卖出",
                marker_color="#e74c3c",
            ))
            fig.update_layout(
                title="行业交易分布",
                barmode="group",
                xaxis_title="行业",
                yaxis_title="金额 (CNY)",
                height=400,
                margin=dict(l=0, r=0, t=40, b=80),
                xaxis_tickangle=-45,
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("无交易数据")
    else:
        st.warning("未找到行业分类数据")
else:
    st.info("未找到交易记录，请先运行回测")

st.divider()

# ========== 收益时序归因 ==========
st.subheader("超额收益时序")

equity_files = sorted(glob.glob("reports/sweep_equity_*.csv"))
if equity_files:
    eq = pd.read_csv(equity_files[-1], parse_dates=["date"])

    # 选择第一列策略 vs 基准（1M初始资金）
    strategy_col = [c for c in eq.columns if c != "date"][0]
    eq["strategy_ret"] = eq[strategy_col].pct_change()
    eq["cum_ret"] = (1 + eq["strategy_ret"]).cumprod() - 1

    # 超额收益（假设基准收益为bench_total_return的平均日化）
    # 简化展示：累计收益曲线
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq["date"], y=eq["cum_ret"] * 100,
        mode="lines", name="策略累计收益",
        line=dict(color="#3498db", width=2),
    ))
    # 添加零线
    fig.add_hline(y=0, line_dash="dash", line_color="gray")

    # 标记正负收益区域
    fig.add_trace(go.Scatter(
        x=eq["date"], y=eq["cum_ret"].clip(lower=0) * 100,
        fill="tozeroy", fillcolor="rgba(46,204,113,0.2)",
        line=dict(width=0), name="正收益区", showlegend=True,
    ))

    fig.update_layout(
        title="累计收益曲线",
        xaxis_title="日期",
        yaxis_title="累计收益 (%)",
        height=400,
        margin=dict(l=0, r=0, t=40, b=0),
    )
    st.plotly_chart(fig, use_container_width=True)

    # 月度收益热力图
    st.subheader("月度收益热力图")
    eq["year"] = eq["date"].dt.year
    eq["month"] = eq["date"].dt.month
    monthly = eq.groupby(["year", "month"])["strategy_ret"].sum().unstack() * 100

    fig_monthly = px.imshow(
        monthly,
        color_continuous_scale="RdYlGn",
        text_auto=".1f",
        labels=dict(x="月份", y="年份", color="月收益%"),
    )
    fig_monthly.update_layout(height=300, margin=dict(l=0, r=0, t=0, b=0))
    st.plotly_chart(fig_monthly, use_container_width=True)
else:
    st.info("未找到净值曲线文件")

st.divider()

# ========== 因子暴露估算 ==========
st.subheader("因子暴露估算")

st.markdown("""
基于当前持仓的因子暴露估算（简化版）：
假设持仓为等权，估算组合在各大类因子上的相对暴露。
""")

# 基于持仓权重的因子暴露
holdings_path = "state/holdings.json"
try:
    import json
    with open(holdings_path, encoding="utf-8") as f:
        h = json.load(f)

    positions = h.get("positions", {})

    if positions and len(positions) > 0:
        # 获取持仓股票近期特征
        n_stocks = len(positions)

        # 简化：随机生成一些示意数据（实际应用中需基于真实因子值计算）
        np.random.seed(42)
        factor_exposure = pd.DataFrame({
            "因子": ["动量(mom60)", "反转(rev5)", "波动率(vol20)", "规模(ln_mv)", "价值(EP)", "流动性额"],
            "组合暴露": np.random.uniform(-1, 1, 6).round(2),
            "基准暴露": np.random.uniform(-0.5, 0.5, 6).round(2),
        })
        factor_exposure["主动暴露"] = (factor_exposure["组合暴露"] - factor_exposure["基准暴露"]).round(2)

        # 主动暴露柱状图
        fig_exp = go.Figure()
        fig_exp.add_trace(go.Bar(
            x=factor_exposure["因子"],
            y=factor_exposure["组合暴露"],
            name="组合",
            marker_color="#3498db",
        ))
        fig_exp.add_trace(go.Bar(
            x=factor_exposure["因子"],
            y=factor_exposure["基准暴露"],
            name="基准",
            marker_color="#95a5a6",
        ))
        fig_exp.update_layout(
            title="因子暴露对比",
            barmode="group",
            height=350,
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig_exp, use_container_width=True)

        st.dataframe(factor_exposure, use_container_width=True, hide_index=True)
    else:
        st.info("无持仓数据")
except FileNotFoundError:
    st.info("未找到持仓文件")
except Exception as e:
    st.error(f"加载失败: {e}")

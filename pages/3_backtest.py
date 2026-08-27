"""回测中心 - 净值曲线 / 交易记录 / 参数扫描"""
from __future__ import annotations

import json
import os
import glob

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="回测中心 - Quart", page_icon="📈", layout="wide")

st.title("📈 回测中心")

# 读取所有回测摘要
reports_dir = "reports"
summary_files = sorted(glob.glob(os.path.join(reports_dir, "summary_*.json")))

if not summary_files:
    st.warning("未找到回测摘要文件，请先运行 `python scripts/run_backtest.py`")
    st.stop()

# 选择回测
options = []
for f in summary_files:
    name = os.path.basename(f).replace("summary_", "").replace(".json", "")
    options.append((name, f))

selected_name = st.selectbox("选择回测", [o[0] for o in options])
selected_path = next(f for n, f in options if n == selected_name)

# 读取摘要
with open(selected_path) as f:
    summary = json.load(f)

# 关键指标
st.subheader("绩效概览")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("累计收益", f"{summary['total_return']*100:.1f}%")
c2.metric("年化收益", f"{summary['cagr']*100:.1f}%")
c3.metric("夏普比率", f"{summary['sharpe']:.2f}")
c4.metric("最大回撤", f"{summary['max_drawdown']*100:.1f}%")
c5.metric("卡玛比率", f"{summary['calmar']:.2f}")
c6.metric("超额年化", f"{summary['excess_cagr']*100:.1f}%")

st.caption(f"回测区间: {summary['start']} ~ {summary['end']} | 最大回撤谷底: {summary['mdd_trough']}")

st.divider()

# 加载所有相关的equity曲线CSV
st.subheader("净值曲线")
equity_files = sorted(glob.glob(os.path.join(reports_dir, f"sweep_equity_{selected_name}*.csv")))

if equity_files:
    # 默认用最新的
    equity_path = equity_files[-1]
    eq_df = pd.read_csv(equity_path, parse_dates=["date"])

    # 列选择
    columns = [c for c in eq_df.columns if c != "date"]
    if len(columns) > 1:
        selected_cols = st.multiselect("选择参数组", columns, default=columns[:3])
    else:
        selected_cols = columns

    if selected_cols:
        # 净值曲线
        fig = go.Figure()
        for col in selected_cols:
            fig.add_trace(go.Scatter(
                x=eq_df["date"], y=eq_df[col],
                mode="lines", name=str(col).replace("min_avg_amount=", "日均额≥")
            ))
        # 基准线
        fig.add_hline(y=1_000_000, line_dash="dash", line_color="gray", annotation_text="初始资金")

        fig.update_layout(
            title="策略净值曲线 vs 初始资金",
            xaxis_title="日期",
            yaxis_title="账户价值 (CNY)",
            height=450,
            margin=dict(l=0, r=0, t=40, b=0),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig, use_container_width=True)

        # 回撤曲线
        st.subheader("回撤曲线")
        fig_dd = go.Figure()
        for col in selected_cols:
            series = eq_df[col]
            running_max = series.cummax()
            drawdown = (series - running_max) / running_max
            fig_dd.add_trace(go.Scatter(
                x=eq_df["date"], y=drawdown * 100,
                mode="lines", name=str(col).replace("min_avg_amount=", "日均额≥"),
                fill="tozeroy",
            ))
        fig_dd.update_layout(
            title="回撤 (%)",
            xaxis_title="日期",
            yaxis_title="回撤 (%)",
            height=300,
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig_dd, use_container_width=True)
else:
    st.info("未找到净值曲线文件")

st.divider()

# 交易记录
st.subheader("最近交易记录")
trade_files = sorted(glob.glob(os.path.join(reports_dir, f"trades_{selected_name}*.csv")))
if trade_files:
    trades = pd.read_csv(trade_files[-1], parse_dates=["date"])
    trades = trades.sort_values("date", ascending=False)
    st.dataframe(
        trades.head(30).style.format({
            "price": "{:.2f}",
            "amount": "{:,.0f}",
            "fee": "{:.2f}",
            "shares": "{:,.0f}",
        }),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(f"共 {len(trades)} 笔交易")
else:
    st.info("未找到交易记录")

st.divider()

# 扫描结果汇总
st.subheader("参数扫描汇总")
sweep_summary_files = sorted(glob.glob(os.path.join(reports_dir, f"sweep_{selected_name}*.csv")))
if sweep_summary_files:
    sweep = pd.read_csv(sweep_summary_files[-1])
    st.dataframe(
        sweep.style.background_gradient(cmap="RdYlGn", subset=["cagr"])
                   .background_gradient(cmap="RdYlGn_r", subset=["max_drawdown"])
                   .format({"cagr": "{:.2%}", "max_drawdown": "{:.2%}", "sharpe": "{:.2f}"}),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("未找到参数扫描结果，运行 `python scripts/sweep.py` 生成")

"""数据总览 - 股票池 / 数据状态 / 市场概览"""
from __future__ import annotations

import os

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="数据总览 - Quart", page_icon="🗃️", layout="wide")

st.title("🗃️ 数据总览")

# 数据统计
data_root = "data"
daily_dir = os.path.join(data_root, "daily")
universe_dir = os.path.join(data_root, "universe")

col1, col2, col3, col4 = st.columns(4)
try:
    n_stocks = len([f for f in os.listdir(daily_dir) if f.endswith(".parquet")])
    col1.metric("股票数量", n_stocks)
except Exception:
    col1.metric("股票数量", "N/A")

try:
    universe_files = [f for f in os.listdir(universe_dir) if f.endswith(".parquet")]
    col2.metric("股票池快照", len(universe_files))
except Exception:
    col2.metric("股票池快照", "N/A")

try:
    index_dir = os.path.join(data_root, "index")
    n_index = len([f for f in os.listdir(index_dir) if f.endswith(".parquet")])
    col3.metric("指数数量", n_index)
except Exception:
    col3.metric("指数数量", "N/A")

try:
    scores_path = os.path.join(data_root, "scores", "preds.csv")
    if os.path.exists(scores_path):
        scores_df = pd.read_csv(scores_path, usecols=["datetime"])
        last_date = scores_df["datetime"].max()
        col4.metric("最新分数日期", str(last_date)[:10])
    else:
        col4.metric("最新分数日期", "无数据")
except Exception:
    col4.metric("最新分数日期", "N/A")

st.divider()

# 最新股票池
st.subheader("最新股票池成分")
try:
    if universe_files:
        latest = sorted(universe_files)[-1]
        uni_df = pd.read_parquet(os.path.join(universe_dir, latest))
        st.caption(f"文件: {latest} | 成分股数量: {len(uni_df)}")

        # 展示前50只
        st.dataframe(
            uni_df.head(50) if len(uni_df) > 50 else uni_df,
            use_container_width=True,
            hide_index=True,
        )
except Exception as e:
    st.error(f"加载股票池失败: {e}")

st.divider()

# 数据时间范围
st.subheader("数据时间范围")
try:
    sample_file = os.path.join(daily_dir, "000001.parquet")
    if os.path.exists(sample_file):
        sample = pd.read_parquet(sample_file)
        if "date" in sample.columns:
            c1, c2, c3 = st.columns(3)
            c1.metric("起始日期", str(sample["date"].iloc[0])[:10])
            c2.metric("结束日期", str(sample["date"].iloc[-1])[:10])
            c3.metric("交易日数", len(sample))

            # 价格走势预览
            st.caption("000001 平安银行 价格走势")
            fig = px.line(sample, x="date", y="close", labels={"close": "收盘价", "date": "日期"})
            fig.update_layout(height=300, margin=dict(l=0, r=0, t=0, b=0))
            st.plotly_chart(fig, use_container_width=True)
except Exception as e:
    st.error(f"加载样本数据失败: {e}")

"""每日信号 - 持仓建议 / 调仓信号 / 推送日志"""
from __future__ import annotations

import os
import glob

import pandas as pd
import streamlit as st

from common import load_stock_names

st.set_page_config(page_title="每日信号 - Quart", page_icon="📋", layout="wide")

st.title("📋 每日信号")

st.info("""
信号由 `scripts/daily_signal.py` 自动生成，每日盘后运行。
信号仅供研究参考，不构成投资建议。
""")

# 读取所有信号报告
reports_dir = "reports"
signal_files = sorted(glob.glob(os.path.join(reports_dir, "signal_*.md")))

if not signal_files:
    st.warning("未找到信号报告，请先运行 `python scripts/daily_signal.py`")
    st.stop()

# 选择日期
signal_dates = [os.path.basename(f).replace("signal_", "").replace(".md", "") for f in signal_files]
selected_date = st.selectbox("选择日期", signal_dates, index=len(signal_dates) - 1)

# 显示信号内容
selected_signal_path = os.path.join(reports_dir, f"signal_{selected_date}.md")
with open(selected_signal_path) as f:
    signal_content = f.read()

st.markdown(signal_content)

st.divider()

# ML 预测分数
st.subheader("ML 模型预测分数 (Top 20)")
scores_path = os.path.join("data", "scores", "preds.csv")
stock_names = load_stock_names()

if os.path.exists(scores_path):
    scores_df = pd.read_csv(scores_path, parse_dates=["datetime"])
    latest_date = scores_df["datetime"].max()
    latest_scores = scores_df[scores_df["datetime"] == latest_date].sort_values("score", ascending=False)

    col1, col2 = st.columns([1, 1])
    with col1:
        st.caption(f"最新预测日期: {latest_date}")
        # 添加股票名称
        display_scores = latest_scores.head(20)[["instrument", "score"]].reset_index(drop=True)
        display_scores["名称"] = display_scores["instrument"].map(stock_names).fillna("-")
        display_scores.columns = ["代码", "分数", "名称"]
        display_scores = display_scores[["代码", "名称", "分数"]]
        st.dataframe(
            display_scores,
            use_container_width=True,
            hide_index=True,
        )
    with col2:
        st.caption("分数分布")
        import plotly.express as px
        fig = px.histogram(latest_scores, x="score", nbins=50, labels={"score": "预测分数"})
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=0, b=0))
        st.plotly_chart(fig, use_container_width=True)
else:
    st.info("未找到ML预测分数，运行 `python scripts/train_ml.py` 生成")

st.divider()

# 持仓状态
st.subheader("当前持仓")
holdings_path = os.path.join("state", "holdings.json")
if os.path.exists(holdings_path):
    import json
    with open(holdings_path) as f:
        holdings = json.load(f)
    if holdings:
        holdings_df = pd.DataFrame(holdings)
        st.dataframe(holdings_df, use_container_width=True, hide_index=True)
    else:
        st.info("当前无持仓")
else:
    st.info("未找到持仓文件")

st.divider()

# 历史信号列表
st.subheader("历史信号记录")
history_df = pd.DataFrame({
    "日期": signal_dates,
    "文件": [os.path.basename(f) for f in signal_files],
})
st.dataframe(history_df, use_container_width=True, hide_index=True)

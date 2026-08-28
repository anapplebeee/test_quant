"""数据总览页面"""
from __future__ import annotations

import gradio as gr
import plotly.express as px

from api.data_api import get_stock_stats, get_universe, get_sample_data
from frontend.theme import metric_card, page_header


def render():
    """渲染数据总览 Tab"""
    with gr.Tab("🗃️ 数据总览"):
        gr.HTML(page_header("🗃️ 数据总览", "股票池状态 / 数据覆盖 / 市场概览"))

        stats = get_stock_stats()
        with gr.Row():
            gr.HTML(metric_card("股票数量", f"{stats['stock_count']:,}", "blue"))
            gr.HTML(metric_card("股票池快照", str(stats['universe_count']), "green"))
            gr.HTML(metric_card("指数数量", str(stats['index_count']), "purple"))
            gr.HTML(metric_card("最新分数日期", stats.get('last_score_date', 'N/A'), "teal"))

        gr.Markdown("---")
        gr.Markdown("### 📋 最新股票池成分")
        universe_df = get_universe()
        gr.Dataframe(value=universe_df, interactive=False)

        gr.Markdown("---")
        gr.Markdown("### 🔍 样本数据 (000001 平安银行)")
        sample_df = get_sample_data()
        if sample_df is not None:
            with gr.Row():
                gr.Markdown(f"**起始:** {sample_df['date'].iloc[0]}")
                gr.Markdown(f"**结束:** {sample_df['date'].iloc[-1]}")
                gr.Markdown(f"**交易日:** {len(sample_df):,}")

            fig = px.line(sample_df, x="date", y="close",
                          labels={"close": "收盘价", "date": "日期"})
            fig.update_layout(height=350, margin=dict(l=0, r=0, t=0, b=0))
            gr.Plot(value=fig)

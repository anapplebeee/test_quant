"""数据总览页面"""
from __future__ import annotations

import gradio as gr
import plotly.express as px

from api.data_api import get_stock_stats, get_universe, get_stock_list, get_stock_data
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
        gr.Markdown("### 📈 股票日线数据")
        
        stock_list = get_stock_list()
        stock_selector = gr.Dropdown(
            label="选择股票",
            choices=stock_list[:100],  # 显示前100只
            value="000001",
            interactive=True,
        )
        
        stock_info = gr.Markdown()
        stock_plot = gr.Plot()
        
        def update_stock_chart(symbol):
            df = get_stock_data(symbol)
            if df is None or df.empty:
                return f"未找到 {symbol} 的数据", None
            
            name_map = {}
            try:
                from common import load_stock_names
                name_map = load_stock_names()
            except Exception:
                pass
            
            stock_name = name_map.get(symbol, "")
            display_name = f"{symbol} {stock_name}" if stock_name else symbol
            
            info = f"**{display_name}** | 起始: {df['date'].iloc[0]} | 结束: {df['date'].iloc[-1]} | 交易日: {len(df):,}"
            
            fig = px.line(df, x="date", y="close",
                         labels={"close": "收盘价", "date": "日期"})
            fig.update_layout(
                title=f"{display_name} 价格走势",
                height=400,
                margin=dict(l=0, r=0, t=40, b=0),
                xaxis_title="日期",
                yaxis_title="收盘价",
            )
            return info, fig
        
        stock_selector.change(
            fn=update_stock_chart,
            inputs=[stock_selector],
            outputs=[stock_info, stock_plot],
        )
        
        # 初始加载
        init_info, init_plot = update_stock_chart("000001")
        stock_info.value = init_info
        stock_plot.value = init_plot

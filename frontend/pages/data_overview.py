"""数据总览页面"""
from __future__ import annotations

import gradio as gr
import plotly.express as px

from api.data_api import get_index_coverage, get_stock_stats, get_universe, get_stock_list, get_stock_data
from frontend.theme import metric_card, page_header, info_card


def _board_text(stats: dict) -> str:
    return " / ".join(
        f"{board} {count}" for board, count in stats.get("index_boards", {}).items() if count
    ) or "无"


def render():
    """渲染数据总览 Tab"""
    with gr.Tab("🗃️ 数据总览"):
        gr.HTML(page_header("🗃️ 数据总览", "股票池状态 / 数据覆盖 / 市场概览"))

        stats = get_stock_stats()
        with gr.Row():
            m1 = gr.HTML(metric_card("股票数量", f"{stats['stock_count']:,}", "blue"))
            m2 = gr.HTML(metric_card("股票池快照", str(stats['universe_count']), "green"))
            m3 = gr.HTML(metric_card("指数数量", str(stats['index_count']), "purple"))
            m4 = gr.HTML(metric_card("最新分数日期", stats.get('last_score_date', 'N/A'), "teal"))
        board_md = gr.Markdown(f"> **指数板块覆盖**：{_board_text(stats)}（明细见下方表格）")

        def _refresh_overview():
            s = get_stock_stats()
            return (
                metric_card("股票数量", f"{s['stock_count']:,}", "blue"),
                metric_card("股票池快照", str(s['universe_count']), "green"),
                metric_card("指数数量", str(s['index_count']), "purple"),
                metric_card("最新分数日期", s.get('last_score_date', 'N/A'), "teal"),
                f"> **指数板块覆盖**：{_board_text(s)}（明细见下方表格）",
                get_universe(),
            )

        refresh_btn = gr.Button("🔄 刷新数据概览", size="sm")
        universe_df = gr.Dataframe(value=get_universe(), interactive=False)
        refresh_btn.click(_refresh_overview, outputs=[m1, m2, m3, m4, board_md, universe_df])
        gr.Timer(60).tick(_refresh_overview, outputs=[m1, m2, m3, m4, board_md, universe_df])

        gr.Markdown("---")

        with gr.Accordion("📊 指数覆盖（按板块分类）", open=False):
            gr.Markdown(
                "*口径说明：**指数数量 = 已覆盖指数个数**（与股票数量的「唯一代码数」口径一致）；"
                f"实际指数日线文件 {stats.get('index_file_count', '-')} 个（按年分区，上证指数历史可回溯至 1990 年）。"
                "⬜ 未拉取时可在 🧰 操作中心运行「更新常用指数」批量补齐。*"
            )
            coverage = get_index_coverage()
            gr.Dataframe(value=coverage, interactive=False, max_height=320)

        gr.Markdown("---")

        with gr.Accordion("📈 股票日线数据", open=True):
            stock_list = get_stock_list()
            default_symbol = "000001"
            top100 = stock_list[:100]
            if default_symbol in stock_list and default_symbol not in top100:
                dropdown_choices = [default_symbol] + top100
            else:
                dropdown_choices = top100
            stock_selector = gr.Dropdown(
                label="选择股票",
                choices=dropdown_choices,
                value=default_symbol,
                allow_custom_value=True,
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
                    template="plotly_white",
                )
                return info, fig

            stock_selector.change(
                fn=update_stock_chart,
                inputs=[stock_selector],
                outputs=[stock_info, stock_plot],
            )

            init_info, init_plot = update_stock_chart("000001")
            stock_info.value = init_info
            stock_plot.value = init_plot

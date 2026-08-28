"""回测中心页面"""
from __future__ import annotations

import plotly.graph_objects as go
import gradio as gr

from api.backtest_api import (
    get_backtest_list,
    get_backtest_summary,
    get_equity_curve,
    get_trades,
)
from frontend.theme import metric_card, page_header


def render():
    """渲染回测中心 Tab"""
    with gr.Tab("📈 回测中心"):
        gr.HTML(page_header("📈 回测中心", "净值曲线 / 回撤分析 / 交易记录"))

        backtest_names = get_backtest_list()
        if not backtest_names:
            gr.Info("未找到回测结果，请先运行回测")
            return

        default_name = backtest_names[-1]

        # 绩效概览
        gr.Markdown("### 🎯 绩效概览")
        summary = get_backtest_summary(default_name)
        if summary:
            with gr.Row():
                gr.HTML(metric_card("累计收益", f"{summary['total_return']*100:.1f}%",
                                    "green" if summary['total_return'] > 0 else "red"))
                gr.HTML(metric_card("年化收益", f"{summary['cagr']*100:.1f}%", "blue"))
                gr.HTML(metric_card("夏普比率", f"{summary['sharpe']:.2f}", "purple"))
                gr.HTML(metric_card("最大回撤", f"{summary['max_drawdown']*100:.1f}%", "red"))
                gr.HTML(metric_card("卡玛比率", f"{summary.get('calmar', 0):.2f}", "teal"))
            gr.Markdown(f"*回测区间: {summary.get('start','')} ~ {summary.get('end','')}*")

        gr.Markdown("---")

        # 净值曲线 + 回撤曲线
        equity_df = get_equity_curve(default_name)
        if equity_df is not None:
            columns = [c for c in equity_df.columns if c != "date"]
            default_cols = columns[:2] if len(columns) > 2 else columns

            gr.Markdown("### 📈 净值曲线")
            col_select = gr.CheckboxGroup(
                label="选择参数组", choices=columns, value=default_cols,
            )

            def plot_all(selected_cols):
                if not selected_cols:
                    return None, None
                fig_eq = go.Figure()
                fig_dd = go.Figure()
                for col in selected_cols:
                    series = equity_df[col]
                    fig_eq.add_trace(go.Scatter(
                        x=equity_df["date"], y=series, mode="lines",
                        name=str(col).replace("min_avg_amount=", "日均额≥"),
                    ))
                    # 回撤
                    dd = (series - series.cummax()) / series.cummax() * 100
                    fig_dd.add_trace(go.Scatter(
                        x=equity_df["date"], y=dd, mode="lines", fill="tozeroy",
                        name=str(col).replace("min_avg_amount=", "日均额≥"),
                    ))
                fig_eq.add_hline(y=1_000_000, line_dash="dash", line_color="gray",
                                 annotation_text="初始资金")
                fig_eq.update_layout(title="策略净值", height=400,
                                     margin=dict(l=0, r=0, t=40, b=0))
                fig_dd.update_layout(title="回撤 (%)", height=250,
                                     margin=dict(l=0, r=0, t=40, b=0))
                return fig_eq, fig_dd

            initial_figs = plot_all(default_cols)
            gr.Plot(value=initial_figs[0])
            gr.Markdown("### 📉 回撤曲线")
            gr.Plot(value=initial_figs[1])
            col_select.change(plot_all, inputs=col_select, outputs=[gr.Plot(), gr.Plot()])

        gr.Markdown("---")

        # 交易记录
        gr.Markdown("### 📋 最近交易记录")
        trades_df = get_trades(default_name)
        if trades_df is not None:
            gr.Dataframe(value=trades_df.head(30), interactive=False)

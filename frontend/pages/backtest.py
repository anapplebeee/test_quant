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


def _load_backtest(name: str):
    """加载指定回测的摘要/图表/交易"""
    if not name:
        return "未选择回测", None, None, None

    # 摘要
    s = get_backtest_summary(name)
    if s:
        md = (
            f"**累计收益:** {s['total_return']*100:.1f}% | "
            f"**年化:** {s['cagr']*100:.1f}% | "
            f"**夏普:** {s['sharpe']:.2f} | "
            f"**最大回撤:** {s['max_drawdown']*100:.1f}% | "
            f"**卡玛:** {s.get('calmar', 0):.2f}  \n"
            f"*区间: {s.get('start','')} ~ {s.get('end','')}*"
        )
    else:
        md = "未找到回测摘要"

    # 净值 + 回撤
    equity_df = get_equity_curve(name)
    fig_eq, fig_dd = None, None
    if equity_df is not None:
        cols = [c for c in equity_df.columns if c != "date"]
        fig_eq = go.Figure()
        fig_dd = go.Figure()
        for col in cols:
            series = equity_df[col]
            label = str(col).replace("min_avg_amount=", "日均额≥")
            fig_eq.add_trace(go.Scatter(
                x=equity_df["date"], y=series, mode="lines", name=label,
            ))
            dd = (series - series.cummax()) / series.cummax() * 100
            fig_dd.add_trace(go.Scatter(
                x=equity_df["date"], y=dd, mode="lines", fill="tozeroy", name=label,
            ))
        fig_eq.add_hline(y=1_000_000, line_dash="dash", line_color="gray",
                         annotation_text="初始资金")
        fig_eq.update_layout(title="策略净值", height=400,
                             margin=dict(l=0, r=0, t=40, b=0))
        fig_dd.update_layout(title="回撤 (%)", height=250,
                             margin=dict(l=0, r=0, t=40, b=0))

    # 交易
    trades_df = get_trades(name)
    trades_display = trades_df.head(30) if trades_df is not None else None

    return md, fig_eq, fig_dd, trades_display


def render():
    """渲染回测中心 Tab"""
    with gr.Tab("📈 回测中心"):
        gr.HTML(page_header("📈 回测中心", "净值曲线 / 回撤分析 / 交易记录"))

        backtest_names = get_backtest_list()
        if not backtest_names:
            gr.Info("未找到回测结果，请先运行回测")
            return

        default_name = backtest_names[-1]
        init = _load_backtest(default_name)

        with gr.Row():
            selected_bt = gr.Dropdown(
                label="选择回测", choices=backtest_names, value=default_name,
                filterable=True,
            )

        summary_md = gr.Markdown(value=init[0])
        equity_plot = gr.Plot(value=init[1])
        dd_plot = gr.Plot(value=init[2])
        trades_table = gr.Dataframe(value=init[3], interactive=False)

        selected_bt.change(
            _load_backtest,
            inputs=[selected_bt],
            outputs=[summary_md, equity_plot, dd_plot, trades_table],
        )

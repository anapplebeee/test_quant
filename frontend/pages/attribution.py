"""归因分析页面：行业归因 / 月度收益 / 持仓因子暴露。"""
from __future__ import annotations

import gradio as gr
import plotly.express as px
import plotly.graph_objects as go

from api.portfolio_api import (
    latest_industry_trade_summary,
    latest_monthly_returns,
    portfolio_factor_exposure,
)
from frontend.theme import page_header, section_header


def render():
    """渲染归因分析 Tab。"""
    with gr.Tab("🧩 归因分析"):
        gr.HTML(page_header(
            "🧩 归因分析",
            "从行业交易、收益时序与当前持仓风格三个视角解释策略结果。",
            "ATTRIBUTION",
        ))
        gr.Button("🔄 刷新本页（重新加载最新制品）", size="sm").click(
            js="() => location.reload()")

        gr.HTML(section_header("行业交易分布", "按最新回测成交额汇总买卖方向。", "INDUSTRY"))
        industry = latest_industry_trade_summary()
        if industry.empty:
            gr.Markdown("> ⚠️ 暂无同时包含交易记录和行业映射的回测制品")
        else:
            figure = go.Figure()
            figure.add_trace(go.Bar(
                x=industry["行业"], y=industry["买入"],
                name="买入", marker_color="#E53935",
            ))
            figure.add_trace(go.Bar(
                x=industry["行业"], y=industry["卖出"],
                name="卖出", marker_color="#43A047",
            ))
            figure.update_layout(
                barmode="group", xaxis_tickangle=-45, height=400,
                margin=dict(l=0, r=0, t=30, b=80), template="plotly_white",
            )
            gr.Plot(value=figure)

        gr.HTML(section_header("月度收益热力图", "识别年度稳定性、季节性与收益集中月份。", "RETURN PATTERN"))
        monthly = latest_monthly_returns()
        if monthly.empty:
            gr.Markdown("> ⚠️ 暂无带日期索引的回测净值制品")
        else:
            monthly_figure = px.imshow(
                monthly,
                color_continuous_scale="RdYlGn",
                text_auto=".1f",
                aspect="auto",
                labels=dict(x="月份", y="年份", color="月收益%"),
            )
            monthly_figure.update_layout(
                height=300, margin=dict(l=0, r=0, t=0, b=0), template="plotly_white",
            )
            gr.Plot(value=monthly_figure)

        gr.HTML(section_header(
            "持仓因子暴露",
            "基于当前真实持仓计算；这是可解释风格代理，并非完整 Barra 风险模型。",
            "STYLE EXPOSURE",
        ))
        gr.Markdown(
            "*持仓等权是当前持仓股票的等权参照；主动暴露 = 市值加权组合 − 持仓等权。"
            "它不是全市场 Barra 风险模型。价值等基本面因子缺数据时明确显示 N/A。*"
        )
        exposure = portfolio_factor_exposure()
        if exposure.empty:
            gr.Markdown("> ⚠️ 当前无持仓，或持仓行情不足 25 个交易日")
        else:
            gr.Dataframe(
                value=exposure, interactive=False, show_search="filter",
                pinned_columns=1, buttons=["fullscreen", "copy"],
            )
            valid = exposure.dropna(subset=["主动暴露"])
            if not valid.empty:
                exposure_figure = go.Figure()
                exposure_figure.add_trace(go.Bar(
                    x=valid["因子"], y=valid["组合暴露"],
                    name="组合", marker_color="#1E88E5",
                ))
                exposure_figure.add_trace(go.Bar(
                    x=valid["因子"], y=valid["持仓等权"],
                    name="持仓等权", marker_color="#90A4AE",
                ))
                exposure_figure.update_layout(
                    barmode="group", height=350,
                    margin=dict(l=0, r=0, t=30, b=0), template="plotly_white",
                )
                gr.Plot(value=exposure_figure)

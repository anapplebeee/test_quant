"""Factor ecology: rolling health, redundancy and degradation alerts."""

from __future__ import annotations

import gradio as gr
import pandas as pd
import plotly.graph_objects as go

from api.research_api import (
    factor_audit_status_md,
    factor_audit_summary,
    factor_correlation,
)
from frontend.theme import page_header


def _health_table() -> pd.DataFrame:
    summary = factor_audit_summary()
    if summary.empty:
        return summary
    columns = [
        "factor",
        "status",
        "category",
        "in_strategy",
        "ic",
        "late_ic",
        "recent_ic",
        "positive_rate",
        "coverage",
        "max_abs_corr",
        "corr_peer",
    ]
    output = summary[[column for column in columns if column in summary.columns]].copy()
    output["alert"] = "正常"
    output.loc[output["recent_ic"] <= 0, "alert"] = "近期失效"
    output.loc[output["coverage"] < 0.5, "alert"] = "覆盖不足"
    output.loc[output["max_abs_corr"] >= 0.85, "alert"] = "高度冗余"
    output.loc[
        (output["in_strategy"] == True) & (output["recent_ic"] <= 0),  # noqa: E712
        "alert",
    ] = "策略因子预警"
    return output.rename(
        columns={
            "factor": "因子",
            "status": "结论",
            "category": "类别",
            "in_strategy": "当前策略",
            "ic": "全期IC",
            "late_ic": "后半段IC",
            "recent_ic": "近12期IC",
            "positive_rate": "正率",
            "coverage": "覆盖率",
            "max_abs_corr": "最大相关",
            "corr_peer": "最高相关因子",
            "alert": "预警",
        }
    )


def _correlation_heatmap() -> go.Figure | None:
    correlation = factor_correlation()
    if correlation.empty:
        return None
    figure = go.Figure(
        data=go.Heatmap(
            z=correlation.to_numpy(dtype=float),
            x=correlation.columns,
            y=correlation.index,
            zmin=-1,
            zmax=1,
            colorscale="RdYlGn",
            colorbar=dict(title="Rank corr"),
        )
    )
    figure.update_layout(
        title="因子截面排名相关性",
        height=650,
        xaxis_tickangle=-45,
        margin=dict(l=80, r=20, t=50, b=100),
    )
    return figure


def _refresh():
    return factor_audit_status_md(), _health_table(), _correlation_heatmap()


def render() -> None:
    """Render factor ecology monitoring."""
    with gr.Tab("🌿 因子生态"):
        gr.HTML(page_header("🌿 因子生态监控", "近期失效 / 覆盖退化 / 冗余拥挤"))
        status = gr.Markdown(value=factor_audit_status_md())
        refresh = gr.Button("🔄 刷新生态监控", size="sm")

        health = _health_table()
        if health.empty:
            gr.Markdown(
                "> 先在“🧰 操作中心 → 因子审计”运行统一审计。"
                "页面只显示真实产物，不生成占位 IC 或拥挤度数字。"
            )
        health_table = gr.Dataframe(
            value=health,
            interactive=False,
            max_height=520,
            wrap=True,
        )
        heatmap = gr.Plot(value=_correlation_heatmap())
        refresh.click(_refresh, outputs=[status, health_table, heatmap])

        gr.Markdown(
            "*预警优先级：当前策略因子近期 IC≤0 > 覆盖不足 > 高度冗余。"
            "拥挤度在缺少持仓/资金流一手数据时仅使用因子相关性代理，不冒充真实资金拥挤度。*"
        )

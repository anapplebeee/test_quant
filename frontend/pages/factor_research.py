"""Unified factor research dashboard backed by traceable audit artifacts."""

from __future__ import annotations

import gradio as gr
import pandas as pd
import plotly.graph_objects as go

from api.research_api import (
    factor_audit_status_md,
    factor_audit_summary,
    factor_correlation,
    factor_ic_history,
)
from frontend.theme import page_header
from quart.research.factor_audit import FACTOR_SPECS

FACTOR_DEFINITIONS = pd.DataFrame(
    [
        {
            "因子": spec.name,
            "类别": spec.category,
            "定义": spec.description,
            "新候选": "是" if spec.is_new else "否",
            "当前策略": "是" if spec.in_strategy else "否",
        }
        for spec in FACTOR_SPECS
    ]
)

SUMMARY_LABELS = {
    "factor": "因子",
    "status": "结论",
    "category": "类别",
    "is_new": "新候选",
    "in_strategy": "当前策略",
    "ic": "IC",
    "icir": "ICIR",
    "positive_rate": "IC正率",
    "early_ic": "前半段IC",
    "late_ic": "后半段IC",
    "recent_ic": "近12期IC",
    "ic_pvalue": "IC p值",
    "fdr_qvalue": "FDR q值",
    "top_abs_bp": "多头绝对bp",
    "long_only_bp": "多头超额bp",
    "long_short_bp": "多空bp",
    "top_turnover": "Top组换手",
    "top_median_amount_m": "Top组成交额中位数(百万元)",
    "coverage": "覆盖率",
    "avg_stocks": "平均股票数",
    "max_abs_corr": "最大相关",
    "corr_peer": "最高相关因子",
}


def _display_summary(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return summary
    output = summary.rename(columns=SUMMARY_LABELS).copy()
    for column in ("新候选", "当前策略"):
        if column in output.columns:
            output[column] = output[column].map({True: "是", False: "否"}).fillna(output[column])
    return output


def _summary_figure(summary: pd.DataFrame) -> go.Figure | None:
    if summary.empty or "ic" not in summary.columns:
        return None
    figure = go.Figure(
        go.Bar(
            x=summary["factor"],
            y=summary["ic"],
            marker_color=["#E53935" if value >= 0 else "#43A047" for value in summary["ic"]],
            text=summary["ic"].round(3),
            textposition="outside",
            customdata=summary[["status", "icir", "recent_ic"]],
            hovertemplate=(
                "%{x}<br>IC=%{y:.4f}<br>状态=%{customdata[0]}"
                "<br>ICIR=%{customdata[1]:.2f}<br>近期IC=%{customdata[2]:.4f}<extra></extra>"
            ),
        )
    )
    figure.add_hline(y=0, line_dash="dash", line_color="gray")
    figure.update_layout(
        title="统一方向后的 RankIC（越高越优）",
        height=410,
        xaxis_tickangle=-45,
        margin=dict(l=0, r=0, t=45, b=0),
        template="plotly_white",
    )
    return figure


def _history_figure(factor: str | None) -> go.Figure | None:
    history = factor_ic_history(factor)
    if history.empty:
        return None
    history = history.sort_values("date")
    history["rolling_ic"] = history["ic"].rolling(min(6, len(history)), min_periods=2).mean()
    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=history["date"],
            y=history["ic"],
            name="单期 IC",
            marker_color=["#E53935" if value >= 0 else "#43A047" for value in history["ic"]],
        )
    )
    figure.add_trace(
        go.Scatter(
            x=history["date"],
            y=history["rolling_ic"],
            mode="lines+markers",
            name="滚动 IC",
            line=dict(color="#1E88E5", width=2),
        )
    )
    figure.add_hline(y=0, line_dash="dash", line_color="gray")
    figure.update_layout(
        title=f"{factor or '因子'} IC 时序与失效监控",
        height=360,
        margin=dict(l=0, r=0, t=45, b=0),
        template="plotly_white",
    )
    return figure


def _correlation_table() -> pd.DataFrame:
    correlation = factor_correlation()
    if correlation.empty:
        return correlation
    output = correlation.round(3).reset_index()
    return output.rename(columns={output.columns[0]: "因子"})


def _refresh_view():
    summary = factor_audit_summary()
    factors = summary["factor"].tolist() if not summary.empty and "factor" in summary else []
    selected = factors[0] if factors else None
    return (
        factor_audit_status_md(),
        _display_summary(summary),
        _summary_figure(summary),
        gr.update(choices=factors, value=selected),
        _history_figure(selected),
        _correlation_table(),
    )


def render() -> None:
    """Render the factor audit tab."""
    summary = factor_audit_summary()
    factors = summary["factor"].tolist() if not summary.empty and "factor" in summary else []
    selected = factors[0] if factors else None

    with gr.Tab("🔬 因子研究"):
        gr.HTML(page_header("🔬 因子研究", "T+1 可执行标签 / 滚动 IC / 冗余与失效监控"))
        status = gr.Markdown(value=factor_audit_status_md())
        gr.Markdown(
            "*口径：T 日收盘形成因子，T+1 开盘进入，过滤入场/退出停牌与开盘涨跌停；"
            "多头超额相对同日可交易股票等权收益，候选还需通过 FDR 多重检验。"
            "结论是研究门槛，不构成收益承诺。*"
        )
        refresh = gr.Button("🔄 刷新最新审计", size="sm")

        with gr.Accordion(f"📖 因子定义（{len(FACTOR_DEFINITIONS)} 个）", open=False):
            gr.Dataframe(value=FACTOR_DEFINITIONS, interactive=False, max_height=420)

        summary_plot = gr.Plot(value=_summary_figure(summary))
        summary_table = gr.Dataframe(
            value=_display_summary(summary),
            interactive=False,
            max_height=480,
            wrap=True,
        )

        gr.Markdown("### 滚动 IC 与失效预警")
        factor_selector = gr.Dropdown(
            label="因子",
            choices=factors,
            value=selected,
            filterable=True,
        )
        history_plot = gr.Plot(value=_history_figure(selected))
        factor_selector.change(_history_figure, inputs=[factor_selector], outputs=[history_plot])

        with gr.Accordion("🧬 因子相关性（识别重复押注）", open=False):
            correlation_table = gr.Dataframe(
                value=_correlation_table(),
                interactive=False,
                max_height=460,
                wrap=False,
            )

        refresh.click(
            _refresh_view,
            outputs=[
                status,
                summary_table,
                summary_plot,
                factor_selector,
                history_plot,
                correlation_table,
            ],
        )

        gr.Markdown(
            "**准入规则**：IC、ICIR、FDR q≤0.10、正率、前后半段、近期 IC、覆盖率同时通过才列为候选；"
            "与已有因子相关性 ≥0.85 的标为“冗余候选”，不能直接叠加权重。"
        )

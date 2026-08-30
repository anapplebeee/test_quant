"""制品浏览组件：按 run_id 追溯运行结果。

本模块**只做 Gradio 绑定**，所有格式化逻辑在 `api/artifacts_api.py`
（`runs_table` / `run_detail_md` / `wfa_panel_md`）。这样：
  * 展示逻辑可测试（frontend 依赖 gradio，测试环境装不了）
  * 符合 UI 与业务逻辑分离，换 Streamlit 前端时逻辑不用重写

设计原则：**面板不能让页面崩溃**。制品目录可能不存在、manifest 可能损坏、
字段可能缺失——任何异常都必须降级为一句提示，而不是整页 KeyError。
"""
from __future__ import annotations

import gradio as gr

from api import artifacts_api


def _safe(fn, default):
    """组件内所有 api 调用的统一降级：异常 → 默认值，绝不向上抛。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - UI 层必须吞掉所有异常
        return default(f"{type(exc).__name__}: {exc}") if callable(default) else default


def render_artifacts_panel(open: bool = False) -> None:
    """运行制品浏览器（Accordion）。"""
    with gr.Accordion(
        "🧾 运行制品（artifacts/ · 按 run_id 追溯参数/数据/代码版本）", open=open
    ):
        gr.Markdown("*每次运行的任务、状态、数据版本与关键指标*")

        runs_tbl = gr.Dataframe(
            value=_safe(artifacts_api.runs_table, lambda e: None),
            interactive=False, max_height=280, wrap=True,
        )
        refresh_runs = gr.Button("🔄 刷新运行列表", size="sm")

        choices = _safe(artifacts_api.run_choices, lambda e: [])
        run_dd = gr.Dropdown(
            label="选择运行", choices=choices,
            value=choices[0] if choices else None, filterable=True,
        )
        detail_md = gr.Markdown(
            value=(
                _safe(lambda: artifacts_api.run_detail_md(
                    artifacts_api.run_id_from_choice(choices[0])),
                    lambda e: f"*读取失败: {e}*")
                if choices else "*暂无运行制品*"
            )
        )

        def _on_select(choice: str) -> str:
            run_id = artifacts_api.run_id_from_choice(choice) if choice else ""
            if not run_id:
                return "*未选择运行*"
            return _safe(lambda: artifacts_api.run_detail_md(run_id),
                         lambda e: f"*读取失败: {e}*")

        def _refresh_all():
            tbl = _safe(artifacts_api.runs_table, lambda e: None)
            ch = _safe(artifacts_api.run_choices, lambda e: [])
            first = ch[0] if ch else None
            return (
                tbl if tbl is not None and not tbl.empty else None,
                gr.update(choices=ch, value=first),
                _on_select(first) if first else "*暂无运行制品*",
            )

        run_dd.change(_on_select, inputs=[run_dd], outputs=[detail_md])
        refresh_runs.click(_refresh_all, outputs=[runs_tbl, run_dd, detail_md])


def render_wfa_panel(open: bool = False) -> None:
    """Walk-Forward 过拟合诊断面板（Accordion）。"""
    with gr.Accordion("🔁 Walk-Forward 样本外验证（过拟合诊断）", open=open):
        wfa_md = gr.Markdown(
            value=_safe(artifacts_api.wfa_panel_md, lambda e: f"*读取失败: {e}*")
        )
        refresh_wfa = gr.Button("🔄 刷新 WFA 结果", size="sm")
        refresh_wfa.click(
            lambda: _safe(artifacts_api.wfa_panel_md, lambda e: f"*读取失败: {e}*"),
            outputs=[wfa_md],
        )
        gr.Markdown(
            "*判读：衰减比 = 样本外指标 / 样本内指标。"
            "≥0.8 稳健；0.4~0.8 存在过拟合；<0.4 基本在挑噪声。*"
        )


__all__ = ["render_artifacts_panel", "render_wfa_panel"]

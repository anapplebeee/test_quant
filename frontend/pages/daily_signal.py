"""每日信号页面：信号报告 + 交易计划详情 + ML 预测分数。

功能：
- 信号报告：查看历史每日信号 markdown 报告
- 计划详情：展示明日交易计划（买卖方向和数量）
- 计划审批：跳转手动交易页签审批
"""
from __future__ import annotations

from datetime import date

import gradio as gr
import pandas as pd

import data_bus
from api.data_api import get_latest_ml_scores
from api.manual_trading_api import plan_view, plans_view
from api.strategy_api import load_signal_report, signal_snapshot
from frontend.theme import page_header


def _plan_snapshot(as_of: str):
    """获取计划快照"""
    if not as_of:
        as_of = date.today().isoformat()
    frame, choices = plans_view(as_of=as_of)
    value = choices[0] if choices else None
    return frame, gr.update(choices=choices, value=value), value


def _plan_detail(plan_id):
    """获取单个计划的详情"""
    if not plan_id:
        return "*请选择计划查看详情*", pd.DataFrame()
    md, orders = plan_view(plan_id)
    if isinstance(orders, list):
        orders = pd.DataFrame(orders)
    return md, orders


def render():
    """渲染每日信号 Tab"""
    with gr.Tab("📋 每日信号"):
        gr.HTML(page_header("📋 每日信号", "信号报告 / 交易计划 / 审批"))
        gr.Markdown("> ⚠️ 信号仅供研究参考，不构成投资建议")

        # ===== 计划审批区（置顶，方便操作） =====
        with gr.Accordion("📋 交易计划（T+1 委托审批）", open=True):
            frame, choices = plans_view(as_of=date.today().isoformat())
            value = choices[0] if choices else None
            plan_md, orders = plan_view(value)

            today = date.today().isoformat()
            with gr.Row():
                plan_date = gr.Textbox(label="计划截止日期", value=today)
                refresh_plans_btn = gr.Button("🔄 刷新计划", variant="secondary", size="sm")
            plans_table = gr.Dataframe(value=frame, interactive=False, max_height=250)

            plan_selector = gr.Dropdown(label="选择计划查看详情", choices=choices, value=value)
            plan_detail_md = gr.Markdown(value=plan_md)
            orders_table = gr.Dataframe(value=orders, interactive=False, max_height=300)

            gr.Markdown("""
            > 💡 **操作指引**
            > - 上表展示当前及其历史交易计划（状态：DRAFT 待审批 / APPROVED 已审批）
            > - 在「💼 手动交易」页完成计划审批、成交录入和收盘对账
            > - 切入计划后会在下方展示具体买卖订单
            """)

            plan_selector.change(
                _plan_detail,
                inputs=[plan_selector],
                outputs=[plan_detail_md, orders_table],
            )

            def _refresh_plan_panel(plan_date_val):
                f, sel_update, sel_val = _plan_snapshot(plan_date_val)
                md, ords = plan_view(sel_val)
                return f, sel_update, md, ords

            refresh_plans_btn.click(
                _refresh_plan_panel,
                inputs=[plan_date],
                outputs=[plans_table, plan_selector, plan_detail_md, orders_table],
            )

        gr.Markdown("---")

        # ===== 信号报告区 =====
        with gr.Accordion("📄 每日信号报告", open=True):
            choices2, latest2, content2 = signal_snapshot()
            signal_date = gr.Dropdown(label="选择日期", choices=choices2, value=latest2)
            signal_content = gr.Markdown(value=content2)
            signal_date.change(load_signal_report, inputs=signal_date, outputs=signal_content)

            def _refresh():
                c, v, txt = signal_snapshot()
                return gr.update(choices=c, value=v), txt

            refresh_btn = gr.Button("🔄 刷新信号列表", size="sm")
            refresh_btn.click(_refresh, outputs=[signal_date, signal_content])

        gr.Markdown("---")

        # ===== ML 预测分数 =====
        with gr.Accordion("🤖 ML 预测分数 (最新)", open=False):
            scores_df = get_latest_ml_scores(limit=50)
            if scores_df is None:
                gr.Markdown("*暂无 ML 预测数据。运行 🤖 ML 训练 生成 `data/scores/preds.csv`*")
            preds_table = gr.Dataframe(value=scores_df, interactive=False, max_height=400)

        # ===== 跨页联动：任务完成 → 自动刷新计划/信号/预测 =====
        seen_state = gr.State(data_bus.current())

        def _poll_data_version(seen_val: int, plan_date_val: str, sig_sel: str | None):
            changed, cur = data_bus.poll(seen_val)
            if not changed:
                return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), seen_val
            f, sel_update, sel_val = _plan_snapshot(plan_date_val)
            md, ords = plan_view(sel_val)
            c, v, txt = signal_snapshot()
            sig_update = gr.update(choices=c, value=v if v in c else (sig_sel if sig_sel in c else v))
            preds = get_latest_ml_scores(limit=50)
            return f, sel_update, md, ords, sig_update, txt, preds, cur

        gr.Timer(5).tick(
            _poll_data_version,
            inputs=[seen_state, plan_date, signal_date],
            outputs=[plans_table, plan_selector, plan_detail_md, orders_table,
                     signal_date, signal_content, preds_table, seen_state],
        )

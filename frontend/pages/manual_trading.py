"""手动交易页面：T+1 账户、计划、成交与收盘对账。"""
from __future__ import annotations

from datetime import date

import gradio as gr

from api.manual_trading_api import (
    account_view,
    adjust_order_action,
    approve_plan_action,
    cancel_plan_action,
    execution_view,
    export_plan_action,
    fills_view,
    import_fills_action,
    initialize_account_action,
    paper_trade_action,
    plan_view,
    plans_view,
    reconcile_action,
    reconciliation_template,
    record_fill_action,
)
from frontend.theme import page_header


def _plan_snapshot(as_of: str):
    frame, choices = plans_view(as_of=as_of)
    value = choices[0] if choices else None
    return frame, gr.update(choices=choices, value=value)


def _refresh_plan_bundle(choice: str | None, as_of: str):
    """计划相关面板级联刷新：计划列表 + 详情 + 执行复盘。

    背景（2026-08-31 前端体验审查）：审批/取消/调减后计划状态已变，
    但列表、详情和复盘面板停留在旧状态，用户以为操作失败。
    """
    frame, choices = plans_view(as_of=as_of)
    value = choice if choice in choices else (choices[0] if choices else None)
    plan_md, orders = plan_view(value)
    exec_md, exec_table = execution_view(value)
    return (
        frame,
        gr.update(choices=choices, value=value),
        plan_md,
        orders,
        exec_md,
        exec_table,
    )


def _refresh_account_bundle(as_of: str):
    """账户与成交面板级联刷新：成交录入/导入/对账后账本已变。"""
    summary, positions = account_view(as_of)
    return summary, positions, fills_view()


def render() -> None:
    """渲染手动交易 Tab。"""
    today = date.today().isoformat()
    initial_summary, initial_positions = account_view(today)
    initial_plans, initial_choices = plans_view(as_of=today)
    initial_choice = initial_choices[0] if initial_choices else None
    initial_plan_md, initial_orders = plan_view(initial_choice)
    initial_execution_md, initial_execution = execution_view(initial_choice)

    with gr.Tab("💼 手动交易"):
        gr.HTML(page_header("💼 手动交易", "账户快照 → T+1 计划 → 成交回填 → 收盘对账 → 执行复盘"))
        gr.Markdown(
            "> 当前为人工确认模式：平台不连接券商、不自动报单。所有计划必须先核对券商账户并手工审批。"
        )

        with gr.Accordion("1️⃣ 账户状态与初始化", open=True):
            with gr.Row():
                account_date = gr.Textbox(label="账户状态日期", value=today)
                account_refresh = gr.Button("🔄 刷新账户", variant="secondary")
            account_summary = gr.Markdown(value=initial_summary)
            positions_table = gr.Dataframe(value=initial_positions, interactive=False, max_height=380)
            account_refresh.click(account_view, inputs=[account_date], outputs=[account_summary, positions_table])

            with gr.Accordion("首次初始化 / 以券商快照覆盖", open=False):
                gr.Markdown(
                    "持仓 JSON 示例：`{\"600000\": {\"total_quantity\": 1000, "
                    "\"sellable_quantity\": 1000, \"cost_price\": 10.0}}`"
                )
                with gr.Row():
                    init_date = gr.Textbox(label="快照日期", value=today)
                    init_cash = gr.Number(label="现金总额", value=1_000_000.0)
                    init_force = gr.Checkbox(label="确认覆盖现有账户状态", value=False)
                init_positions = gr.Code(label="持仓 JSON", language="json", value="{}", lines=8)
                init_button = gr.Button("保存账户快照", variant="primary")
                init_status = gr.Markdown()
                init_button.click(
                    initialize_account_action,
                    inputs=[init_date, init_cash, init_positions, init_force],
                    outputs=[init_status, account_summary, positions_table],
                )

        with gr.Accordion("2️⃣ T+1 交易计划审批", open=True):
            with gr.Row():
                plan_selector = gr.Dropdown(
                    label="选择计划",
                    choices=initial_choices,
                    value=initial_choice,
                    filterable=True,
                )
                refresh_plans = gr.Button("🔄 刷新计划", variant="secondary")
            plans_table = gr.Dataframe(value=initial_plans, interactive=False, max_height=280)
            plan_summary = gr.Markdown(value=initial_plan_md)
            orders_table = gr.Dataframe(value=initial_orders, interactive=False, max_height=360)
            # 计划切换的详情+复盘联动统一在文件末尾注册（组件就绪后）
            refresh_plans.click(
                _plan_snapshot,
                inputs=[account_date],
                outputs=[plans_table, plan_selector],
            )

            gr.Markdown("**审批前只允许调减数量；新增交易应按计划外成交记录并在复盘中解释。**")
            with gr.Row():
                adjust_order_id = gr.Number(label="计划订单 ID", precision=0)
                adjust_quantity = gr.Number(label="批准数量", precision=0)
                adjust_reason = gr.Textbox(label="调减原因", placeholder="例如：降低单票风险")
                adjust_button = gr.Button("调减订单")
            plan_action_status = gr.Markdown()
            adjust_evt = adjust_button.click(
                adjust_order_action,
                inputs=[adjust_order_id, adjust_quantity, adjust_reason],
                outputs=[plan_action_status],
            )
            with gr.Row():
                approve_button = gr.Button("✅ 审批计划", variant="primary")
                cancel_reason = gr.Textbox(label="取消原因", placeholder="可选")
                cancel_button = gr.Button("⛔ 取消计划", variant="stop")
                export_button = gr.Button("⬇️ 导出人工下单 CSV")
            export_file = gr.File(label="计划 CSV", interactive=False)
            approve_evt = approve_button.click(approve_plan_action, inputs=[plan_selector], outputs=[plan_action_status])
            cancel_evt = cancel_button.click(
                cancel_plan_action,
                inputs=[plan_selector, cancel_reason],
                outputs=[plan_action_status],
            )
            export_button.click(export_plan_action, inputs=[plan_selector], outputs=[export_file])

        with gr.Accordion("3️⃣ 真实成交回填", open=True):
            gr.Markdown("计划订单 ID 可留空；系统会按交易日、代码、方向和剩余数量自动匹配唯一计划订单。")
            with gr.Row():
                fill_date = gr.Textbox(label="成交日期", value=today)
                fill_time = gr.Textbox(label="成交时间", placeholder="09:35:00")
                fill_symbol = gr.Textbox(label="代码", placeholder="600000")
                fill_side = gr.Dropdown(label="方向", choices=["BUY", "SELL"], value="BUY")
            with gr.Row():
                fill_quantity = gr.Number(label="数量", precision=0)
                fill_price = gr.Number(label="成交价")
                fill_order_id = gr.Number(label="计划订单 ID（可空）", precision=0)
                fill_broker_id = gr.Textbox(label="券商成交编号（推荐填写）")
            with gr.Accordion("费用与结算", open=False):
                with gr.Row():
                    fill_commission = gr.Number(label="佣金", value=0.0)
                    fill_stamp = gr.Number(label="印花税", value=0.0)
                    fill_transfer = gr.Number(label="过户费", value=0.0)
                    fill_other = gr.Number(label="其他费用", value=0.0)
                with gr.Row():
                    fill_settle_date = gr.Textbox(label="可卖日期（留空自动推算）")
                    estimate_fees = gr.Checkbox(label="费用全空时按配置估算", value=True)
            fill_button = gr.Button("记录成交", variant="primary")
            fill_status = gr.Markdown()
            with gr.Row():
                fills_file = gr.File(label="批量成交 CSV", type="filepath")
                import_estimate = gr.Checkbox(label="缺失费用时自动估算", value=True)
                import_button = gr.Button("导入成交 CSV")
                fills_refresh = gr.Button("🔄 刷新成交")
            import_status = gr.Markdown()
            fills_table = gr.Dataframe(value=fills_view(), interactive=False, max_height=360)
            fill_evt = fill_button.click(
                record_fill_action,
                inputs=[
                    fill_date,
                    fill_time,
                    fill_symbol,
                    fill_side,
                    fill_quantity,
                    fill_price,
                    fill_order_id,
                    fill_broker_id,
                    fill_commission,
                    fill_stamp,
                    fill_transfer,
                    fill_other,
                    fill_settle_date,
                    estimate_fees,
                ],
                outputs=[fill_status],
            ).then(
                _refresh_account_bundle,
                inputs=[account_date],
                outputs=[account_summary, positions_table, fills_table],
            )

            import_evt = import_button.click(
                import_fills_action,
                inputs=[fills_file, import_estimate],
                outputs=[import_status],
            ).then(
                _refresh_account_bundle,
                inputs=[account_date],
                outputs=[account_summary, positions_table, fills_table],
            )
            fills_refresh.click(_refresh_account_bundle, inputs=[account_date], outputs=[account_summary, positions_table, fills_table])

        with gr.Accordion("4️⃣ 收盘账户对账", open=True):
            gr.Markdown(
                "先预览差异，再勾选确认。确认后以券商收盘快照覆盖账本；未完成信号日对账的计划不能审批。"
            )
            reconcile_payload = gr.Code(
                label="券商账户快照 JSON",
                language="json",
                value=reconciliation_template(today),
                lines=14,
            )
            with gr.Row():
                reconcile_confirm = gr.Checkbox(label="确认以券商快照覆盖账本", value=False)
                reconcile_resolution = gr.Textbox(label="差异说明 / 处理结论")
                reconcile_button = gr.Button("执行对账", variant="primary")
            reconcile_status = gr.Markdown()
            reconcile_diff = gr.Dataframe(interactive=False)
            reconcile_evt = reconcile_button.click(
                reconcile_action,
                inputs=[reconcile_payload, reconcile_confirm, reconcile_resolution],
                outputs=[reconcile_status, reconcile_diff],
            ).then(
                _refresh_account_bundle,
                inputs=[account_date],
                outputs=[account_summary, positions_table, fills_table],
            )

        with gr.Accordion("5️⃣ 计划与成交偏差复盘", open=True):
            execution_summary = gr.Markdown(value=initial_execution_md)
            execution_table = gr.Dataframe(value=initial_execution, interactive=False, max_height=360)
            review_button = gr.Button("🔎 刷新执行复盘")
            review_button.click(
                execution_view,
                inputs=[plan_selector],
                outputs=[execution_summary, execution_table],
            )

        with gr.Accordion("🧪 Paper Broker 模拟执行（券商 API 联调）", open=False):
            gr.Markdown(
                "> 把已审批计划的订单提交给内存模拟券商（PaperBroker），按参考价±滑点模拟成交，"
                "再把成交回报**统一写入交易账本**（与人工录入同一入账管线，来源 `PAPER_BROKER`）。"
                "用于验证订单状态机与回报入账链路；接真实券商时仅替换 Adapter，不动账本。"
            )
            with gr.Row():
                paper_exec_date = gr.Textbox(label="模拟成交日期", value=today)
                paper_slip = gr.Number(label="不利滑点(bps)", value=10.0)
                paper_button = gr.Button("🚀 模拟执行所选计划", variant="secondary")
            paper_status = gr.Markdown()
            paper_evt = paper_button.click(
                paper_trade_action,
                inputs=[plan_selector, paper_exec_date, paper_slip],
                outputs=[paper_status],
            )
        # ===== 计划面板级联刷新：所有组件创建后统一追加 =====
        # 背景（2026-08-31 体验审查）：审批/取消/调减/成交/对账改变计划与账本状态后，
        # 列表、详情、复盘、账户面板需要同步刷新，否则用户以为操作未生效。
        # 注意：execution_summary/execution_table 在第 5 区创建，
        # 因此计划面板的 .then 必须在此处（组件就绪后）追加，而非注册动作时。
        plan_bundle_outputs = [
            plans_table, plan_selector, plan_summary, orders_table,
            execution_summary, execution_table,
        ]
        for evt in (adjust_evt, approve_evt, cancel_evt, fill_evt, import_evt, reconcile_evt, paper_evt):
            evt.then(
                _refresh_plan_bundle,
                inputs=[plan_selector, account_date],
                outputs=plan_bundle_outputs,
            )

        plan_selector.change(
            lambda choice: (*plan_view(choice), *execution_view(choice)),
            inputs=[plan_selector],
            outputs=[plan_summary, orders_table, execution_summary, execution_table],
        )


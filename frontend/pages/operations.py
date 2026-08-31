"""操作中心：把常用 CLI 任务迁移为参数化、安全白名单前端。"""
from __future__ import annotations

import queue

import gradio as gr

from api.strategy_api import live_signal_choices, strategy_choices
from api.task_api import task_queue
from frontend.theme import page_header


def _stream_operation(task_id: str, extra_args: list[str], title: str):
    events: queue.Queue = queue.Queue()

    def on_output(instance_id: str, line: str) -> None:
        events.put(("output", instance_id, line))

    def on_complete(instance_id: str, code: int) -> None:
        events.put(("complete", instance_id, code))

    ok, message, instance_id = task_queue.submit(
        task_id,
        on_output=on_output,
        on_complete=on_complete,
        extra_args=extra_args,
    )
    if not ok:
        yield f"❌ {message}"
        return

    yield f"🟡 **{title}** 已进入队列。"
    while True:
        try:
            kind, event_id, payload = events.get(timeout=1.0)
        except queue.Empty:
            yield f"🟡 **{title}** 运行中\n\n```text\n{task_queue.get_output(instance_id, tail=60)}\n```"
            continue
        if event_id != instance_id:
            continue
        output = task_queue.get_output(instance_id, tail=60)
        if kind == "complete":
            icon = "✅" if payload == 0 else "❌"
            yield f"{icon} **{title}** {'完成' if payload == 0 else f'失败（code={payload}）'}\n\n```text\n{output}\n```"
            return
        yield f"🟡 **{title}** 运行中\n\n```text\n{output}\n```"


def _run_refresh(
    universe: str,
    index_code: str,
    start: str,
    max_symbols: float | None,
    keep_st: bool,
    full_refresh: bool,
    confirm_full_refresh: bool,
):
    if full_refresh and not confirm_full_refresh:
        yield "❌ 全量刷新会重拉并覆盖所选股票历史，请先勾选确认。"
        return
    extra = ["--universe", universe, "--index", index_code, "--start", start]
    if max_symbols:
        extra += ["--max", str(int(max_symbols))]
    if keep_st:
        extra.append("--keep-st")
    if full_refresh:
        extra.append("--full-refresh")
    title = "全量数据刷新" if full_refresh else "增量数据刷新"
    yield from _stream_operation("refresh", extra, title)


def _run_signal(strategy: str, trade_date: str, no_push: bool):
    extra = ["--strategy", strategy]
    normalized_trade_date = str(trade_date or "").strip()
    if normalized_trade_date:
        extra += ["--trade-date", normalized_trade_date]
    if no_push:
        extra.append("--no-push")
    yield from _stream_operation("signal", extra, f"生成 {strategy} T+1 信号")


def _run_sweep(strategy: str, start: str, end: str, combos: str):
    extra = ["--strategy", strategy, "--start", start]
    normalized_end = str(end or "").strip()
    if normalized_end:
        extra += ["--end", normalized_end]
    for combo in str(combos or "").splitlines():
        combo = combo.strip()
        if combo:
            extra += ["--combo", combo]
    yield from _stream_operation("sweep", extra, f"{strategy} 参数扫描")


def _run_quality(jump_threshold: float):
    yield from _stream_operation(
        "data_quality",
        ["--jumps", str(float(jump_threshold))],
        "数据质量扫描",
    )


def _run_universe(index_code: str, describe_only: bool):
    extra = ["--index", index_code]
    if describe_only:
        extra.append("--describe-only")
    yield from _stream_operation("universe_history", extra, "PIT 股票池历史")


def _run_migration(dry_run: bool, confirm_write: bool):
    if not dry_run and not confirm_write:
        yield "❌ 写盘迁移前必须勾选“确认执行写盘迁移”。"
        return
    extra = ["--dry-run"] if dry_run else []
    yield from _stream_operation("migrate_store", extra, "行情存储分区迁移")


def _run_industries():
    yield from _stream_operation("industries", ["--refresh"], "行业映射更新")


def _run_financial_factors():
    yield from _stream_operation("financial_factors", [], "财务因子更新")


def _run_trading_calendar():
    yield from _stream_operation("trading_calendar", [], "交易日历更新")


def _run_indices():
    yield from _stream_operation("update_indices", [], "常用指数更新")


def render() -> None:
    """渲染操作中心。"""
    strategies = strategy_choices()
    live_strategies = live_signal_choices()
    default_strategy = "lowvol_indz" if "lowvol_indz" in strategies else strategies[0]

    with gr.Tab("🧰 操作中心"):
        gr.HTML(page_header("🧰 操作中心", "安全参数表单 / 任务队列 / 日志；无需复制命令行"))
        gr.Markdown(
            "> 前端只能提交后端白名单参数，不能执行任意 shell。长任务可在“策略监控”查看队列、日志并取消。"
        )

        operation_output = gr.Markdown("*选择下方操作开始执行。*")

        with gr.Accordion("📥 数据刷新", open=True):
            with gr.Row():
                refresh_universe = gr.Dropdown(label="股票池", choices=["index", "all"], value="all")
                refresh_index = gr.Textbox(label="指数代码（仅 index 池生效）", value="000300")
                refresh_start = gr.Textbox(label="历史起点 YYYYMMDD", value="20190101")
                refresh_max = gr.Number(label="最多股票数（调试可空）", precision=0)
                refresh_keep_st = gr.Checkbox(label="保留 ST", value=False)
                refresh_full = gr.Checkbox(label="全量重拉并覆盖", value=False)
                refresh_confirm_full = gr.Checkbox(label="确认全量覆盖", value=False)
            gr.Markdown(
                "默认执行增量更新。全量模式会从历史起点重拉所选股票；远端空响应不会删除本地旧数据。"
            )
            refresh_button = gr.Button("🔄 执行数据刷新", variant="primary")
            refresh_button.click(
                _run_refresh,
                inputs=[
                    refresh_universe,
                    refresh_index,
                    refresh_start,
                    refresh_max,
                    refresh_keep_st,
                    refresh_full,
                    refresh_confirm_full,
                ],
                outputs=[operation_output],
            )

        with gr.Accordion("📋 生成每日 T+1 信号", open=True):
            with gr.Row():
                signal_strategy = gr.Dropdown(
                    label="策略（仅正式信号白名单）",
                    choices=live_strategies,
                    value=live_strategies[0],
                )
                signal_trade_date = gr.Textbox(label="计划交易日（节假日前建议填写）", placeholder="2026-09-01")
                signal_no_push = gr.Checkbox(label="不推送钉钉", value=True)
            signal_button = gr.Button("生成信号与 DRAFT 计划", variant="primary")
            signal_button.click(
                _run_signal,
                inputs=[signal_strategy, signal_trade_date, signal_no_push],
                outputs=[operation_output],
            )

        with gr.Accordion("🔍 参数扫描", open=False):
            with gr.Row():
                sweep_strategy = gr.Dropdown(label="策略", choices=strategies, value=default_strategy)
                sweep_start = gr.Textbox(label="起始日期", value="2020-01-01")
                sweep_end = gr.Textbox(label="结束日期（可空）")
            sweep_combos = gr.Textbox(
                label="参数组合（每行一组）",
                lines=5,
                value="top_k=20,rebalance_days=20,rank_buffer=0.5\ntop_k=30,rebalance_days=45,rank_buffer=0.5",
            )
            sweep_button = gr.Button("运行参数扫描")
            sweep_button.click(
                _run_sweep,
                inputs=[sweep_strategy, sweep_start, sweep_end, sweep_combos],
                outputs=[operation_output],
            )

        with gr.Accordion("🧪 数据治理", open=False):
            with gr.Row():
                quality_jump = gr.Number(label="异常跳变阈值", value=0.25)
                quality_button = gr.Button("扫描数据质量")
                universe_index = gr.Textbox(label="PIT 股票池指数", value="000300")
                universe_describe = gr.Checkbox(label="只查看覆盖情况", value=True)
                universe_button = gr.Button("构建/检查股票池历史")
            quality_button.click(_run_quality, inputs=[quality_jump], outputs=[operation_output])
            universe_button.click(
                _run_universe,
                inputs=[universe_index, universe_describe],
                outputs=[operation_output],
            )
            with gr.Row():
                industries_button = gr.Button("更新行业映射")
                financial_button = gr.Button("更新财务因子")
                calendar_button = gr.Button("更新交易日历")
                indices_button = gr.Button("更新常用指数")
            industries_button.click(
                _run_industries,
                outputs=[operation_output],
            )
            financial_button.click(
                _run_financial_factors,
                outputs=[operation_output],
            )
            calendar_button.click(
                _run_trading_calendar,
                outputs=[operation_output],
            )
            indices_button.click(
                _run_indices,
                outputs=[operation_output],
            )

        with gr.Accordion("🗄️ 行情存储迁移", open=False):
            gr.Markdown("默认仅预演。正式写盘前请先备份 `data/`，并确认没有数据刷新任务正在运行。")
            with gr.Row():
                migration_dry_run = gr.Checkbox(label="仅预演（推荐）", value=True)
                migration_confirm = gr.Checkbox(label="确认执行写盘迁移", value=False)
                migration_button = gr.Button("执行存储迁移", variant="stop")
            migration_button.click(
                _run_migration,
                inputs=[migration_dry_run, migration_confirm],
                outputs=[operation_output],
            )

        with gr.Accordion("📡 当前任务队列", open=True):
            queue_status = gr.Textbox(value=task_queue.get_status_summary(), lines=8, interactive=False)
            queue_refresh = gr.Button("🔄 刷新队列")
            queue_refresh.click(task_queue.get_status_summary, outputs=[queue_status])
            gr.Timer(3).tick(task_queue.get_status_summary, outputs=[queue_status])

"""数据总览页面"""
from __future__ import annotations

import queue

import gradio as gr
import plotly.express as px

import data_bus
from api.data_api import (
    get_index_coverage,
    get_stock_data,
    get_stock_list,
    get_stock_names,
    get_stock_stats,
    get_universe,
)
from frontend.theme import metric_card, page_header


def _board_text(stats: dict) -> str:
    return " / ".join(
        f"{board} {count}" for board, count in stats.get("index_boards", {}).items() if count
    ) or "无"


def _freshness_text(stats: dict) -> str:
    freshness = stats.get("freshness_days")
    coverage = float(stats.get("latest_coverage") or 0.0)
    icon = "✅" if freshness is not None and freshness <= 3 and coverage >= 0.8 else "⚠️"
    text = (
        f"> {icon} **数据健康**：最新交易日 {stats.get('latest_date', 'N/A')}，"
        f"当日覆盖 {stats.get('latest_symbols', 0):,}/{stats.get('stock_count', 0):,} 只"
        f"（{coverage:.1%}），距今天 {freshness if freshness is not None else '?'} 个自然日。"
    )
    update = stats.get("last_update") or {}
    if update:
        text += (
            f"\n> 最近刷新：{str(update.get('updated_at', '-'))[:19]} UTC，"
            f"成功 {int(update.get('ok', 0)):,} / 空数据 {int(update.get('empty', 0)):,} / "
            f"失败 {int(update.get('failed', 0)):,}。"
        )
    return text


def render():
    """渲染数据总览 Tab"""
    with gr.Tab("🗃️ 数据总览"):
        gr.HTML(page_header("🗃️ 数据总览", "股票池状态 / 数据覆盖 / 市场概览"))

        # ---- 并发数据刷新入口（主板全量，后台任务）----
        with gr.Accordion("🔄 并发数据刷新（拉取所有主板股票）", open=False):
            gr.Markdown(
                "*选择股票池后以并发方式拉取。主板全量 ~3000 只，"
                "默认 8 并发，可在下方调整。任务在后台执行，进度见「📡 策略监控」。*"
            )
            with gr.Row():
                upd_universe = gr.Dropdown(
                    label="股票池",
                    choices=["mainboard", "index", "all"],
                    value="mainboard",
                    info="mainboard=沪深主板全量 | index=指数成分股 | all=全市场",
                )
                upd_workers = gr.Number(
                    label="并发数（1-32）", value=8, precision=0, minimum=1, maximum=32,
                )
                upd_full = gr.Checkbox(label="全量重拉（replace 历史）", value=False)
            upd_btn = gr.Button("🚀 并发刷新", variant="primary")
            upd_out = gr.Markdown()

            def _submit_refresh(universe: str, workers: float, full: bool):
                from api.task_api import TASKS, task_queue

                if "refresh" not in TASKS:
                    return "❌ 未找到刷新任务定义"
                try:
                    w = int(workers) if workers else 8
                except Exception:
                    return "❌ 并发数必须为整数"
                extra = ["--universe", universe, "--workers", str(w)]
                if full:
                    extra += ["--full"]

                q: queue.Queue = queue.Queue()

                def _on_output(tid, line):
                    q.put(("out", tid, line))

                def _on_complete(tid, code):
                    q.put(("done", tid, code))

                ok, msg, instance_id = task_queue.submit(
                    "refresh", on_output=_on_output, on_complete=_on_complete,
                    extra_args=extra,
                )
                if not ok:
                    return f"⚠️ {msg}"

                lines = [
                    f"🚀 已提交并发刷新：**{universe}** | 并发 {w} | "
                    f"{'全量重拉' if full else '增量'}",
                    "",
                ]
                import time

                deadline = time.time() + 3600
                while time.time() < deadline:
                    try:
                        kind, tid, payload = q.get(timeout=2)
                    except Exception:
                        yield "\n".join(lines[-60:])
                        continue
                    if tid != instance_id:
                        continue
                    if kind == "out":
                        lines.append(str(payload).rstrip())
                    elif kind == "done":
                        lines.append("")
                        lines.append(
                            f"{'✅ 刷新完成' if payload == 0 else f'❌ 刷新失败 (code={payload})'}"
                        )
                        yield "\n".join(lines[-60:])
                        return
                    yield "\n".join(lines[-60:])

                yield "\n".join(lines[-60:]) + "\n\n⏱️ 等待超时，任务仍在后台，见「📡 策略监控」"

            upd_btn.click(
                _submit_refresh,
                inputs=[upd_universe, upd_workers, upd_full],
                outputs=[upd_out],
            )

        gr.Markdown("---")

        stats = get_stock_stats()
        with gr.Row():
            m1 = gr.HTML(metric_card("股票数量", f"{stats['stock_count']:,}", "blue"))
            m2 = gr.HTML(metric_card("股票池快照", str(stats['universe_count']), "green"))
            m3 = gr.HTML(metric_card("指数数量", str(stats['index_count']), "purple"))
            m4 = gr.HTML(metric_card("最新交易日", stats.get('latest_date', 'N/A'), "teal"))
            m5 = gr.HTML(metric_card("最新日覆盖", f"{stats.get('latest_coverage', 0):.1%}", "orange"))
        board_md = gr.Markdown(f"> **指数板块覆盖**：{_board_text(stats)}（明细见下方表格）")
        freshness_md = gr.Markdown(_freshness_text(stats))

        def _refresh_overview():
            s = get_stock_stats()
            return (
                metric_card("股票数量", f"{s['stock_count']:,}", "blue"),
                metric_card("股票池快照", str(s['universe_count']), "green"),
                metric_card("指数数量", str(s['index_count']), "purple"),
                metric_card("最新交易日", s.get('latest_date', 'N/A'), "teal"),
                metric_card("最新日覆盖", f"{s.get('latest_coverage', 0):.1%}", "orange"),
                f"> **指数板块覆盖**：{_board_text(s)}（明细见下方表格）",
                _freshness_text(s),
                get_universe(),
            )

        refresh_btn = gr.Button("🔄 刷新数据概览", size="sm")
        universe_df = gr.Dataframe(value=get_universe(), interactive=False)
        refresh_btn.click(
            _refresh_overview,
            outputs=[m1, m2, m3, m4, m5, board_md, freshness_md, universe_df],
        )
        gr.Timer(60).tick(
            _refresh_overview,
            outputs=[m1, m2, m3, m4, m5, board_md, freshness_md, universe_df],
        )

        gr.Markdown("---")

        with gr.Accordion("📊 指数覆盖（按板块分类）", open=False):
            gr.Markdown(
                "*口径说明：**指数数量 = 已覆盖指数个数**（与股票数量的「唯一代码数」口径一致）；"
                f"实际指数日线文件 {stats.get('index_file_count', '-')} 个（按年分区，上证指数历史可回溯至 1990 年）。"
                "⬜ 未拉取时可在 🧰 操作中心运行「更新常用指数」批量补齐。*"
            )
            coverage = get_index_coverage()
            coverage_df = gr.Dataframe(value=coverage, interactive=False, max_height=320)

        # ===== 跨页联动：任务完成 → 自动刷新本页数据（版本门控，未变化时不产生流量） =====
        seen_state = gr.State(data_bus.current())

        def _poll_data_version(seen_val: int):
            changed, cur = data_bus.poll(seen_val)
            if not changed:
                return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), seen_val
            refreshed = _refresh_overview()
            return *refreshed, get_index_coverage(), cur

        gr.Timer(5).tick(
            _poll_data_version,
            inputs=[seen_state],
            outputs=[m1, m2, m3, m4, board_md, universe_df, coverage_df, seen_state],
        )

        gr.Markdown("---")

        with gr.Accordion("📈 股票日线数据", open=True):
            stock_list = get_stock_list()
            default_symbol = "000001"
            top100 = stock_list[:100]
            if default_symbol in stock_list and default_symbol not in top100:
                dropdown_choices = [default_symbol, *top100]
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

                name_map = get_stock_names()

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

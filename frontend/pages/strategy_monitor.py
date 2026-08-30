"""策略监控页面 - 任务队列/运行状态/持仓分析"""
from __future__ import annotations

import queue
from datetime import datetime, timedelta

import gradio as gr
import pandas as pd

from api.manual_trading_api import latest_prices, manual_settings, repository
from api.strategy_api import (
    STRATEGY_META,
    strategy_catalog,
)
from api.strategy_api import (
    strategy_choices as _strategy_choices,
)
from api.task_api import TASKS, get_task_artifacts, task_queue
from common import load_stock_names
from frontend.theme import metric_card, page_header


# ---------- 任务执行（流式+队列） ----------

def on_run_task(task_id: str, strategy: str = ""):
    """提交任务到队列并流式显示进度，完成后显示产出结果"""
    if task_id not in TASKS:
        yield "❌ 未知任务", task_queue.get_status_summary(), "", ""
        return

    q: queue.Queue = queue.Queue()

    def _on_output(tid: str, line: str):
        q.put(("out", tid, line))

    def _on_complete(tid: str, code: int):
        q.put(("done", tid, code))

    # 回测任务：附加策略参数
    extra_args = None
    display_name = TASKS[task_id]["name"]
    if task_id == "backtest" and strategy:
        extra_args = ["--strategy", strategy]
        display_name = f"运行回测 [{strategy}]"

    ok, msg, instance_id = task_queue.submit(task_id, on_output=_on_output, on_complete=_on_complete,
                                             extra_args=extra_args)
    if not ok:
        yield msg, task_queue.get_status_summary(), f"⚠️ {msg}", ""
        return

    my_done = False
    idle_ticks = 0

    while True:
        try:
            kind, tid, _payload = q.get(timeout=0.5)
            # 匹配本次提交的实例 ID（第二次提交同名任务是 'xxx#2'，不能用族 ID 全等匹配）
            if tid == instance_id and kind == "done":
                my_done = True
        except queue.Empty:
            pass

        has_active = any(
            t.status.value in ("running", "pending")
            for t in task_queue.tasks.values()
        )
        output = task_queue.get_output(instance_id, tail=40)
        status_summary = task_queue.get_status_summary()

        if my_done:
            my_task = task_queue.tasks.get(instance_id)
            code = my_task.returncode if my_task else -1
            icon = "✅" if code == 0 else "❌"
            yield output, status_summary, (
                f"{icon} {display_name} {'完成' if code == 0 else f'失败(code={code})'}"), ""
        else:
            yield output, status_summary, f"🟡 {display_name} 运行中...", ""

        if my_done and not has_active:
            break
        if my_done and q.empty():
            idle_ticks += 1
            if idle_ticks > 3:
                break
        elif not my_done:
            idle_ticks = 0

    # 任务完成后：显示产出文件清单
    artifacts = get_task_artifacts(task_id)
    yield (task_queue.get_output(task_id, tail=40),
           task_queue.get_status_summary(),
           "🏁 队列空闲",
           artifacts)


def on_refresh_status():
    """刷新任务状态面板"""
    return task_queue.get_status_summary()


# ---------- 持仓数据 ----------

def _get_holdings_data():
    try:
        _, account_name = manual_settings()
        state = repository().account_state(account_name)
    except Exception:
        state = None
    if state is None or not state.positions:
        return None, None

    cash = state.cash_total
    positions = state.total_positions

    stock_names = load_stock_names()
    # 统一走 BarStore 分区查询（2026-08-31 修复：旧 per-symbol parquet 路径已迁移，直读会全部"数据缺失"）
    prices = latest_prices(list(positions))
    pos_data = []
    total_value = cash
    missing = []

    for sym, shares in positions.items():
        price = float(prices.get(sym, 0.0)) or None
        if not price or price <= 0:
            # 缺数据持仓按 price=0 计入会拉低其余持仓权重，单列提示不计入权重分母
            missing.append({"代码": sym, "名称": stock_names.get(sym, "-"),
                            "持股数": shares, "最新价": "数据缺失",
                            "市值": "-", "权重%": "-"})
            continue
        value = shares * price
        total_value += value
        pos_data.append({
            "代码": sym,
            "名称": stock_names.get(sym, "-"),
            "持股数": shares,
            "最新价": round(price, 2),
            "市值": round(value, 2),
            "权重%": 0.0,
        })

    for row in pos_data:
        row["权重%"] = round(float(row["市值"]) / total_value * 100, 1) if total_value > 0 else 0

    pos_df = pd.DataFrame(pos_data + missing).sort_values(
        "市值", ascending=False, key=lambda s: pd.to_numeric(s, errors="coerce").fillna(-1)
    )
    summary = {"cash": cash, "equity": total_value - cash, "total": total_value}
    return pos_df, summary


def render():
    """渲染策略监控 Tab"""
    with gr.Tab("📡 策略监控"):
        gr.HTML(page_header("📡 策略监控", "任务队列 / 并发执行 / 调仓日历 / 持仓分析"))

        # ===== 任务执行 =====
        gr.Markdown("### ⚡ 任务执行（支持队列和并发）")
        gr.Markdown("""
        > 💡 **并发规则**: 数据类任务（🔄刷新/📋信号/🔬因子）同一时间只运行1个；
        > 计算类任务（📈回测/🤖ML/🔍扫描）最多并行2个。
        > 相同任务重复提交会自动去重。
        """)

        with gr.Row():
            btn_refresh = gr.Button("🔄 数据刷新", variant="secondary", size="sm")
            btn_backtest = gr.Button("📈 运行回测", variant="primary", size="sm")
            btn_signal = gr.Button("📋 生成信号", variant="secondary", size="sm")
            btn_ml = gr.Button("🤖 ML训练", variant="secondary", size="sm")
            btn_factor = gr.Button("🔬 因子研究", variant="secondary", size="sm")
            btn_status = gr.Button("🔄 刷新状态", variant="secondary", size="sm")

        # 策略清单单一数据源：REGISTRY 驱动（与回测中心/首页同源，新策略自动出现）
        strategy_choices = _strategy_choices()
        strategy_select = gr.Dropdown(
            label="回测策略选择（仅对 📈 运行回测 生效）",
            choices=strategy_choices,
            value=strategy_choices[0],
            info=" | ".join(f"{k}={v['label']}" for k, v in STRATEGY_META.items()),
        )

        # ===== 策略准入状态 =====
        gr.Markdown("### 🎯 策略准入状态")
        gr.Markdown(
            "> **准入** = 允许生成正式 T+1 实盘信号（`strategy.live_allowlist`）；"
            "**研究** = 仅回测/扫描，禁止实盘信号。策略升级必须通过回测、成本压力和 Walk-Forward 验证。"
        )
        catalog_rows = [
            {
                "策略": item["label"],
                "标识": item["name"],
                "实盘准入": item["admitted"],
                "研究状态": item["status"],
                "默认调仓(日)": item["default_rebalance"],
                "默认持仓数": item["default_top_k"],
                "说明": item["desc"],
            }
            for item in strategy_catalog()
        ]
        gr.Dataframe(
            value=pd.DataFrame(
                catalog_rows,
                columns=["策略", "标识", "实盘准入", "研究状态", "默认调仓(日)", "默认持仓数", "说明"],
            ),
            interactive=False,
            max_height=300,
        )

        with gr.Row():
            task_status_bar = gr.Textbox(label="执行状态", interactive=False)
            cancel_select = gr.Dropdown(label="选择要取消的任务实例",
                                        choices=[], interactive=True)
            btn_cancel = gr.Button("⛔ 取消任务", variant="stop", size="sm")

        queue_status = gr.Textbox(label="📋 任务队列", lines=6, interactive=False,
                                  value=task_queue.get_status_summary())
        task_output = gr.Textbox(label="最新任务输出（日志）", lines=10, interactive=False)
        task_artifacts = gr.Markdown(value="*任务完成后此处显示产出文件清单和结果位置*",
                                     label="📦 任务产出")

        def _active_choices():
            return [
                f"{t.task_id}（{t.name}·{t.status.value}）"
                for t in sorted(task_queue.tasks.values(), key=lambda x: x.created_at, reverse=True)
                if t.status.value in ("running", "pending")
            ]

        def _on_cancel(selection: str):
            if not selection:
                return task_queue.get_status_summary(), "未选择任务", _active_choices()
            instance_id = selection.split("（")[0].strip()
            ok, msg = task_queue.cancel(instance_id)
            return (task_queue.get_status_summary(),
                    ("✅ " if ok else "⚠️ ") + msg,
                    _active_choices())

        btn_cancel.click(_on_cancel, inputs=[cancel_select],
                         outputs=[queue_status, task_status_bar, cancel_select])

        btn_refresh.click(on_run_task, inputs=[gr.State("refresh"), strategy_select],
                          outputs=[task_output, queue_status, task_status_bar, task_artifacts])
        btn_backtest.click(on_run_task, inputs=[gr.State("backtest"), strategy_select],
                           outputs=[task_output, queue_status, task_status_bar, task_artifacts])
        btn_signal.click(on_run_task, inputs=[gr.State("signal"), strategy_select],
                         outputs=[task_output, queue_status, task_status_bar, task_artifacts])
        btn_ml.click(on_run_task, inputs=[gr.State("ml_train"), strategy_select],
                     outputs=[task_output, queue_status, task_status_bar, task_artifacts])
        btn_factor.click(on_run_task, inputs=[gr.State("factor_research"), strategy_select],
                         outputs=[task_output, queue_status, task_status_bar, task_artifacts])
        btn_status.click(on_refresh_status, outputs=[queue_status])

        # 自动刷新队列状态 + 跟随最新任务日志（每3秒）
        timer = gr.Timer(3)
        timer.tick(on_refresh_status, outputs=[queue_status])
        timer.tick(
            lambda: task_queue.get_output(
                max(task_queue.tasks.values(), key=lambda t: t.created_at).task_id, tail=40)
            if task_queue.tasks else "",
            outputs=[task_output],
        )
        timer.tick(_active_choices, outputs=[cancel_select])

        gr.Markdown("---")

        # ===== 调仓日历 =====
        with gr.Accordion("📅 调仓日历", open=True):
            try:
                import yaml
                with open("config/settings.yaml", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                rebalance_days = cfg.get("strategy", {}).get("rebalance_days", 5)
            except Exception:
                rebalance_days = 5

            today = datetime.now().date()
            next_reb = today
            added = 0
            while added < rebalance_days:
                next_reb += timedelta(days=1)
                if next_reb.weekday() < 5:
                    added += 1

            with gr.Row():
                gr.HTML(metric_card("调仓周期", f"{rebalance_days} 交易日", "blue"))
                gr.HTML(metric_card("下次调仓", next_reb.strftime("%Y-%m-%d"), "green"))
                gr.HTML(metric_card("倒计时", f"{(next_reb - today).days} 天", "orange"))

        gr.Markdown("---")

        # ===== 持仓分析 =====
        with gr.Accordion("💰 当前持仓分析", open=True):
            pos_df, summary = _get_holdings_data()

            if summary:
                with gr.Row():
                    gr.HTML(metric_card("现金", f"{summary['cash']:,.0f} CNY", "green"))
                    gr.HTML(metric_card("持仓市值", f"{summary['equity']:,.0f} CNY", "blue"))
                    gr.HTML(metric_card("账户总值", f"{summary['total']:,.0f} CNY", "purple"))
                gr.Dataframe(value=pos_df, interactive=False)

                gr.Markdown("### 🛡️ 风控规则")
                gr.Markdown("""
                | 参数 | 当前值 | 含义 |
                |------|--------|------|
                | `max_position_pct` | 25% | 单只股票最大持仓权重 |
                | `max_daily_loss_pct` | 5% | 单日最大亏损（触发止损） |
                | `min_avg_amount` | 5000万 | 最低日均成交额（流动性门槛） |
                """)
            else:
                gr.Info("当前无持仓")

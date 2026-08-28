"""策略监控页面 - 任务队列/运行状态/持仓分析"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import gradio as gr

from api.task_api import TASKS, task_queue
from common import load_stock_names
from frontend.theme import metric_card, page_header


# ---------- 任务执行（流式+队列） ----------

def on_run_task(task_id: str):
    """提交任务到队列并流式显示进度"""
    if task_id not in TASKS:
        yield "❌ 未知任务", task_queue.get_status_summary(), ""
        return

    q: queue.Queue = queue.Queue()

    def _on_output(tid: str, line: str):
        q.put(("out", tid, line))

    def _on_complete(tid: str, code: int):
        q.put(("done", tid, code))

    ok, msg = task_queue.submit(task_id, on_output=_on_output, on_complete=_on_complete)
    if not ok:
        yield msg, task_queue.get_status_summary(), f"⚠️ {msg}"
        return

    task_name = TASKS[task_id]["name"]
    my_done = False
    idle_ticks = 0

    while True:
        try:
            kind, tid, payload = q.get(timeout=0.5)
            if tid == task_id and kind == "done":
                my_done = True
        except queue.Empty:
            pass

        has_active = any(
            t.status.value in ("running", "pending")
            for t in task_queue.tasks.values()
        )
        output = task_queue.get_output(task_id, tail=40)
        status_summary = task_queue.get_status_summary()

        if my_done:
            my_task = task_queue.tasks.get(task_id)
            code = my_task.returncode if my_task else -1
            icon = "✅" if code == 0 else "❌"
            yield output, status_summary, (
                f"{icon} {task_name} {'完成' if code == 0 else f'失败(code={code})'}")
        else:
            yield output, status_summary, f"🟡 {task_name} 运行中..."

        if my_done and not has_active:
            break
        if my_done and q.empty():
            idle_ticks += 1
            if idle_ticks > 3:
                break
        elif not my_done:
            idle_ticks = 0

    yield task_queue.get_output(task_id, tail=40), task_queue.get_status_summary(), "🏁 队列空闲"


def on_refresh_status():
    """刷新任务状态面板"""
    return task_queue.get_status_summary()


# ---------- 持仓数据 ----------

def _get_holdings_data():
    holdings_path = "state/holdings.json"
    if not os.path.exists(holdings_path):
        return None, None

    with open(holdings_path, encoding="utf-8") as f:
        holdings = json.load(f)

    cash = holdings.get("cash", 0)
    positions = holdings.get("positions", {})
    if not positions:
        return None, None

    stock_names = load_stock_names()
    pos_data = []
    total_value = cash

    for sym, shares in positions.items():
        price = 0
        daily_path = f"data/daily/{sym}.parquet"
        if os.path.exists(daily_path):
            try:
                df = pd.read_parquet(daily_path)
                price = df["close"].iloc[-1]
            except Exception:
                pass
        value = shares * price
        total_value += value
        pos_data.append({
            "代码": sym,
            "名称": stock_names.get(sym, "-"),
            "持股数": shares,
            "最新价": round(price, 2),
            "市值": round(value, 2),
            "权重%": round(value / total_value * 100, 1) if total_value > 0 else 0,
        })

    pos_df = pd.DataFrame(pos_data).sort_values("市值", ascending=False)
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

        task_status_bar = gr.Textbox(label="执行状态", interactive=False)
        queue_status = gr.Textbox(label="📋 任务队列", lines=8, interactive=False,
                                  value=task_queue.get_status_summary())
        task_output = gr.Textbox(label="最新任务输出", lines=12, interactive=False)

        btn_refresh.click(on_run_task, inputs=[gr.State("refresh")],
                          outputs=[task_output, queue_status, task_status_bar])
        btn_backtest.click(on_run_task, inputs=[gr.State("backtest")],
                           outputs=[task_output, queue_status, task_status_bar])
        btn_signal.click(on_run_task, inputs=[gr.State("signal")],
                         outputs=[task_output, queue_status, task_status_bar])
        btn_ml.click(on_run_task, inputs=[gr.State("ml_train")],
                     outputs=[task_output, queue_status, task_status_bar])
        btn_factor.click(on_run_task, inputs=[gr.State("factor_research")],
                         outputs=[task_output, queue_status, task_status_bar])
        btn_status.click(on_refresh_status, outputs=[queue_status])

        # 自动刷新队列状态（每3秒）
        timer = gr.Timer(3)
        timer.tick(on_refresh_status, outputs=[queue_status])

        gr.Markdown("---")

        # ===== 调仓日历 =====
        gr.Markdown("### 📅 调仓日历")
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
        gr.Markdown("### 💰 当前持仓分析")
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

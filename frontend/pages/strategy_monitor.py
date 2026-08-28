"""策略监控页面 - 任务执行/运行状态/持仓分析"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timedelta

import pandas as pd
import gradio as gr

from api.task_api import TASKS, get_task_status, run_task
from common import load_stock_names
from frontend.theme import metric_card, page_header


# 任务执行回调（流式输出）
def on_run_task(task_id: str):
    """运行任务并流式输出"""
    import queue
    import threading

    if task_id not in TASKS:
        yield "❌ 未知任务", "❌"
        return

    task = TASKS[task_id]
    q: queue.Queue = queue.Queue()
    done = threading.Event()

    def on_output(line: str):
        q.put(("out", line))

    def on_complete(code: int):
        q.put(("done", code))
        done.set()

    started = run_task(task_id, on_output=on_output, on_complete=on_complete)
    if not started:
        yield "已有任务在运行，请等待完成", "⚠️ 任务冲突"
        return

    lines = []
    result = None
    while result is None:
        try:
            kind, payload = q.get(timeout=0.3)
            if kind == "out":
                lines.append(payload)
                yield "\n".join(lines[-60:]), f"🟡 {task['name']} 运行中..."
            elif kind == "done":
                result = payload
        except queue.Empty:
            if not result:
                yield "\n".join(lines[-60:]), f"🟡 {task['name']} 运行中..."

    status_text = f"✅ {task['name']} 完成" if result == 0 else f"❌ {task['name']} 失败 (code={result})"
    yield "\n".join(lines[-60:]), status_text


def _get_holdings_data():
    """获取持仓数据（业务逻辑在页面层封装为纯数据）"""
    holdings_path = "state/holdings.json"
    if not os.path.exists(holdings_path):
        return None, None, None

    with open(holdings_path, encoding="utf-8") as f:
        holdings = json.load(f)

    cash = holdings.get("cash", 0)
    positions = holdings.get("positions", {})
    if not positions:
        return None, None, None

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
    summary = {
        "cash": cash,
        "equity": total_value - cash,
        "total": total_value,
    }
    return pos_df, summary, None


def render():
    """渲染策略监控 Tab"""
    with gr.Tab("📡 策略监控"):
        gr.HTML(page_header("📡 策略监控", "任务执行 / 调仓日历 / 持仓分析"))

        # ===== 任务执行 =====
        gr.Markdown("### ⚡ 任务执行")

        with gr.Row():
            btn_refresh = gr.Button("🔄 数据刷新", variant="secondary")
            btn_backtest = gr.Button("📈 运行回测", variant="primary")
            btn_signal = gr.Button("📋 生成信号", variant="secondary")
            btn_ml = gr.Button("🤖 ML训练", variant="secondary")
            btn_factor = gr.Button("🔬 因子研究", variant="secondary")

        task_status = gr.Textbox(label="状态", interactive=False)
        task_output = gr.Textbox(label="任务输出", lines=15, interactive=False)

        btn_refresh.click(on_run_task, inputs=[gr.State("refresh")],
                          outputs=[task_output, task_status])
        btn_backtest.click(on_run_task, inputs=[gr.State("backtest")],
                           outputs=[task_output, task_status])
        btn_signal.click(on_run_task, inputs=[gr.State("signal")],
                         outputs=[task_output, task_status])
        btn_ml.click(on_run_task, inputs=[gr.State("ml_train")],
                     outputs=[task_output, task_status])
        btn_factor.click(on_run_task, inputs=[gr.State("factor_research")],
                         outputs=[task_output, task_status])

        gr.Markdown("---")

        # ===== 调仓日历 =====
        gr.Markdown("### 📅 调仓日历")
        import yaml
        try:
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
        pos_df, summary, _ = _get_holdings_data()

        if summary:
            with gr.Row():
                gr.HTML(metric_card("现金", f"{summary['cash']:,.0f} CNY", "green"))
                gr.HTML(metric_card("持仓市值", f"{summary['equity']:,.0f} CNY", "blue"))
                gr.HTML(metric_card("账户总值", f"{summary['total']:,.0f} CNY", "purple"))
            gr.Dataframe(value=pos_df, interactive=False)

            # 风控规则
            gr.Markdown("### 🛡️ 风控规则")
            gr.Markdown(f"""
            | 参数 | 当前值 | 含义 |
            |------|--------|------|
            | `max_position_pct` | 25% | 单只股票最大持仓权重 |
            | `max_daily_loss_pct` | 5% | 单日最大亏损（触发止损） |
            | `min_avg_amount` | 5000万 | 最低日均成交额（流动性门槛） |
            """)
        else:
            gr.Info("当前无持仓")

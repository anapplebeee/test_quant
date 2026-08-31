"""每日信号页面：信号报告 + 交易计划详情 + ML 预测分数。

功能：
- 信号报告：查看历史每日信号 markdown 报告
- 计划详情：展示明日交易计划（买卖方向和数量）
- 计划审批：跳转手动交易页签审批
"""
from __future__ import annotations

import os
from datetime import date

import gradio as gr
import pandas as pd

import data_bus
from api.manual_trading_api import plans_view, plan_view
from common import reports_dir
from frontend.theme import page_header


def _load_signal(date: str) -> str:
    """加载信号报告（路径走 common.reports_dir()，避免配置根目录漂移）"""
    from common import safe_path, valid_date8

    if not valid_date8(date):
        return "非法日期格式"
    path = safe_path(reports_dir(), f"signal_{date}.md")
    if path is not None and path.exists():
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "未找到信号报告"


def _snapshot():
    """扫描信号文件，返回 (日期选项, 最新日期, 最新内容)"""
    # 注意：glob 返回 Path，Path.replace() 是「重命名文件」而非字符串替换。
    # 必须先取 .stem（已去掉 .md 后缀）再做字符串处理。
    signal_files = sorted([
        f.stem.replace("signal_", "")
        for f in reports_dir().glob("signal_*.md")
    ])
    if not signal_files:
        return [], None, "暂无信号报告，运行 scripts/daily_signal.py 生成"
    latest = signal_files[-1]
    return signal_files, latest, _load_signal(latest)


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

            # 计划详情切换
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
            choices2, latest2, content2 = _snapshot()
            signal_date = gr.Dropdown(label="选择日期", choices=choices2, value=latest2)
            signal_content = gr.Markdown(value=content2)
            signal_date.change(_load_signal, inputs=signal_date, outputs=signal_content)

            def _refresh():
                c, v, txt = _snapshot()
                return gr.update(choices=c, value=v), txt

            refresh_btn = gr.Button("🔄 刷新信号列表", size="sm")
            refresh_btn.click(_refresh, outputs=[signal_date, signal_content])

        gr.Markdown("---")

        # ===== ML 预测分数 =====
        with gr.Accordion("🤖 ML 预测分数 (最新)", open=False):
            scores_path = os.path.join("data", "scores", "preds.csv")
            scores_df = None
            if os.path.exists(scores_path):
                try:
                    df = pd.read_csv(scores_path)
                    if "datetime" in df.columns:
                        df = df.sort_values("datetime", ascending=False)
                    if len(df) > 50:
                        df = df.head(50)
                    scores_df = df
                except Exception:
                    scores_df = None
            if scores_df is None:
                gr.Markdown("*暂无 ML 预测数据。运行 🤖 ML 训练 生成 `data/scores/preds.csv`*")
            preds_table = gr.Dataframe(value=scores_df, interactive=False, max_height=400)

        # ===== 跨页联动：任务完成 → 自动刷新计划/信号/预测（版本门控） =====
        seen_state = gr.State(data_bus.current())

        def _poll_data_version(seen_val: int, plan_date_val: str, sig_sel: str | None):
            changed, cur = data_bus.poll(seen_val)
            if not changed:
                return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), seen_val
            # 计划面板（保留当前选择）
            f, sel_update, sel_val = _plan_snapshot(plan_date_val)
            md, ords = plan_view(sel_val)
            # 信号报告（保留当前选择）
            c, v, txt = _snapshot()
            sig_update = gr.update(choices=c, value=v if v in c else (sig_sel if sig_sel in c else v))
            # ML 预测分数
            preds = None
            if os.path.exists(scores_path):
                try:
                    p = pd.read_csv(scores_path)
                    if "datetime" in p.columns:
                        p = p.sort_values("datetime", ascending=False)
                    preds = p.head(50)
                except Exception:
                    preds = None
            return f, sel_update, md, ords, sig_update, txt, preds, cur

        gr.Timer(5).tick(
            _poll_data_version,
            inputs=[seen_state, plan_date, signal_date],
            outputs=[plans_table, plan_selector, plan_detail_md, orders_table,
                     signal_date, signal_content, preds_table, seen_state],
        )

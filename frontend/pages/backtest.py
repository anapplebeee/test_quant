"""回测中心页面"""
from __future__ import annotations

import queue
import time

import pandas as pd
import plotly.graph_objects as go
import gradio as gr

from api.backtest_api import (
    get_backtest_list,
    get_backtest_summary,
    get_cost_breakdown,
    get_equity_curve,
    get_trades,
)
from api.research_api import list_research_reports, list_sweeps, load_research_report, load_sweep, sweep_headline
from api.strategy_api import get_strategy_defaults, strategy_choices
from api.task_api import TASKS, task_queue
from frontend.theme import metric_card, page_header

# 策略清单单一数据源：REGISTRY 驱动（与首页/策略监控同源）
try:
    STRATEGY_CHOICES = strategy_choices()
except Exception:
    STRATEGY_CHOICES = ["momentum_rotation", "lowvol_composite", "dual_ma", "ml_rank", "lowvol_indz"]


def _strategy_defaults(strategy: str) -> dict:
    """该策略当前生效的默认参数（overrides 优先，与 build_strategy 同语义）。"""
    try:
        return get_strategy_defaults(strategy)
    except Exception:
        return {"rebalance_days": 5, "top_k": 10}


def _cost_md(name: str) -> str:
    """交易成本分解：引擎已含佣金/印花税/滑点，此前页面不可见。"""
    c = get_cost_breakdown(name)
    if not c:
        return "*成本明细：无交易记录*"

    s = get_backtest_summary(name) or {}
    # 年化口径：默认 100 万初始资金，区间年数按 start/end 折算
    years = 1.0
    if s.get("start") and s.get("end"):
        try:
            years = max((pd.Timestamp(s["end"]) - pd.Timestamp(s["start"])).days / 365.25, 0.01)
        except Exception:
            years = 1.0
    annual_turn = c["turnover_x"] / years
    slip_ratio = c["slip_cost"] / c["total_fee"] if c["total_fee"] else 0.0

    lines = [
        f"**交易成本合计: {c['total_cost']:,.0f} 元（占初始资金 {c['cost_pct_init'] * 100:.1f}%）**  ",
        f"手续费 {c['total_fee']:,.0f} + 滑点 {c['slip_cost']:,.0f}"
        + (f"（滑点为手续费的 {slip_ratio:.1f} 倍）" if c["total_fee"] else ""),
        f"*累计成交额 {c['turnover_2way'] / 1e4:,.0f} 万元 = 初始资金的 {c['turnover_x']:.1f} 倍"
        f"（年均 {annual_turn:.1f} 倍换手）| 成交 {c['n_trades']} 笔"
        f"（买 {c['n_buy']} / 卖 {c['n_sell']}）*",
    ]
    tr = float(s.get("total_return") or 0.0)
    if tr != 0.0:
        loss_amt = abs(tr) * 1_000_000
        lines.append(f"*成本相当于{'亏损' if tr < 0 else '收益'}额的 {c['total_cost'] / loss_amt * 100:.0f}%*")
    return "  \n".join(lines)


def _load_backtest(name: str):
    """加载指定回测的摘要/图表/交易"""
    if not name:
        return "未选择回测", None, None, None

    # 摘要（旧格式 summary 可能缺键，统一 .get 防整页 KeyError 崩溃）
    s = get_backtest_summary(name)
    if s:
        md = (
            f"**累计收益:** {s.get('total_return',0)*100:.1f}% | "
            f"**年化:** {s.get('cagr',0)*100:.1f}% | "
            f"**夏普:** {s.get('sharpe',0):.2f} | "
            f"**最大回撤:** {s.get('max_drawdown',0)*100:.1f}% | "
            f"**卡玛:** {s.get('calmar', 0):.2f}  \n"
            f"*区间: {s.get('start','')} ~ {s.get('end','')}*"
        )
    else:
        md = "未找到回测摘要"

    # 净值 + 回撤
    equity_df = get_equity_curve(name)
    fig_eq, fig_dd = None, None
    if equity_df is not None:
        cols = [c for c in equity_df.columns if c != "date"]
        fig_eq = go.Figure()
        fig_dd = go.Figure()
        for col in cols:
            series = equity_df[col]
            label = str(col).replace("min_avg_amount=", "日均额≥")
            fig_eq.add_trace(go.Scatter(
                x=equity_df["date"], y=series, mode="lines", name=label,
            ))
            dd = (series - series.cummax()) / series.cummax() * 100
            fig_dd.add_trace(go.Scatter(
                x=equity_df["date"], y=dd, mode="lines", fill="tozeroy", name=label,
            ))
        fig_eq.add_hline(y=1_000_000, line_dash="dash", line_color="gray",
                         annotation_text="初始资金")
        fig_eq.update_layout(title="策略净值", height=400,
                             margin=dict(l=0, r=0, t=40, b=0))
        fig_dd.update_layout(title="回撤 (%)", height=250,
                             margin=dict(l=0, r=0, t=40, b=0))

    md += "\n\n" + _cost_md(name)

    # 交易
    trades_df = get_trades(name)
    trades_display = trades_df.head(30) if trades_df is not None else None

    return md, fig_eq, fig_dd, trades_display


def _run_backtest(strategy: str, rebalance_days: float, top_k: float, start: str):
    """提交回测任务并流式回显日志（换手频率/持仓数等参数由前端传入，覆盖 config）。"""
    if "backtest" not in TASKS:
        yield "❌ 未找到回测任务定义"
        return

    try:
        rb = int(rebalance_days) if rebalance_days else None
        tk = int(top_k) if top_k else None
    except Exception:
        yield "❌ 参数必须为整数"
        return

    extra = ["--strategy", strategy, "--start", (start or "2020-01-01")]
    # 显式参数优先于 config.strategy.overrides（build_strategy 内部合并）
    if rb:
        extra += ["--rebalance-days", str(rb)]
    if tk:
        extra += ["--top-k", str(tk)]

    q: queue.Queue = queue.Queue()

    def _on_output(tid: str, line: str):
        q.put(("out", tid, line))

    def _on_complete(tid: str, code: int):
        q.put(("done", tid, code))

    ok, msg, instance_id = task_queue.submit(
        "backtest", on_output=_on_output, on_complete=_on_complete, extra_args=extra
    )
    if not ok:
        yield f"⚠️ {msg}"
        return

    lines = [
        f"🚀 已提交回测：**{strategy}** | 换手 {rb or '默认'} 日 | "
        f"持仓 {tk or '默认'} | 起始 {start or '2020-01-01'}",
        "",
    ]
    yield "\n".join(lines)

    deadline = time.time() + 600
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
                f"{'✅ 完成' if payload == 0 else f'❌ 失败 (code={payload})'} — 点击上方「🔄 刷新回测列表」查看结果"
            )
            yield "\n".join(lines[-60:])
            return
        yield "\n".join(lines[-60:])

    yield "\n".join(lines[-60:]) + "\n\n⏱️ 已等待 10 分钟，任务仍在后台运行，请到「📡 策略监控」查看"


def render():
    """渲染回测中心 Tab"""
    with gr.Tab("📈 回测中心"):
        gr.HTML(page_header("📈 回测中心", "参数化运行 / 净值曲线 / 回撤分析 / 交易成本 / 交易记录"))

        # ---- 参数面板：策略与关键参数前端可调 ----
        with gr.Accordion("⚙️ 运行新回测（参数可调，覆盖 config）", open=False):
            with gr.Row():
                strategy_in = gr.Dropdown(
                    label="策略", choices=STRATEGY_CHOICES, value=STRATEGY_CHOICES[0],
                )
                rebal_in = gr.Number(
                    label="换手频率（交易日）",
                    value=_strategy_defaults(STRATEGY_CHOICES[0])["rebalance_days"], precision=0,
                )
                topk_in = gr.Number(
                    label="持仓数 top_k",
                    value=_strategy_defaults(STRATEGY_CHOICES[0])["top_k"], precision=0,
                )
                start_in = gr.Textbox(label="起始日期", value="2020-01-01")
            run_btn = gr.Button("🚀 运行回测", variant="primary")
            run_out = gr.Markdown()

            def _on_strategy_change(name: str):
                """切换策略时同步该策略当前生效的默认参数（换手频率/持仓数）"""
                d = _strategy_defaults(name)
                return gr.update(value=d["rebalance_days"]), gr.update(value=d["top_k"])

            strategy_in.change(_on_strategy_change, inputs=[strategy_in], outputs=[rebal_in, topk_in])
            run_btn.click(
                _run_backtest,
                inputs=[strategy_in, rebal_in, topk_in, start_in],
                outputs=[run_out],
            )

        backtest_names = get_backtest_list()
        if not backtest_names:
            gr.Info("未找到回测结果，请先运行回测")
            return

        default_name = backtest_names[-1]
        init = _load_backtest(default_name)

        with gr.Row():
            selected_bt = gr.Dropdown(
                label="选择回测", choices=backtest_names, value=default_name,
                filterable=True,
            )

        def _refresh_list():
            """重建回测列表并加载最新一个（新回测跑完后可刷新发现）"""
            names = get_backtest_list()
            if not names:
                return gr.update(choices=[], value=None), "暂无回测结果", None, None, None
            latest = names[-1]
            md, fe, fd, tr = _load_backtest(latest)
            return gr.update(choices=names, value=latest), md, fe, fd, tr

        summary_md = gr.Markdown(value=init[0])
        equity_plot = gr.Plot(value=init[1])
        dd_plot = gr.Plot(value=init[2])
        trades_table = gr.Dataframe(value=init[3], interactive=False)

        selected_bt.change(
            _load_backtest,
            inputs=[selected_bt],
            outputs=[summary_md, equity_plot, dd_plot, trades_table],
        )

        refresh_btn = gr.Button("🔄 刷新回测列表", size="sm")
        refresh_btn.click(
            _refresh_list,
            outputs=[selected_bt, summary_md, equity_plot, dd_plot, trades_table],
        )

        # ---- 参数扫描结果浏览器：reports/sweep_*.csv（数据关联前端化）----
        with gr.Accordion("🧪 参数扫描结果（reports/sweep_*.csv，按 CAGR 排序）", open=False):
            sweep_files = list_sweeps()
            if sweep_files:
                sweep_dd = gr.Dropdown(
                    label="选择扫描文件", choices=sweep_files, value=sweep_files[-1],
                    filterable=True,
                )
                sweep_tbl = gr.Dataframe(
                    value=sweep_headline(load_sweep(sweep_files[-1])), interactive=False,
                )
                sweep_dd.change(
                    lambda f: sweep_headline(load_sweep(f)),
                    inputs=[sweep_dd], outputs=[sweep_tbl],
                )
            else:
                gr.Markdown("*暂无扫描结果，运行 scripts/sweep.py 后刷新*")

        # ---- 研究报告浏览器：reports/*.md（新验证结论的入口）----
        with gr.Accordion("📚 研究报告（引擎终审/退市回填/调仓周期/缓冲带等验证结论）", open=False):
            report_files = list_research_reports()
            if report_files:
                rep_dd = gr.Dropdown(
                    label="选择报告", choices=report_files, value=report_files[-1],
                    filterable=True,
                )
                rep_md = gr.Markdown(value=load_research_report(report_files[-1]))
                rep_dd.change(load_research_report, inputs=[rep_dd], outputs=[rep_md])
            else:
                gr.Markdown("*暂无研究报告*")

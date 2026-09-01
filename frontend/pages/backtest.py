"""回测中心页面"""
from __future__ import annotations

import queue
import time

import pandas as pd
import plotly.graph_objects as go
import gradio as gr

import data_bus
from api.backtest_api import (
    get_backtest_list,
    get_backtest_summary,
    get_cost_breakdown,
    get_equity_curve,
    get_trades,
    scan_summaries,
)
from api.research_api import list_research_reports, list_sweeps, load_research_report, load_sweep, sweep_headline
from api.strategy_api import get_strategy_defaults, strategy_choices
from api.task_api import TASKS, task_queue
from frontend.components.artifacts_panel import render_artifacts_panel, render_wfa_panel
from frontend.theme import metric_card, page_header

# 策略清单单一数据源：REGISTRY 驱动（与首页/策略监控同源）
try:
    STRATEGY_CHOICES = strategy_choices()
except Exception:
    STRATEGY_CHOICES = [
        "momentum_rotation", "momentum_path", "lowvol_composite", "dual_ma",
        "ml_rank", "lowvol_indz",
    ]


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
                             margin=dict(l=0, r=0, t=40, b=0),
                             template="plotly_white",
                             hovermode="x unified")
        fig_dd.update_layout(title="回撤 (%)", height=250,
                             margin=dict(l=0, r=0, t=40, b=0),
                             template="plotly_white",
                             hovermode="x unified")

    md += "\n\n" + _cost_md(name)

    # 交易（完整记录，不再截断；表格内滚动）
    trades_df = get_trades(name)
    n_trades = len(trades_df) if trades_df is not None else 0
    if trades_df is not None:
        md += f"\n\n**共 {n_trades} 笔成交**（下表为完整记录，可滚动）"

    return md, fig_eq, fig_dd, trades_df


def _stream_task(task_id: str, extra: list[str], header: str, wait_seconds: int = 600):
    """提交任务并流式回显日志（回测 / walk-forward 共用）。

    超时后不撤销任务——它仍在后台跑，提示用户去策略监控看。
    """
    if task_id not in TASKS:
        yield f"❌ 未找到任务定义: {task_id}"
        return

    q: queue.Queue = queue.Queue()

    def _on_output(tid: str, line: str):
        q.put(("out", tid, line))

    def _on_complete(tid: str, code: int):
        q.put(("done", tid, code))

    ok, msg, instance_id = task_queue.submit(
        task_id, on_output=_on_output, on_complete=_on_complete, extra_args=extra
    )
    if not ok:
        yield f"⚠️ {msg}"
        return

    lines = [header, ""]
    yield "\n".join(lines)

    deadline = time.time() + wait_seconds
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
                f"{'✅ 完成' if payload == 0 else f'❌ 失败 (code={payload})'}"
                f" — 点击上方「🔄 刷新」查看结果"
            )
            yield "\n".join(lines[-60:])
            return
        yield "\n".join(lines[-60:])

    yield (
        "\n".join(lines[-60:])
        + f"\n\n⏱️ 已等待 {wait_seconds // 60} 分钟，任务仍在后台运行，请到「📡 策略监控」查看"
    )


def _run_backtest(strategy: str, rebalance_days: float, top_k: float, start: str):
    """提交回测任务（换手频率/持仓数等参数由前端传入，覆盖 config）。"""
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

    header = (
        f"🚀 已提交回测：**{strategy}** | 换手 {rb or '默认'} 日 | "
        f"持仓 {tk or '默认'} | 起始 {start or '2020-01-01'}"
    )
    yield from _stream_task("backtest", extra, header)


def _run_wfa(strategy: str, train: float, test: float, embargo: float,
             metric: str, grid: str, anchored: bool):
    """提交 Walk-Forward 验证任务。"""
    try:
        tr = int(train) if train else None
        te = int(test) if test else None
        em = int(embargo) if embargo else 0
    except Exception:
        yield "❌ 训练/测试/隔离天数必须为整数"
        return

    extra = ["--strategy", strategy]
    if tr:
        extra += ["--train", str(tr)]
    if te:
        extra += ["--test", str(te)]
    extra += ["--embargo", str(em)]
    if metric:
        extra += ["--metric", metric]
    if anchored:
        extra += ["--anchored"]
    # --grid 可重复传入多次；前端用分号分隔多组
    for g in (grid or "").split(";"):
        g = g.strip()
        if g:
            extra += ["--grid", g]

    header = (
        f"🔁 已提交 Walk-Forward：**{strategy}** | train {tr or '默认'} / "
        f"test {te or '默认'} / embargo {em} 日 | 指标 {metric or '默认'}"
        + (" | 锚定窗口" if anchored else "")
    )
    # WFA 要跑 N 折 × 网格组合，比单次回测慢得多，超时后转后台
    yield from _stream_task("walk_forward", extra, header, wait_seconds=900)


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
            run_btn = gr.Button("🚀 运行回测", variant="primary", size="lg")
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

        # ---- 运行 Walk-Forward（样本外验证）----
        with gr.Accordion("🔁 运行 Walk-Forward 验证（样本外 / 过拟合诊断）", open=False):
            gr.Markdown(
                "*每折只在 train 段选参数，再在紧接的 test 段验证；"
                "最后把 test 段拼接成完整的样本外曲线。*"
            )
            with gr.Row():
                wfa_strategy = gr.Dropdown(
                    label="策略", choices=STRATEGY_CHOICES, value=STRATEGY_CHOICES[0],
                )
                wfa_train = gr.Number(label="训练窗口（交易日）", value=504, precision=0)
                wfa_test = gr.Number(label="测试窗口（交易日）", value=126, precision=0)
                wfa_embargo = gr.Number(label="隔离天数（防泄漏）", value=5, precision=0)
            with gr.Row():
                wfa_metric = gr.Dropdown(
                    label="参数选择指标",
                    choices=["sharpe", "cagr", "calmar", "total_return", "bench_excess_cagr"],
                    value="sharpe",
                )
                wfa_grid = gr.Textbox(
                    label="参数网格（多组用 ; 分隔）",
                    placeholder="top_k=10,20,30; rebalance_days=20,45",
                    info="留空 = 固定参数前推，只检验稳健性",
                )
                wfa_anchored = gr.Checkbox(label="锚定窗口（train 逐折变长）", value=False)
            wfa_run_btn = gr.Button("🔁 运行 Walk-Forward", variant="primary", size="lg")
            wfa_run_out = gr.Markdown()
            wfa_run_btn.click(
                _run_wfa,
                inputs=[wfa_strategy, wfa_train, wfa_test, wfa_embargo,
                        wfa_metric, wfa_grid, wfa_anchored],
                outputs=[wfa_run_out],
            )

        # ---- Walk-Forward 过拟合诊断 + 制品追溯 ----
        render_wfa_panel()
        render_artifacts_panel()

        # ---- 回测索引：搜索 + 策略筛选 + 表格选择（替代长下拉）----
        full_df = scan_summaries()
        if full_df.empty:
            gr.Info("未找到回测结果，请先运行回测")
            return

        default_name = full_df["name"].iloc[0]
        init = _load_backtest(default_name)

        # 状态：全量表 + 当前筛选后的名称列表（保证行选 index ↔ name 对齐）
        df_state = gr.State(full_df)

        with gr.Row():
            search_box = gr.Textbox(
                label="搜索（名称 / 区间 / 策略）",
                placeholder="输入关键字过滤…留空显示全部",
                scale=3,
            )
            strategy_choices = ["全部"] + sorted(full_df["strategy"].unique().tolist())
            strat_filter = gr.Dropdown(
                label="策略筛选", choices=strategy_choices, value="全部",
            )
            refresh_btn = gr.Button("🔄 刷新列表", size="sm")

        def _fmt(df: pd.DataFrame) -> pd.DataFrame:
            """数值列 → 可读字符串；保留 label（主列）与 name（行选 key）。
            顺序：可读标签 | 区间 | 收益/风险指标，文件名 name 隐藏但保留用于行选。"""
            df = df.copy()
            for col in ("CAGR", "最大回撤", "波动"):
                df[col] = df[col].apply(
                    lambda v: f"{v * 100:.2f}%" if pd.notna(v) else ""
                )
            for col in ("夏普", "卡玛"):
                df[col] = df[col].apply(lambda v: f"{v:.2f}" if pd.notna(v) else "")
            show = ["label", "run_date", "区间", "CAGR", "夏普", "最大回撤", "波动", "卡玛", "name"]
            return df[[c for c in show if c in df.columns]]

        def _apply_filter(full: pd.DataFrame, keyword: str, strategy: str) -> pd.DataFrame:
            f = full.copy()
            if strategy and strategy != "全部":
                f = f[f["strategy"] == strategy]
            if keyword:
                kw = keyword.lower()
                mask = (
                    f["label"].str.lower().str.contains(kw, na=False)
                    | f["name"].str.lower().str.contains(kw, na=False)
                    | f["区间"].str.lower().str.contains(kw, na=False)
                )
                f = f[mask]
            return f.reset_index(drop=True)

        filtered_df = _apply_filter(full_df, "", "全部")

        # 筛选后的表格状态：供行选中时查 name（行 index ↔ filtered_df 对齐）
        filtered_state = gr.State(filtered_df)

        # 表格：行选择加载详情（业界标准——聚宽/米筐/QuantConnect 均采用表格/卡片选回测）
        table = gr.Dataframe(
            value=_fmt(filtered_df),
            interactive=False,
            max_height=380,
            datatype=["str"] * len(_fmt(filtered_df).columns),
            wrap=True,
            label="点击行查看详情（按策略 / 按搜索词过滤；列按日期倒序）",
        )

        def _on_select(evt: gr.SelectData, fdf: pd.DataFrame) -> tuple:
            """行选中 → 从该行的 name 列取唯一标识加载详情。"""
            if evt.index is None:
                return (gr.update(),) * 4
            idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
            try:
                name = str(fdf["name"].iloc[idx])
            except Exception:
                return "❌ 未找到匹配的回测", None, None, None
            return _load_backtest(name)

        def _on_filter(keyword: str, strategy: str, full: pd.DataFrame):
            """搜索 / 策略筛选 → 返回格式化表 + 新过滤状态。"""
            fdf = _apply_filter(full, keyword, strategy)
            return _fmt(fdf), fdf

        def _on_refresh():
            """刷新列表：重新扫描报告目录，重建筛选项 + 加载最新一个。"""
            new_full = scan_summaries()
            if new_full.empty:
                # 与 outputs[strat_filter, table, df_state, filtered_state,
                # summary_md, equity_plot, dd_plot, trades_table] 一一对应；
                # 空列表时 filtered_state 与 df_state 同源（全量即过滤结果）
                return (
                    gr.update(choices=["全部"], value="全部"),
                    _fmt(new_full), new_full, new_full,
                    "暂无回测结果", None, None, None,
                )
            fdf = _apply_filter(new_full, "", "全部")
            new_choices = ["全部"] + sorted(new_full["strategy"].unique().tolist())
            latest = fdf["name"].iloc[0] if not fdf.empty else None
            md, fe, fd, tr = _load_backtest(latest) if latest else ("暂无", None, None, None)
            return (
                gr.update(choices=new_choices, value="全部"),
                _fmt(fdf), new_full, fdf,
                md, fe, fd, tr,
            )

        summary_md = gr.Markdown(value=init[0])
        equity_plot = gr.Plot(value=init[1])
        dd_plot = gr.Plot(value=init[2])
        trades_table = gr.Dataframe(value=init[3], interactive=False, max_height=420)

        # 行选择 → 加载详情
        table.select(
            _on_select,
            inputs=[filtered_state],
            outputs=[summary_md, equity_plot, dd_plot, trades_table],
        )
        # 搜索 / 策略筛选 → 刷新表格
        search_box.change(
            _on_filter, inputs=[search_box, strat_filter, df_state],
            outputs=[table, filtered_state],
        )
        strat_filter.change(
            _on_filter, inputs=[search_box, strat_filter, df_state],
            outputs=[table, filtered_state],
        )
        # 刷新按钮 → 重建全量 + 筛选 + 详情
        refresh_btn.click(
            _on_refresh,
            outputs=[strat_filter, table, df_state, filtered_state,
                     summary_md, equity_plot, dd_plot, trades_table],
        )

        # ===== 跨页联动：任务完成（回测/扫描/数据刷新）→ 自动重建列表与详情（版本门控） =====
        seen_state = gr.State(data_bus.current())

        def _poll_data_version(seen_val: int):
            changed, cur = data_bus.poll(seen_val)
            if not changed:
                return (*[gr.skip()] * 8, seen_val)
            return (*_on_refresh(), cur)

        gr.Timer(5).tick(
            _poll_data_version,
            inputs=[seen_state],
            outputs=[strat_filter, table, df_state, filtered_state,
                     summary_md, equity_plot, dd_plot, trades_table, seen_state],
        )

        # ---- 参数扫描结果浏览器 ----
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

        # ---- 研究报告浏览器 ----
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

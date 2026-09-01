"""回测中心页面"""
from __future__ import annotations

import queue
import time
from html import escape

import gradio as gr
import pandas as pd
import plotly.graph_objects as go

import data_bus
from api.backtest_api import (
    get_backtest_summary,
    get_benchmark_comparison,
    get_cost_breakdown,
    get_equity_curve,
    get_execution_assumptions,
    get_factor_execution_receipt,
    get_performance_diagnostics,
    get_trades,
    scan_summaries,
)
from api.research_api import list_research_reports, list_sweeps, load_research_report, load_sweep, sweep_headline
from api.strategy_api import (
    STRATEGY_PARAMETER_COLUMNS,
    default_strategy_name,
    encode_strategy_parameter_table,
    get_strategy_defaults,
    strategy_choices,
    strategy_factor_preview,
    strategy_parameter_table,
)
from api.task_api import TASKS, task_queue
from frontend.components.artifacts_panel import render_artifacts_panel, render_wfa_panel
from frontend.theme import metric_card, metric_grid, page_header, section_header

# 策略清单单一数据源：REGISTRY 驱动（与首页/策略监控同源）
try:
    STRATEGY_CHOICES = strategy_choices()
    DEFAULT_STRATEGY = default_strategy_name()
except Exception:
    STRATEGY_CHOICES = [
        "momentum_rotation", "momentum_path", "lowvol_composite", "dual_ma",
        "ml_rank", "lowvol_indz",
    ]
    DEFAULT_STRATEGY = "lowvol_indz"
if DEFAULT_STRATEGY not in STRATEGY_CHOICES:
    DEFAULT_STRATEGY = STRATEGY_CHOICES[0]


def _strategy_defaults(strategy: str) -> dict:
    """该策略当前生效的默认参数（overrides 优先，与 build_strategy 同语义）。"""
    try:
        return get_strategy_defaults(strategy)
    except Exception:
        return {"rebalance_days": 5, "top_k": 10}


def _strategy_parameter_table(strategy: str) -> pd.DataFrame:
    try:
        return strategy_parameter_table(strategy)
    except Exception:
        return pd.DataFrame(columns=STRATEGY_PARAMETER_COLUMNS)


def _fmt_pct(value, digits: int = 2) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%" if pd.notna(value) else "—"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(value, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}" if pd.notna(value) else "—"
    except (TypeError, ValueError):
        return "—"


def _tone(value, *, inverse: bool = False) -> str:
    try:
        positive = float(value) >= 0
    except (TypeError, ValueError):
        return "gray"
    if inverse:
        positive = not positive
    return "green" if positive else "red"


def _cost_html(name: str) -> str:
    """交易成本分解；费用与滑点分开呈现，便于判断换手侵蚀。"""
    c = get_cost_breakdown(name)
    if not c:
        return '<div class="info-card"><span class="muted">暂无交易成本明细。</span></div>'

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

    cards = metric_grid([
        metric_card("总交易成本", f"¥{c['total_cost']:,.0f}", "orange", _fmt_pct(c["cost_pct_init"])),
        metric_card("费用合计", f"¥{c['total_fee']:,.0f}", "gray", "佣金 / 印花税 / 过户费"),
        metric_card("估算滑点", f"¥{c['slip_cost']:,.0f}", "red", f"费用的 {slip_ratio:.1f} 倍"),
        metric_card("年均双边换手", f"{annual_turn:.1f}×", "purple", f"累计 {c['turnover_x']:.1f}×"),
        metric_card("成交笔数", f"{c['n_trades']:,}", "blue", f"买 {c['n_buy']} / 卖 {c['n_sell']}"),
    ])
    tr = float(s.get("total_return") or 0.0)
    note = ""
    if tr != 0.0:
        init_cash = float(s.get("initial_cash") or 1_000_000)
        pnl_amt = abs(tr) * init_cash
        ratio = c["total_cost"] / pnl_amt * 100 if pnl_amt else 0.0
        note = f'<div class="microcopy">成本相当于区间{"亏损" if tr < 0 else "收益"}额的 {ratio:.0f}%</div>'
    return f'<div class="content-card"><div class="info-card-title">交易成本与换手</div>{cards}{note}</div>'


def _execution_html(name: str) -> str:
    a = get_execution_assumptions(name)
    source_note = (
        "本次 run 已固化执行参数"
        if a.get("source") == "run"
        else "旧 run 未固化完整执行参数；以下为当前配置口径，请勿据此反推历史运行"
    )
    items = [
        ("成交时点与价格", a.get("price_model")),
        ("基础滑点", _fmt_pct(a.get("slippage_rate"), 3)),
        ("冲击模型", a.get("impact_model")),
        ("冲击系数", _fmt_num(a.get("impact_coef"), 3)),
        ("佣金", f"{_fmt_pct(a.get('commission_rate'), 3)}，最低 ¥{float(a.get('commission_min') or 0):g}"),
        ("卖出印花税", _fmt_pct(a.get("stamp_tax_rate"), 3)),
        ("涨跌停", a.get("limit_rule")),
        ("停牌", a.get("suspension_rule")),
        ("整手 / 复权", f"{a.get('lot_size', 100)} 股 / {a.get('price_adjust', 'unknown')}"),
    ]
    body = "".join(
        f'<div class="assumption-item"><span>{escape(str(label))}</span>'
        f'<strong>{escape(str(value or "—"))}</strong></div>'
        for label, value in items
    )
    return (
        '<div class="assumption-panel"><div class="info-card-title">执行与数据口径</div>'
        f'<div class="assumption-grid">{body}</div>'
        f'<div class="microcopy">{escape(source_note)}</div></div>'
    )


def _receipt_value(value) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _factor_receipt_html(
    name: str | None = None,
    receipt: dict | None = None,
    *,
    preview: bool = False,
) -> str:
    """展示实际因子公式、权重和运行期降级；预览与结果共用。"""
    receipt = receipt or (get_factor_execution_receipt(name or "") if name else None)
    if not receipt:
        return (
            '<div class="assumption-panel"><div class="info-card-title">因子执行回执</div>'
            '<div class="microcopy">该结果没有可追溯的策略参数，无法确认实际因子。</div></div>'
        )

    source = str(receipt.get("source") or "unknown")
    source_labels = {
        "run": "本次运行已固化",
        "preview": "提交前预览",
        "request": "任务请求参数",
        "artifact_params_inferred": "旧结果：由同次制品参数推断",
        "current_config_fallback": "旧结果：仅按当前配置回退，不代表历史真实口径",
    }
    enabled = receipt.get("enabled_factors") or []
    disabled = receipt.get("disabled_factors") or []
    items = []
    for item in enabled:
        status = str(item.get("status") or "configured")
        badge_class = "status-warning" if status == "degraded" else "status-active"
        badge_label = {
            "active": "已执行",
            "configured": "已配置",
            "degraded": "已降级",
        }.get(status, status)
        value = _receipt_value(item.get("value"))
        items.append(
            '<div class="assumption-item">'
            f'<span>{escape(str(item.get("factor") or item.get("key") or "—"))}</span>'
            f'<strong>{escape(value)} '
            f'<span class="status-indicator {badge_class}">{escape(badge_label)}</span></strong>'
            f'<small>{escape(str(item.get("detail") or ""))}</small></div>'
        )
    controls = receipt.get("controls") or {}
    controls_text = " · ".join(
        f"{key}={_receipt_value(value)}" for key, value in controls.items()
    ) or "—"
    disabled_text = "、".join(str(item.get("factor") or item.get("key")) for item in disabled)
    warnings = receipt.get("warnings") or []
    warning_html = "".join(
        f'<div class="risk-banner risk-high"><span>{escape(str(warning))}</span></div>'
        for warning in warnings
    )
    strategy_kind = "横截面因子策略" if receipt.get("is_factor_strategy") else "规则/技术信号策略"
    title = "提交前因子预览" if preview else "因子执行回执"
    return (
        f'<div class="assumption-panel"><div class="info-card-title">{title}</div>'
        f'<div><strong>{escape(strategy_kind)}</strong></div>'
        f'<div class="microcopy">公式：{escape(str(receipt.get("formula") or "—"))}</div>'
        f'<div class="assumption-grid">{"".join(items)}</div>'
        f'<div class="microcopy">组合与过滤：{escape(controls_text)}</div>'
        f'<div class="microcopy">关闭的可选因子（{len(disabled)}）：'
        f'{escape(disabled_text or "无")}</div>{warning_html}'
        f'<div class="microcopy">来源：{escape(source_labels.get(source, source))}</div></div>'
    )


def _factor_preview_html(strategy: str, table=None) -> str:
    try:
        return _factor_receipt_html(
            receipt=strategy_factor_preview(strategy, table), preview=True,
        )
    except Exception as exc:
        return (
            '<div class="risk-banner risk-high"><b>高级参数无效</b>'
            f'<span>{escape(str(exc))}</span></div>'
        )


def _summary_html(name: str, summary: dict, diagnostics: dict | None, n_trades: int) -> str:
    diagnostics = diagnostics or {}
    primary = metric_grid([
        metric_card("累计收益", _fmt_pct(summary.get("total_return")), _tone(summary.get("total_return"))),
        metric_card("年化收益", _fmt_pct(summary.get("cagr")), _tone(summary.get("cagr"))),
        metric_card("基准年化", _fmt_pct(summary.get("bench_cagr")), "gray"),
        metric_card("超额年化", _fmt_pct(summary.get("bench_excess_cagr")), _tone(summary.get("bench_excess_cagr"))),
        metric_card("夏普", _fmt_num(summary.get("sharpe")), "purple"),
        metric_card("最大回撤", _fmt_pct(summary.get("max_drawdown")), "red"),
        metric_card("卡玛", _fmt_num(summary.get("calmar")), "teal"),
        metric_card("成交笔数", f"{n_trades:,}", "blue"),
    ])
    secondary = metric_grid([
        metric_card("Sortino", _fmt_num(diagnostics.get("sortino")), "purple"),
        metric_card("信息比率", _fmt_num(diagnostics.get("information_ratio")), "teal"),
        metric_card("跟踪误差", _fmt_pct(diagnostics.get("tracking_error")), "gray"),
        metric_card("下行波动", _fmt_pct(diagnostics.get("downside_vol")), "orange"),
        metric_card("持仓日胜率", _fmt_pct(summary.get("invested_win_rate", summary.get("daily_win_rate"))), "blue"),
        metric_card("单日最大亏损", _fmt_pct(diagnostics.get("worst_day")), "red"),
        metric_card("95% CVaR", _fmt_pct(diagnostics.get("cvar_95")), "red"),
        metric_card("最长回撤", f"{diagnostics.get('max_drawdown_duration', '—')} 日", "orange"),
    ])
    recovery = diagnostics.get("drawdown_recovery_date") or "尚未恢复"
    meta = (
        '<div class="content-card"><div class="info-card-title">所选回测</div>'
        f'<div><strong>{escape(name)}</strong></div>'
        f'<div class="microcopy">区间 {escape(str(summary.get("start") or "—"))} → '
        f'{escape(str(summary.get("end") or "—"))} · 最大回撤谷底 '
        f'{escape(str(diagnostics.get("drawdown_trough_date") or summary.get("mdd_trough") or "—"))} · '
        f'恢复日 {escape(str(recovery))}</div></div>'
    )
    return (
        meta
        + '<div class="info-card-title">核心绩效</div>' + primary
        + '<div class="info-card-title">相对与下行风险</div>' + secondary
        + _factor_receipt_html(name)
        + _cost_html(name)
        + _execution_html(name)
    )


def _load_backtest(name: str):
    """加载指定回测的摘要/图表/交易"""
    if not name:
        return "未选择回测", None, None, None, None

    # 摘要（旧格式 summary 可能缺键，统一 .get 防整页 KeyError 崩溃）
    s = get_backtest_summary(name) or {}
    comparison = get_benchmark_comparison(name)
    fig_eq, fig_dd, fig_excess = None, None, None
    if comparison is not None and not comparison.empty:
        dates = comparison["date"]
        strategy_nav = comparison["strategy_nav"]
        benchmark_nav = comparison["benchmark_nav"]
        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scatter(
            x=dates, y=strategy_nav, mode="lines", name="策略", line=dict(color="#2563EB", width=2.4),
        ))
        fig_eq.add_trace(go.Scatter(
            x=dates, y=benchmark_nav, mode="lines", name="沪深300", line=dict(color="#64748B", width=1.8),
        ))
        fig_dd = go.Figure()
        for values, label, color, fill in (
            (strategy_nav, "策略回撤", "#DC2626", "tozeroy"),
            (benchmark_nav, "基准回撤", "#64748B", None),
        ):
            dd = (values / values.cummax() - 1.0) * 100
            fig_dd.add_trace(go.Scatter(
                x=dates, y=dd, mode="lines", name=label, fill=fill,
                line=dict(color=color, width=2 if label == "策略回撤" else 1.5),
            ))
        fig_excess = go.Figure(go.Scatter(
            x=dates, y=comparison["excess_nav"], mode="lines", name="超额净值",
            line=dict(color="#0891B2", width=2.2), fill="tozeroy",
            fillcolor="rgba(8,145,178,0.10)",
        ))
        fig_excess.add_hline(y=1.0, line_dash="dash", line_color="#94A3B8")
        fig_eq.update_layout(title="策略与基准净值（起点 = 1）", yaxis_title="净值")
        fig_dd.update_layout(title="策略与基准回撤", yaxis_title="回撤 (%)")
        fig_excess.update_layout(title="相对基准超额净值", yaxis_title="超额净值")
        for fig, height in ((fig_eq, 390), (fig_dd, 300), (fig_excess, 390)):
            fig.update_layout(
                height=height, margin=dict(l=12, r=12, t=48, b=12),
                template="plotly_white", hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
    else:
        equity_df = get_equity_curve(name)
        if equity_df is not None and not equity_df.empty and "date" in equity_df:
            value_columns = [c for c in equity_df.columns if c != "date"]
            fig_eq, fig_dd = go.Figure(), go.Figure()
            for col in value_columns:
                values = pd.to_numeric(equity_df[col], errors="coerce")
                valid = values.dropna()
                if valid.empty or float(valid.iloc[0]) <= 0:
                    continue
                nav = values / float(valid.iloc[0])
                label = str(col).replace("min_avg_amount=", "日均额≥")
                fig_eq.add_trace(go.Scatter(x=equity_df["date"], y=nav, mode="lines", name=label))
                fig_dd.add_trace(go.Scatter(
                    x=equity_df["date"], y=(nav / nav.cummax() - 1.0) * 100,
                    mode="lines", name=label,
                ))
            for fig, title, height in (
                (fig_eq, "策略净值（缺少同期基准数据）", 390),
                (fig_dd, "策略回撤（缺少同期基准数据）", 300),
            ):
                fig.update_layout(
                    title=title, height=height, margin=dict(l=12, r=12, t=48, b=12),
                    template="plotly_white", hovermode="x unified",
                )

    trades_df = get_trades(name)
    n_trades = len(trades_df) if trades_df is not None else 0
    summary_html = _summary_html(name, s, get_performance_diagnostics(name), n_trades)
    return summary_html, fig_eq, fig_excess, fig_dd, trades_df


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


def _run_backtest(
    strategy: str,
    rebalance_days: float,
    top_k: float,
    strategy_params,
    start: str,
    end: str,
    research_mode: str,
    cost_multiplier: float,
):
    """提交回测任务（换手频率/持仓数等参数由前端传入，覆盖 config）。"""
    try:
        rb = int(rebalance_days) if rebalance_days else None
        tk = int(top_k) if top_k else None
    except Exception:
        yield "❌ 参数必须为整数"
        return

    try:
        cost = float(cost_multiplier)
    except (TypeError, ValueError):
        yield "❌ 成本压力倍数必须为数字"
        return
    if not 0 <= cost <= 10:
        yield "❌ 成本压力倍数必须在 0 到 10 之间"
        return

    try:
        assignments = encode_strategy_parameter_table(strategy, strategy_params)
        receipt = strategy_factor_preview(strategy, strategy_params)
    except (KeyError, TypeError, ValueError) as exc:
        yield f"❌ 高级策略参数无效：{exc}"
        return

    extra = [
        "--strategy", strategy,
        "--start", (start or "2020-01-01"),
        "--research-mode", research_mode or "exploratory",
        "--cost-multiplier", f"{cost:g}",
    ]
    if end:
        extra += ["--end", end]
    # 显式参数优先于 config.strategy.overrides（build_strategy 内部合并）
    if rb:
        extra += ["--rebalance-days", str(rb)]
    if tk:
        extra += ["--top-k", str(tk)]
    for assignment in assignments:
        extra += ["--param", assignment]

    header = (
        f"🚀 已提交回测：**{strategy}** | 换手 {rb or '默认'} 日 | "
        f"持仓 {tk or '默认'} | {start or '2020-01-01'} ~ {end or '最新'} | "
        f"{research_mode or 'exploratory'} | {cost:g}x 成本 | "
        f"有效因子/信号 {receipt.get('enabled_count', 0)} 个"
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
        gr.HTML(page_header(
            "📈 回测中心",
            "统一查看绝对收益、基准超额、下行风险、执行口径与交易成本；研究结果默认不等于实盘准入。",
            "RESEARCH & VALIDATION",
        ))

        # ---- 参数面板：策略与关键参数前端可调 ----
        with gr.Accordion("运行新回测 · 参数覆盖与成本压力", open=False):
            gr.Markdown(
                "正式研究请选择 `formal`；若 PIT 股票池不完整，任务会直接失败。"
                "成本倍数会同时缩放佣金、印花税、过户费、基础滑点与冲击成本。"
            )
            with gr.Row():
                strategy_in = gr.Dropdown(
                    label="策略", choices=STRATEGY_CHOICES, value=DEFAULT_STRATEGY,
                )
                rebal_in = gr.Number(
                    label="换手频率（交易日）",
                    value=_strategy_defaults(DEFAULT_STRATEGY)["rebalance_days"], precision=0,
                )
                topk_in = gr.Number(
                    label="持仓数 top_k",
                    value=_strategy_defaults(DEFAULT_STRATEGY)["top_k"], precision=0,
                )
            with gr.Row():
                start_in = gr.Textbox(label="起始日期", value="2020-01-01")
                end_in = gr.Textbox(label="结束日期", placeholder="留空 = 最新数据")
                research_mode_in = gr.Dropdown(
                    label="研究口径", choices=["exploratory", "formal"], value="exploratory",
                    info="formal 强制使用逐日 PIT 股票池",
                )
                cost_multiplier_in = gr.Number(
                    label="成本压力倍数", value=1.0, minimum=0.0, maximum=10.0,
                    info="0 = 零成本；1 = 默认；2 = 双倍压力",
                )
            with gr.Accordion("因子与策略高级参数 · schema 动态生成", open=True):
                gr.Markdown(
                    "仅修改“值”列。切换策略时参数表会按该策略的 `PARAMS_SCHEMA` 自动重建；"
                    "核心的调仓周期与持仓数仍使用上方独立控件。"
                )
                strategy_params_in = gr.Dataframe(
                    value=_strategy_parameter_table(DEFAULT_STRATEGY),
                    headers=STRATEGY_PARAMETER_COLUMNS,
                    datatype=["str"] * len(STRATEGY_PARAMETER_COLUMNS),
                    interactive=True,
                    wrap=True,
                    show_search="filter",
                    buttons=["fullscreen", "copy"],
                    static_columns=[0, 2, 3, 4],
                    max_height=430,
                    label="因子、择时、组合构造与交易过滤参数",
                )
                factor_preview = gr.HTML(
                    value=_factor_preview_html(
                        DEFAULT_STRATEGY,
                        _strategy_parameter_table(DEFAULT_STRATEGY),
                    )
                )
            run_btn = gr.Button("运行回测", variant="primary", size="lg")
            run_out = gr.Markdown()

            def _on_strategy_change(name: str):
                """同步核心参数、高级参数和因子公式预览。"""
                d = _strategy_defaults(name)
                table = _strategy_parameter_table(name)
                return (
                    gr.update(value=d["rebalance_days"]),
                    gr.update(value=d["top_k"]),
                    gr.update(value=table),
                    _factor_preview_html(name, table),
                )

            strategy_in.change(
                _on_strategy_change,
                inputs=[strategy_in],
                outputs=[rebal_in, topk_in, strategy_params_in, factor_preview],
            )
            strategy_params_in.change(
                _factor_preview_html,
                inputs=[strategy_in, strategy_params_in],
                outputs=[factor_preview],
            )
            run_btn.click(
                _run_backtest,
                inputs=[
                    strategy_in, rebal_in, topk_in, strategy_params_in, start_in, end_in,
                    research_mode_in, cost_multiplier_in,
                ],
                outputs=[run_out],
            )

        # ---- 运行 Walk-Forward（样本外验证）----
        with gr.Accordion("运行 Walk-Forward · 样本外与过拟合诊断", open=False):
            gr.Markdown(
                "*每折只在 train 段选参数，再在紧接的 test 段验证；"
                "最后把 test 段拼接成完整的样本外曲线。*"
            )
            with gr.Row():
                wfa_strategy = gr.Dropdown(
                    label="策略", choices=STRATEGY_CHOICES, value=DEFAULT_STRATEGY,
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
            wfa_run_btn = gr.Button("运行 Walk-Forward", variant="primary", size="lg")
            wfa_run_out = gr.Markdown()
            wfa_run_btn.click(
                _run_wfa,
                inputs=[wfa_strategy, wfa_train, wfa_test, wfa_embargo,
                        wfa_metric, wfa_grid, wfa_anchored],
                outputs=[wfa_run_out],
            )

        # ---- 回测索引：搜索 + 策略筛选 + 表格选择（替代长下拉）----
        gr.HTML(section_header(
            "历史回测与结果诊断",
            "选择一条运行后，页面会同步更新策略/基准净值、超额、回撤、成本和成交明细。",
            "RESULT EXPLORER",
        ))
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
            strategy_choices = ["全部", *sorted(full_df["strategy"].unique().tolist())]
            strat_filter = gr.Dropdown(
                label="策略筛选", choices=strategy_choices, value="全部",
            )
            refresh_btn = gr.Button("🔄 刷新列表", size="sm")

        def _fmt(df: pd.DataFrame) -> pd.DataFrame:
            """数值列转为可读字符串；内部 name 仅保留在 filtered_state。"""
            visible = ["运行", "日期", "区间", "CAGR", "夏普", "最大回撤", "波动", "卡玛"]
            if df is None or df.empty:
                return pd.DataFrame(columns=visible)
            df = df.copy()
            for col in ("CAGR", "最大回撤", "波动"):
                if col in df:
                    df[col] = df[col].apply(
                        lambda v: f"{v * 100:.2f}%" if pd.notna(v) else ""
                    )
            for col in ("夏普", "卡玛"):
                if col in df:
                    df[col] = df[col].apply(lambda v: f"{v:.2f}" if pd.notna(v) else "")
            show = ["label", "run_date", "区间", "CAGR", "夏普", "最大回撤", "波动", "卡玛"]
            return df[[c for c in show if c in df.columns]].rename(
                columns={"label": "运行", "run_date": "日期"}
            )

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
            label="运行列表 · 点击行查看详情",
            show_search="filter",
            pinned_columns=1,
            buttons=["fullscreen", "copy"],
        )

        def _on_select(evt: gr.SelectData, fdf: pd.DataFrame) -> tuple:
            """行选中 → 从该行的 name 列取唯一标识加载详情。"""
            if evt.index is None:
                return (gr.update(),) * 5
            idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
            try:
                name = str(fdf["name"].iloc[idx])
            except Exception:
                return "❌ 未找到匹配的回测", None, None, None, None
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
                # summary_html, equity_plot, excess_plot, dd_plot, trades_table] 一一对应；
                # 空列表时 filtered_state 与 df_state 同源（全量即过滤结果）
                return (
                    gr.update(choices=["全部"], value="全部"),
                    _fmt(new_full), new_full, new_full,
                    "暂无回测结果", None, None, None, None,
                )
            fdf = _apply_filter(new_full, "", "全部")
            new_choices = ["全部", *sorted(new_full["strategy"].unique().tolist())]
            latest = fdf["name"].iloc[0] if not fdf.empty else None
            detail = _load_backtest(latest) if latest else ("暂无", None, None, None, None)
            return (
                gr.update(choices=new_choices, value="全部"),
                _fmt(fdf), new_full, fdf,
                *detail,
            )

        summary_html = gr.HTML(value=init[0])
        with gr.Row():
            equity_plot = gr.Plot(value=init[1], scale=2)
            excess_plot = gr.Plot(value=init[2], scale=1)
        dd_plot = gr.Plot(value=init[3])
        gr.HTML(section_header(
            "成交明细",
            "完整成交记录，代码统一保留 6 位；费用为佣金、印花税与过户费合计。",
            "TRADES",
        ))
        trades_table = gr.Dataframe(
            value=init[4], interactive=False, max_height=460,
            show_search="filter", pinned_columns=2, buttons=["fullscreen", "copy"],
        )

        # 行选择 → 加载详情
        table.select(
            _on_select,
            inputs=[filtered_state],
            outputs=[summary_html, equity_plot, excess_plot, dd_plot, trades_table],
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
                     summary_html, equity_plot, excess_plot, dd_plot, trades_table],
        )

        # ===== 跨页联动：任务完成（回测/扫描/数据刷新）→ 自动重建列表与详情（版本门控） =====
        seen_state = gr.State(data_bus.current())

        def _poll_data_version(seen_val: int):
            changed, cur = data_bus.poll(seen_val)
            if not changed:
                return (*[gr.skip()] * 9, seen_val)
            return (*_on_refresh(), cur)

        gr.Timer(5).tick(
            _poll_data_version,
            inputs=[seen_state],
            outputs=[strat_filter, table, df_state, filtered_state,
                     summary_html, equity_plot, excess_plot, dd_plot, trades_table, seen_state],
        )

        # ---- Walk-Forward 过拟合诊断 + 制品追溯 ----
        gr.HTML(section_header(
            "研究审计与可复现性",
            "查看样本外衰减、参数稳定性、代码版本、数据版本和运行制品。",
            "AUDIT TRAIL",
        ))
        render_wfa_panel()
        render_artifacts_panel()

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

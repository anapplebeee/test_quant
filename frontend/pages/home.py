"""首页页面：可选择查看的回测结果 + 区间窗口指标 + 策略库/最新验证结果。"""
from __future__ import annotations

import gradio as gr
import pandas as pd

from api.backtest_api import get_backtest_list, get_backtest_summary, get_window_stats
from api.research_api import latest_sweep_headlines
from api.strategy_api import strategy_catalog
from frontend.theme import metric_card


def _fmt_pct(v, digits: int = 1) -> str:
    """统一百分比格式：+12.3% / -4.5%；None/NaN → '-'"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    return f"{v * 100:+.{digits}f}%"


def _fmt_num(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    return f"{v:.{digits}f}"


def _color_by_sign(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "gray"
    return "green" if v > 0 else ("red" if v < 0 else "gray")


def _summary_html(name: str | None) -> str:
    """所选回测结果的完整摘要 HTML（统一卡片口径，供下拉切换刷新）。"""
    if not name:
        return "*暂无回测结果，请先在回测中心运行回测。*"
    s = get_backtest_summary(name) or {}
    ws = get_window_stats(name) or {}
    strategy = name.rsplit("_", 1)[0] if "_" in name else name
    cat = {r["name"]: r for r in strategy_catalog()}
    meta = cat.get(strategy)
    desc = f"{meta['label']}：{meta['desc']}" if meta else f"未知策略 `{strategy}`（未在 REGISTRY 注册）"

    total = s.get("total_return")
    bench_total = s.get("bench_total_return")
    excess = s.get("bench_excess_cagr")
    w1y = ws.get("last_1y") or {}
    w6m = ws.get("last_6m") or {}

    rows = [
        (
            "📌 全周期（区间口径）",
            [
                metric_card("累计收益", _fmt_pct(total), _color_by_sign(total)),
                metric_card("基准同期", _fmt_pct(bench_total), _color_by_sign(bench_total)),
                metric_card("超额年化", _fmt_pct(excess), _color_by_sign(excess)),
                metric_card("最大回撤", _fmt_pct(s.get("max_drawdown")), "red"),
            ],
        ),
        (
            "📏 近 1 年（252 交易日，与沪深300 同期可比）",
            [
                metric_card("近1年收益", _fmt_pct(w1y.get("return")), _color_by_sign(w1y.get("return"))),
                metric_card("近1年回撤", _fmt_pct(w1y.get("mdd")), "red"),
                metric_card("近1年基准收益", _fmt_pct(w1y.get("bench_return")), _color_by_sign(w1y.get("bench_return"))),
                metric_card("近1年基准回撤", _fmt_pct(w1y.get("bench_mdd")), "red"),
            ],
        ),
        (
            "📏 近半年（126 交易日）",
            [
                metric_card("近半年收益", _fmt_pct(w6m.get("return")), _color_by_sign(w6m.get("return"))),
                metric_card("近半年回撤", _fmt_pct(w6m.get("mdd")), "red"),
                metric_card("近半年基准收益", _fmt_pct(w6m.get("bench_return")), _color_by_sign(w6m.get("bench_return"))),
                metric_card("近半年基准回撤", _fmt_pct(w6m.get("bench_mdd")), "red"),
            ],
        ),
        (
            "📌 风险指标（全周期）",
            [
                metric_card("年化收益", _fmt_pct(s.get("cagr")), _color_by_sign(s.get("cagr"))),
                metric_card("夏普比率", _fmt_num(s.get("sharpe")), "purple"),
                metric_card("年化波动", _fmt_pct(s.get("annual_vol")), "gray"),
                # 持仓日胜率：剔除空仓/零收益日，避免对含择时策略的系统性低估
                metric_card("日胜率(持仓)", _fmt_pct(s.get("invested_win_rate", s.get("daily_win_rate"))), "teal"),
            ],
        ),
    ]

    parts = [
        f"**所选结果**: `{name}`",
        f"**策略**: {desc}",
        f"**回测区间**: {s.get('start', '-')} ~ {s.get('end', '-')}　**基准**: 沪深300",
        "",
    ]
    for title, cards in rows:
        parts.append(f"**{title}**")
        parts.append("<div style='display:flex; gap:8px; flex-wrap:wrap'>" + "".join(cards) + "</div>")
        parts.append("")
    return "\n".join(parts)


def render():
    """渲染首页 Tab"""
    with gr.Tab("🏠 首页"):
        gr.Markdown("# 📊 Quart 量化研究平台\n> A-share 量化策略研究 · 因子挖掘 · 回测分析 · 风险管理")

        names = get_backtest_list()
        default = names[-1] if names else None
        gr.Markdown("### 📌 回测结果查看（可选择任意一次回测，默认最新）")
        result_dd = gr.Dropdown(
            label="选择要查看的回测结果",
            choices=names,
            value=default,
            filterable=True,
        )
        summary_html = gr.HTML(value=_summary_html(default))
        result_dd.change(_summary_html, inputs=[result_dd], outputs=[summary_html])

        gr.Markdown("---")

        # 最新验证结果：每个策略最新一次参数扫描的最优行（数据关联 reports/sweep_*.csv）
        heads = latest_sweep_headlines()
        if heads is not None and not heads.empty:
            gr.Markdown("### 🔬 最新验证结果（来自各策略最新参数扫描，按 CAGR 排序）")
            show = heads.copy()
            for c, fmt in (("CAGR", _fmt_pct), ("最大回撤", _fmt_pct)):
                if c in show.columns:
                    show[c] = show[c].map(_fmt_pct)
            if "夏普" in show.columns:
                show["夏普"] = show["夏普"].map(lambda v: _fmt_num(v))
            if "换手x" in show.columns:
                show["换手x"] = show["换手x"].map(lambda v: "-" if pd.isna(v) else f"{v:.1f}x")
            gr.Dataframe(value=show, interactive=False)

        gr.Markdown("### 📚 策略库（由后端 REGISTRY 驱动，与回测中心/策略监控同源）")
        md_rows = ["| 策略 | 名称 | 状态 | 默认换手/持仓 | 说明 |", "|------|------|------|------|------|"]
        for r in strategy_catalog():
            md_rows.append(
                f"| `{r['name']}` | {r['label']} | {r['status']} | "
                f"{r['default_rebalance']}日 / Top{r['default_top_k']} | {r['desc']} |"
            )
        gr.Markdown("\n".join(md_rows))

        gr.Markdown("### 功能模块")
        gr.Markdown("""
        | 模块 | 说明 |
        |------|------|
        | 🗃️ 数据总览 | 股票池 / 数据覆盖 / 市场概览 |
        | 🔬 因子研究 | IC/ICIR / 因子表现 / 选股能力 |
        | 📈 回测中心 | 净值曲线 / 完整交易记录 / 成本分解 / 参数扫描 / 研究报告 |
        | 📋 每日信号 | 持仓建议 / 调仓信号 / ML预测 |
        | 📡 策略监控 | 任务执行 / 运行状态 / 持仓分析 |
        | 🧩 归因分析 | Brinson归因 / 行业分布 / 收益分解 |
        | 🛡️ 风险管理 | VaR/CVaR / 集中度 / 流动性 |
        | 🌿 因子生态 | IC衰减 / IC时序 / 拥挤度 / 预警 |
        | 🔍 回测诊断 | Walk-Forward / 过拟合检验 |
        | 📖 参数词典 | 量化参数含义 / 计算方法 |
        """)

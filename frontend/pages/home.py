"""首页页面"""
from __future__ import annotations

import glob
import json

import gradio as gr
import pandas as pd

from api.research_api import latest_sweep_headlines
from api.strategy_api import strategy_catalog
from frontend.theme import metric_card


def _latest_summary() -> tuple[dict | None, str]:
    """获取最新回测摘要及策略名"""
    files = sorted(glob.glob("reports/summary_*.json"))
    if not files:
        return None, ""
    latest = files[-1]
    # 文件名: summary_{strategy}_{timestamp}.json
    parts = latest.replace("summary_", "").replace(".json", "").rsplit("_", 1)
    strategy = parts[0] if parts else "unknown"
    with open(latest) as f:
        return json.load(f), strategy


def _strategy_desc(name: str) -> str:
    for row in strategy_catalog():
        if row["name"] == name:
            return f"{row['label']}：{row['desc']}" if row["desc"] else name
    return "未知策略（未在 REGISTRY/META 注册）"


def render():
    """渲染首页 Tab"""
    with gr.Tab("🏠 首页"):
        gr.Markdown("# 📊 Quart 量化研究平台\n> A-share 量化策略研究 · 因子挖掘 · 回测分析 · 风险管理")

        summary, strategy = _latest_summary()
        if summary:
            desc = _strategy_desc(strategy)
            gr.Info(f"当前展示策略: {strategy} — {desc}")

            with gr.Accordion(f"📌 当前展示: {strategy}（点击查看策略说明）", open=True):
                gr.Markdown(f"**策略**: `{strategy}` — {desc}")
                gr.Markdown(f"**回测区间**: {summary.get('start','')} ~ {summary.get('end','')}")
                gr.Markdown(f"**基准**: 沪深300 (IDX000300)")
                bench_color = "green"
                # 旧格式 summary 可能缺键，统一 .get 防整页 KeyError 崩溃
                total_return = summary.get("total_return", 0)
                if total_return < 0:
                    bench_color = "red"
                with gr.Row():
                    gr.HTML(metric_card("策略累计收益", f"{total_return*100:.1f}%", bench_color))
                    gr.HTML(metric_card("基准累计收益", f"{summary.get('bench_total_return',0)*100:.1f}%", "gray"))
                    gr.HTML(metric_card("超额年化", f"{summary.get('excess_cagr',0)*100:.1f}%",
                                        "green" if summary.get('excess_cagr',0) > 0 else "red"))
                with gr.Row():
                    gr.HTML(metric_card("年化收益", f"{summary.get('cagr',0)*100:.1f}%", "blue"))
                    gr.HTML(metric_card("夏普比率", f"{summary.get('sharpe',0):.2f}", "purple"))
                    gr.HTML(metric_card("最大回撤", f"{summary.get('max_drawdown',0)*100:.1f}%", "red"))
                    gr.HTML(metric_card("日胜率", f"{summary.get('daily_win_rate',0)*100:.1f}%", "teal"))
        else:
            gr.Info("暂无回测摘要，请先运行回测")

        gr.Markdown("---")

        # 最新验证结果：每个策略最新一次参数扫描的最优行（数据关联 reports/sweep_*.csv）
        heads = latest_sweep_headlines()
        if heads is not None and not heads.empty:
            gr.Markdown("### 🔬 最新验证结果（来自各策略最新参数扫描，按 CAGR 排序）")
            show = heads.copy()
            for c, fmt in (("CAGR", "{:+.1%}"), ("最大回撤", "{:+.1%}")):
                if c in show.columns:
                    show[c] = show[c].map(lambda v: fmt.format(v) if pd.notna(v) else "-")
            if "夏普" in show.columns:
                show["夏普"] = show["夏普"].map(lambda v: f"{v:.2f}" if pd.notna(v) else "-")
            if "换手x" in show.columns:
                show["换手x"] = show["换手x"].map(lambda v: f"{v:.1f}x" if pd.notna(v) else "-")
            gr.Dataframe(value=show, interactive=False)

        gr.Markdown("### 📚 策略库（由后端 REGISTRY 驱动，与回测中心/策略监控同源）")
        rows = strategy_catalog()
        md_rows = ["| 策略 | 名称 | 默认换手/持仓 | 说明 |", "|------|------|------|------|"]
        for r in rows:
            md_rows.append(
                f"| `{r['name']}` | {r['label']} | {r['default_rebalance']}日 / Top{r['default_top_k']} | {r['desc']} |"
            )
        gr.Markdown("\n".join(md_rows))

        gr.Markdown("### 功能模块")
        gr.Markdown("""
        | 模块 | 说明 |
        |------|------|
        | 🗃️ 数据总览 | 股票池 / 数据覆盖 / 市场概览 |
        | 🔬 因子研究 | IC/ICIR / 因子表现 / 选股能力 |
        | 📈 回测中心 | 净值曲线 / 交易记录 / 成本分解 / 参数扫描 / 研究报告 |
        | 📋 每日信号 | 持仓建议 / 调仓信号 / ML预测 |
        | 📡 策略监控 | 任务执行 / 运行状态 / 持仓分析 |
        | 🧩 归因分析 | Brinson归因 / 行业分布 / 收益分解 |
        | 🛡️ 风险管理 | VaR/CVaR / 集中度 / 流动性 |
        | 🌿 因子生态 | IC衰减 / IC时序 / 拥挤度 / 预警 |
        | 🔍 回测诊断 | Walk-Forward / 过拟合检验 |
        | 📖 参数词典 | 量化参数含义 / 计算方法 |
        """)

"""首页页面"""
from __future__ import annotations

import glob
import json

import gradio as gr

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


STRATEGY_DESC = {
    "momentum_rotation": "动量轮动：60日动量排名，持Top10等权，5日调仓，熊市空仓",
    "lowvol_composite": "低波复合：波动率+振幅+下行波动复合排序，低风险选股",
    "ml_rank": "ML排序：Alpha158因子 + LightGBM打分，按预测分数选股",
}


def render():
    """渲染首页 Tab"""
    with gr.Tab("🏠 首页"):
        gr.Markdown("# 📊 Quart 量化研究平台\n> A-share 量化策略研究 · 因子挖掘 · 回测分析 · 风险管理")

        summary, strategy = _latest_summary()
        if summary:
            desc = STRATEGY_DESC.get(strategy, "未知策略")
            gr.Info(f"当前展示策略: {strategy} — {desc}")

            with gr.Accordion(f"📌 当前展示: {strategy}（点击查看策略说明）", open=True):
                gr.Markdown(f"**策略**: `{strategy}` — {desc}")
                gr.Markdown(f"**回测区间**: {summary.get('start','')} ~ {summary.get('end','')}")
                gr.Markdown(f"**基准**: 沪深300 (IDX000300)")
                bench_color = "green"
                if summary["total_return"] < 0:
                    bench_color = "red"
                with gr.Row():
                    gr.HTML(metric_card("策略累计收益", f"{summary['total_return']*100:.1f}%", bench_color))
                    gr.HTML(metric_card("基准累计收益", f"{summary['bench_total_return']*100:.1f}%", "gray"))
                    gr.HTML(metric_card("超额年化", f"{summary.get('excess_cagr',0)*100:.1f}%",
                                        "green" if summary.get('excess_cagr',0) > 0 else "red"))
                with gr.Row():
                    gr.HTML(metric_card("年化收益", f"{summary['cagr']*100:.1f}%", "blue"))
                    gr.HTML(metric_card("夏普比率", f"{summary['sharpe']:.2f}", "purple"))
                    gr.HTML(metric_card("最大回撤", f"{summary['max_drawdown']*100:.1f}%", "red"))
                    gr.HTML(metric_card("日胜率", f"{summary.get('daily_win_rate',0)*100:.1f}%", "teal"))
        else:
            gr.Info("暂无回测摘要，请先运行回测")

        gr.Markdown("---")
        gr.Markdown("### 📚 策略库")
        gr.Markdown(f"""
        | 策略 | 名称 | 说明 |
        |------|------|------|
        | `momentum_rotation` | 动量轮动 | {STRATEGY_DESC['momentum_rotation']} |
        | `lowvol_composite` | 低波复合 | {STRATEGY_DESC['lowvol_composite']} |
        | `ml_rank` | ML排序 | {STRATEGY_DESC['ml_rank']} |
        """)

        gr.Markdown("### 功能模块")
        gr.Markdown("""
        | 模块 | 说明 |
        |------|------|
        | 🗃️ 数据总览 | 股票池 / 数据覆盖 / 市场概览 |
        | 🔬 因子研究 | IC/ICIR / 因子表现 / 选股能力 |
        | 📈 回测中心 | 净值曲线 / 交易记录 / 回撤分析 |
        | 📋 每日信号 | 持仓建议 / 调仓信号 / ML预测 |
        | 📡 策略监控 | 任务执行 / 运行状态 / 持仓分析 |
        | 🧩 归因分析 | Brinson归因 / 行业分布 / 收益分解 |
        | 🛡️ 风险管理 | VaR/CVaR / 集中度 / 流动性 |
        | 🌿 因子生态 | IC衰减 / IC时序 / 拥挤度 / 预警 |
        | 🔍 回测诊断 | Walk-Forward / 过拟合检验 |
        | 📖 参数词典 | 量化参数含义 / 计算方法 |
        """)

"""首页页面"""
from __future__ import annotations

import json
import glob
import os

import gradio as gr

from frontend.theme import metric_card


def render():
    """渲染首页 Tab"""
    with gr.Tab("🏠 首页"):
        gr.Markdown("# 📊 Quart 量化研究平台\n> A-share 量化策略研究 · 因子挖掘 · 回测分析 · 风险管理")

        # 最新回测摘要
        summary_files = sorted(glob.glob("reports/summary_*.json"))
        if summary_files:
            latest = summary_files[-1]
            try:
                with open(latest) as f:
                    s = json.load(f)
                with gr.Row():
                    gr.HTML(metric_card("累计收益", f"{s['total_return']*100:.1f}%",
                                        "green" if s['total_return'] > 0 else "red"))
                    gr.HTML(metric_card("年化收益", f"{s['cagr']*100:.1f}%", "blue"))
                    gr.HTML(metric_card("夏普比率", f"{s['sharpe']:.2f}", "purple"))
                    gr.HTML(metric_card("最大回撤", f"{s['max_drawdown']*100:.1f}%", "red"))
                    gr.HTML(metric_card("超额年化", f"{s.get('excess_cagr', 0)*100:.1f}%", "teal"))
            except Exception:
                pass

        gr.Markdown("---")
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

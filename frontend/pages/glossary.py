"""参数词典页面"""
from __future__ import annotations

import gradio as gr

from frontend.theme import page_header


def render():
    """渲染参数词典 Tab"""
    with gr.Tab("📖 参数词典"):
        gr.HTML(page_header("📖 量化参数词典", "所有关键参数的含义、计算方法和经验取值"))

        with gr.Accordion("⚙️ 策略参数", open=True):
            gr.Markdown("""
            | 参数名 | 含义 | 常用范围 | 当前值 |
            |--------|------|----------|--------|
            | `lookback_days` | 动量回看天数 | 20-252天 | 60 |
            | `top_k` | 持仓股票数 | 5-50只 | 10 |
            | `rebalance_days` | 调仓周期 | 1-20天 | 5 |
            | `max_weight_pct` | 单股最大权重 | 5%-20% | 15% |
            | `min_avg_amount` | 流动性门槛 | 1000万-1亿 | 5000万 |
            | `use_regime_filter` | 市场环境过滤 | true/false | true |
            """)

        with gr.Accordion("🛡️ 风控参数", open=False):
            gr.Markdown("""
            | 参数名 | 含义 | 常用范围 | 当前值 |
            |--------|------|----------|--------|
            | `max_position_pct` | 单股最大持仓权重 | 10%-30% | 25% |
            | `max_daily_loss_pct` | 单日最大亏损止损 | 3%-10% | 5% |
            """)

        with gr.Accordion("💰 回测参数（交易成本）", open=False):
            gr.Markdown("""
            | 参数名 | 含义 | 常用范围 | 当前值 |
            |--------|------|----------|--------|
            | `initial_cash` | 初始资金 | 10万-1000万 | 100万 |
            | `commission_rate` | 佣金率 | 0.01%-0.05% | 0.025% |
            | `stamp_tax_rate` | 印花税(卖出) | 0.05% | 0.05% |
            | `slippage_rate` | 滑点率 | 0.05%-0.2% | 0.1% |

            **交易成本影响估算：** 换手率200%策略，年化成本≈200%×0.25%=0.5%
            """)

        with gr.Accordion("📊 绩效指标", open=False):
            gr.Markdown("""
            | 指标 | 含义 | 优秀标准 |
            |------|------|----------|
            | **CAGR** | 年化复合收益率 | > 15% |
            | **夏普比率** | (年化收益-无风险)/年化波动 | > 1.0 |
            | **最大回撤** | 峰值到谷底最大亏损 | < -20% |
            | **卡玛比率** | CAGR / \\|MaxDD\\| | > 1.0 |
            | **信息比率** | 超额年化 / 跟踪误差 | > 0.5 |
            """)

        with gr.Accordion("🔬 因子指标", open=False):
            gr.Markdown("""
            | 指标 | 含义 | 有效标准 |
            |------|------|----------|
            | **IC** | 因子与未来收益的Spearman相关 | \\|IC\\| > 0.03 |
            | **ICIR** | mean(IC)/std(IC) | \\|ICIR\\| > 0.5 |
            | **IC正率** | IC>0的月份占比 | > 55% |
            | **多空收益** | 头部组-尾部组收益差 | > 0 |
            """)

        with gr.Accordion("🛡️ 风险指标", open=False):
            gr.Markdown("""
            | 指标 | 含义 | 安全范围 |
            |------|------|----------|
            | **VaR(95%)** | 95%置信度最大日亏损 | 越小越好 |
            | **CVaR(95%)** | 超过VaR时的期望亏损 | 越小越好 |
            | **HHI** | Σ(权重²)，持仓集中度 | < 0.1 |
            | **有效持仓数** | 1/HHI | > 10 |
            """)

        with gr.Accordion("🔧 参数调整指南", open=False):
            gr.Markdown("""
            | 问题 | 调整方向 | 参数 |
            |------|----------|------|
            | 回撤太大 | 降低仓位/启用风控 | ↓ max_position_pct |
            | 交易成本高 | 降低换手率 | ↑ rebalance_days |
            | 收益太低 | 提高选股能力 | 优化因子/↑ top_k |
            | 波动太大 | 增加分散度 | ↑ top_k, ↓ max_weight_pct |
            | 实盘与回测差距大 | 检查成本假设 | ↑ slippage_rate |
            """)

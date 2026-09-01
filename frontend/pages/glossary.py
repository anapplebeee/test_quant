"""参数词典页面"""
from __future__ import annotations

import gradio as gr

from api.config_api import get_config_snapshot
from frontend.theme import page_header


def render():
    """渲染参数词典 Tab"""
    snapshot = get_config_snapshot()
    s = snapshot["strategy"]
    r = snapshot["risk"]
    b = snapshot["backtest"]
    # 当前生效值 = 默认策略（config.strategy.name）解析后的参数，与回测/信号同源
    effective = snapshot["effective"]
    cur_top_k = effective["top_k"]
    cur_rebalance = effective["rebalance_days"]
    cur_regime = effective["use_regime_filter"]

    with gr.Tab("📖 参数词典"):
        gr.HTML(page_header("📖 量化参数词典", "所有关键参数的含义、计算方法和经验取值"))
        gr.Markdown(
            f"> 当前生效值来自 `config/settings.yaml`，默认策略 `{s.get('name')}`（"
            f"与回测中心/信号生成同源，2026-08-31 起不再硬编码）"
        )

        with gr.Accordion("⚙️ 策略参数", open=True):
            gr.Markdown(
                f"""
            | 参数名 | 含义 | 常用范围 | 当前值 |
            |--------|------|----------|--------|
            | `lookback_days` | 动量回看天数 | 20-252天 | {s.get('lookback_days', 60)} |
            | `top_k` | 持仓股票数 | 5-50只 | {cur_top_k} |
            | `rebalance_days` | 调仓周期 | 5-60天 | {cur_rebalance} |
            | `max_weight_pct` | 单股最大权重 | 5%-20% | {s.get('max_weight_pct', 0.15) * 100:.0f}% |
            | `min_avg_amount` | 流动性门槛 | 1000万-1亿 | {s.get('min_avg_amount', 50_000_000) / 1e4:.0f}万 |
            | `use_regime_filter` | 市场环境过滤 | true/false | {str(cur_regime).lower()} |
            | `live_allowlist` | 实盘准入白名单（须有门禁 PASS） | - | {s.get('live_allowlist') or '无（空=无策略准入）'} |
            | `paper_allowlist` | Paper 模拟盘候选白名单 | - | {s.get('paper_allowlist') or '无'} |
            """
            )

        with gr.Accordion("🛡️ 风控参数", open=False):
            gr.Markdown(
                f"""
            | 参数名 | 含义 | 常用范围 | 当前值 |
            |--------|------|----------|--------|
            | `max_position_pct` | 单股最大持仓权重 | 10%-30% | {r.get('max_position_pct', 0.25) * 100:.0f}% |
            | `max_daily_loss_pct` | 单日最大亏损止损 | 3%-10% | {r.get('max_daily_loss_pct', 0.05) * 100:.0f}% |
            """
            )

        with gr.Accordion("💰 回测参数（交易成本）", open=False):
            gr.Markdown(
                f"""
            | 参数名 | 含义 | 常用范围 | 当前值 |
            |--------|------|----------|--------|
            | `initial_cash` | 初始资金 | 10万-1000万 | {b.get('initial_cash', 1_000_000):,.0f} |
            | `commission_rate` | 佣金率 | 0.01%-0.05% | {b.get('commission_rate', 0.00025) * 100:.3f}% |
            | `stamp_tax_rate` | 印花税(卖出) | 0.05% | {b.get('stamp_tax_rate', 0.0005) * 100:.2f}% |
            | `slippage_rate` | 滑点率 | 0.05%-0.2% | {b.get('slippage_rate', 0.001) * 100:.1f}% |
            | `min_order_value` | 最小委托金额 | - | {b.get('min_order_value', 1000):,.0f} |

            **交易成本影响估算：** 换手率200%策略，年化成本≈200%×0.25%=0.5%（佣金）
            """
            )

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

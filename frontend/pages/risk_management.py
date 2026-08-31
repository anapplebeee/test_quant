"""风险管理页面 - VaR/CVaR/集中度/流动性

所有指标附带详细注释说明，空仓时显示方法论介绍。
"""
from __future__ import annotations

import gradio as gr
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from api.data_api import get_stock_names
from api.portfolio_api import current_holdings, holding_bars, holding_price_frame
from frontend.theme import page_header

# ---------- 指标说明常量 ----------

RISK_METRICS_DOC = """
### 📐 风险指标方法论

| 指标 | 全称 | 计算公式 | 含义 | 安全范围 |
|------|------|----------|------|----------|
| **HHI** | Herfindahl-Hirschman Index | Σ(权重ᵢ²) | 持仓集中度。等权10只=0.10，等权20只=0.05，单只100%=1.0 | < 0.10 |
| **有效持仓数** | Effective N | 1 / HHI | 等效等权持仓数量，越分散风险越低 | > 10 |
| **VaR(95%)** | Value at Risk | 日收益分布的5%分位数 | 95%置信度下单日最大亏损。VaR=-2%表示95%概率日亏损不超过2% | 越小越好 |
| **CVaR(95%)** | Conditional VaR (Expected Shortfall) | 低于VaR阈值的平均收益 | 尾部风险的期望亏损，比VaR更关注极端情况 | 越小越好 |
| **变现天数** | Days to Liquidate | 持仓市值 / 20日均成交额 | 全部卖出需要的天数，衡量流动性风险 | < 3 天 |
| **最大单股权重** | Max Position Weight | 最大持仓市值 / 总资产 | 个股集中度风险 | < 15% |
| **前5大占比** | Top-5 Concentration | 前5大持仓权重之和 | 头部集中度 | < 50% |
"""

HOLDING_STATUS_DOC = """
### 💡 为什么风险管理重要？

**集中度风险**：个股暴雷时，10%持仓损失1%净值，50%持仓损失5%净值。

**流动性风险**：如果持仓市值是日均成交额的10倍，紧急卖出需要10天，
期间可能承受更大的价格冲击（滑点可达1%-5%）。

**尾部风险**：VaR不告诉你剩下5%的极端情况有多糟，CVaR补充了这个盲区。
2008年金融危机中，很多"99% VaR"模型都失效了——黑天鹅总是超出历史分布。
"""

EMPTY_STATE_HTML = """
<div style="padding: 2rem; text-align: center; background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            border-radius: 12px; margin: 1rem 0;">
    <h2 style="color: #455A64; margin: 0 0 0.5rem 0;">📭 当前空仓</h2>
    <p style="color: #607D8B; margin: 0;">
        没有持仓数据，无法计算实际风险指标。<br>
        以下展示各风险指标的计算方法和判断标准，供研究参考。
    </p>
</div>
"""

RISK_LEVEL_STYLES = {
    "low": ("🟢 低", "background: #E8F5E9; border-left: 4px solid #43A047; padding: 0.8rem 1rem; border-radius: 4px; color: #2E7D32;"),
    "mid": ("🟡 中", "background: #FFF3E0; border-left: 4px solid #FB8C00; padding: 0.8rem 1rem; border-radius: 4px; color: #E65100;"),
    "high": ("🔴 高", "background: #FFEBEE; border-left: 4px solid #E53935; padding: 0.8rem 1rem; border-radius: 4px; color: #C62828;"),
}


def _risk_banner(level: str, detail: str) -> str:
    """风险等级横幅"""
    label, style = RISK_LEVEL_STYLES[level]
    return f'<div style="{style}"><b>{label} 风险</b> — {detail}</div>'


def _metric_row_html(items: list[tuple[str, str, str, str]]) -> str:
    """带注释的指标卡行: (label, value, tooltip, color)"""
    cards = []
    for label, value, tooltip, color in items:
        cards.append(f"""
        <div style="background: white; border: 1px solid #E0E0E0; border-radius: 8px;
                    padding: 0.9rem; text-align: center; position: relative;">
            <div style="font-size: 0.8rem; color: #78909C;">{label}
                <span title="{tooltip}" style="cursor: help; color: #B0BEC5;">ⓘ</span>
            </div>
            <div style="font-size: 1.6rem; font-weight: bold; color: #{color}; margin-top: 0.3rem;">{value}</div>
        </div>""")
    cols = "".join(
        f'<div style="flex:1; margin: 0 4px;">{c}</div>' for c in cards
    )
    return f'<div style="display: flex; margin: 0.5rem 0;">{cols}</div>'


def _get_holdings():
    """通过组合 API 读取持仓与现金。"""
    positions, cash = current_holdings()
    return (positions or None), cash


def _load_prices(positions: dict) -> pd.DataFrame:
    """加载持仓最新价格。

    2026-08-31 修复：存储已迁移为 year=YYYY 分区布局，旧 per-symbol
    parquet 路径不存在导致本页全部显示"数据缺失"；统一改走 BarStore。
    """
    return holding_price_frame(positions)


def _load_bars_frame(symbols: list) -> pd.DataFrame:
    """统一 BarStore 分区查询（兼容新旧布局），返回长表 date/symbol/close/amount。"""
    return holding_bars([str(symbol) for symbol in symbols])


def _build_var_chart(returns: pd.Series) -> go.Figure:
    """构建 VaR 分布图"""
    var_95 = np.percentile(returns, 5)
    cvar_95 = returns[returns <= var_95].mean()

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=returns.values * 100, nbinsx=40,
        marker_color="#5C6BC0", opacity=0.8, name="日收益",
    ))
    fig.add_vline(x=var_95 * 100, line_dash="dash", line_color="#E53935", line_width=2,
                  annotation_text=f"VaR(95%)={var_95*100:.2f}%",
                  annotation_position="left")
    fig.add_vline(x=cvar_95 * 100, line_dash="dot", line_color="#B71C1C", line_width=2,
                  annotation_text=f"CVaR={cvar_95*100:.2f}%",
                  annotation_position="left")
    fig.update_layout(
        title="组合日收益分布（尾部标注 VaR / CVaR）",
        xaxis_title="日收益 (%)", yaxis_title="频次",
        height=380, margin=dict(l=10, r=10, t=50, b=10),
        bargap=0.05,
        template="plotly_white",
    )
    return fig


def render():
    """渲染风险管理 Tab"""
    with gr.Tab("🛡️ 风险管理"):
        gr.HTML(page_header("🛡️ 风险管理",
                            "VaR/CVaR · 集中度 · 流动性 — 所有指标附方法论注释"))

        positions, cash = _get_holdings()

        # ================= 有持仓：计算实际指标 =================
        if positions:
            pos_df = _load_prices(positions)
            cash = cash or 0
            total = pos_df["value"].sum() + cash
            weights = pos_df["value"] / total if total > 0 else pd.Series(dtype=float)

            hhi = float((weights ** 2).sum())
            effective_n = 1 / hhi if hhi > 0 else 0
            max_w = float(weights.max()) if len(weights) else 0
            top5 = float(weights.nlargest(5).sum())
            cash_pct = cash / total if total > 0 else 0

            # 风险等级横幅
            if hhi > 0.2 or max_w > 0.25:
                gr.HTML(_risk_banner("high", f"HHI={hhi:.3f} 或最大权重={max_w*100:.1f}% 超阈值"))
            elif hhi > 0.1 or max_w > 0.15:
                gr.HTML(_risk_banner("mid", f"HHI={hhi:.3f}，建议增加分散度"))
            else:
                gr.HTML(_risk_banner("low", "集中度和权重在安全范围内"))

            # ---- 集中度指标（带注释卡片） ----
            gr.Markdown("### 📊 集中度指标")
            gr.HTML(_metric_row_html([
                ("HHI 集中度 ⓘ", f"{hhi:.3f}",
                 "Herfindahl指数=Σ(权重²)。等权10只=0.10。越低越分散，建议<0.10",
                 "E65100" if hhi > 0.1 else "2E7D32"),
                ("有效持仓数 ⓘ", f"{effective_n:.1f}",
                 "有效持仓数=1/HHI。表示等效等权持仓的分散程度，建议>10",
                 "1565C0" if effective_n > 10 else "E65100"),
                ("最大单股权重 ⓘ", f"{max_w*100:.1f}%",
                 "最大持仓/总资产。个股暴雷直接影响，建议<15%",
                 "C62828" if max_w > 0.15 else "2E7D32"),
                ("前5大占比 ⓘ", f"{top5*100:.1f}%",
                 "前5大持仓权重之和，衡量头部集中度，建议<50%",
                 "6A1B9A"),
                ("现金比例 ⓘ", f"{cash_pct*100:.1f}%",
                 "现金占总资产比例，反映可用加仓空间",
                 "2E7D32"),
            ]))

            # ---- 持仓权重明细表 ----
            with gr.Accordion("📋 持仓权重明细（点击展开）", open=False):
                names = get_stock_names()
                detail = pos_df.copy()
                detail["名称"] = detail["code"].map(names).fillna("-")
                detail["权重%"] = (detail["value"] / total * 100).round(1)
                detail = detail.rename(columns={
                    "code": "代码", "shares": "持股数",
                    "price": "最新价", "value": "市值"})
                detail = detail.sort_values("权重%", ascending=False)
                gr.Dataframe(value=detail, interactive=False)

            # ---- VaR/CVaR ----
            gr.Markdown("### 📉 VaR / CVaR 风险估算")
            gr.Markdown("""
            - **VaR(95%)** ⓘ：日收益分布的5%分位数。`VaR=-2%` 表示 95% 的交易日亏损不超过 2%
            - **CVaR(95%)** ⓘ：超过 VaR 时的期望亏损（尾部均值），比 VaR 更保守
            - **年化VaR** ⓘ：日VaR × √252，将风险外推到年度尺度
            - *计算方法：历史模拟法，基于近60个交易日收益*
            """)

            returns_data = []
            bars_frame = _load_bars_frame(list(positions))
            if not bars_frame.empty:
                for sym, group in bars_frame.groupby("symbol"):
                    close = pd.Series(group["close"].astype(float).values)
                    if len(close) > 60:
                        returns_data.append(
                            close.pct_change().dropna().tail(60).rename(str(sym))
                        )

            if returns_data:
                ret_df = pd.concat(returns_data, axis=1).fillna(0)
                # 用实际权重加权组合收益
                aligned = ret_df[[c for c in pos_df["code"] if c in ret_df.columns]]
                if aligned.empty:
                    gr.Info("持仓历史数据不足60天，无法计算VaR")
                else:
                    w = pos_df.set_index("code").loc[aligned.columns, "value"]
                    w = w / w.sum()
                    portfolio_ret = (aligned * w.values).sum(axis=1)

                    var_95 = np.percentile(portfolio_ret, 5)
                    cvar_95 = portfolio_ret[portfolio_ret <= var_95].mean()
                    annual_var = var_95 * np.sqrt(252)

                    gr.HTML(_metric_row_html([
                        ("日 VaR(95%) ⓘ", f"{var_95*100:.2f}%",
                         "95%置信度下单日最大亏损，基于历史模拟法", "C62828"),
                        ("日 CVaR(95%) ⓘ", f"{cvar_95*100:.2f}%",
                         "超过VaR时的尾部期望亏损（Expected Shortfall）", "B71C1C"),
                        ("年化 VaR ⓘ", f"{annual_var*100:.1f}%",
                         "日VaR×√252，年化尺度风险外推", "6A1B9A"),
                    ]))
                    gr.Plot(value=_build_var_chart(portfolio_ret))
            else:
                gr.Info("持仓历史数据不足60天，无法计算VaR")

            # ---- 流动性 ----
            gr.Markdown("### 💧 流动性风险")
            gr.Markdown("""
            - **变现天数** ⓘ = 持仓市值 / 20日均成交额。建议 **< 3天**
              - 例：持仓100万，日均成交50万 → 需2天全部卖出
              - 变现天数>5天意味着紧急减仓会有显著冲击成本
            - **Amihud非流动性** ⓘ = |日收益| / 日成交额，单位金额的价格冲击，越小越好
            """)

            names = get_stock_names()
            liq_rows = []
            if not bars_frame.empty:
                for sym, shares in positions.items():
                    group = bars_frame[bars_frame["symbol"].astype(str) == str(sym)]
                    if len(group) > 20:
                        try:
                            close = pd.Series(group["close"].astype(float).values)
                            amount = pd.Series(group["amount"].astype(float).values)
                            price = float(close.iloc[-1])
                            value = shares * price
                            avg_amt = float(amount.tail(20).mean())
                            days = value / avg_amt if avg_amt > 0 else float("inf")
                            # Amihud
                            ret = close.pct_change().abs()
                            amihud = (ret / amount).tail(60).mean() * 1e9
                            liq_rows.append({
                                "代码": sym, "名称": names.get(sym, "-"),
                                "持仓市值": f"{value:,.0f}",
                                "20日均额": f"{avg_amt:,.0f}",
                                "变现天数": f"{days:.1f}",
                                "Amihud ⓘ": f"{amihud:.2f}",
                            })
                        except Exception:
                            pass

            if liq_rows:
                liq_df = pd.DataFrame(liq_rows).sort_values("变现天数", ascending=False)
                gr.Dataframe(value=liq_df, interactive=False)
                n_bad = sum(1 for r in liq_rows if float(r["变现天数"]) > 3)
                if n_bad:
                    gr.Warning(f"⚠️ {n_bad} 只持仓变现天数 > 3 天，存在流动性风险")
                else:
                    gr.Success("✅ 所有持仓变现天数 < 3 天，流动性良好")

        # ================= 空仓：显示方法论 =================
        else:
            gr.HTML(EMPTY_STATE_HTML)
            gr.HTML(RISK_METRICS_DOC)
            gr.HTML(HOLDING_STATUS_DOC)

            with gr.Accordion("🛡️ 风控参数说明", open=True):
                gr.Markdown("""
                | 参数 | 含义 | 建议值 | 触发动作 |
                |------|------|--------|----------|
                | `max_position_pct` ⓘ | 单股最大持仓权重 | ≤ 25% | 超过时减仓至阈值内 |
                | `max_daily_loss_pct` ⓘ | 单日最大亏损比例 | ≤ 5% | 触发时次日暂停开仓 |
                | `min_avg_amount` ⓘ | 最低日均成交额 | ≥ 3000万 | 不满足的股票直接过滤 |
                | `max_weight_pct` ⓘ | 策略单股目标权重 | ≤ 15% | 组合构建时限制 |
                """)

            with gr.Accordion("📖 指标解读示例", open=False):
                gr.Markdown("""
                **示例1：HHI 判断**
                - 等权持有10只股票：HHI = 10 × (0.1)² = **0.10** → 分散度合格
                - 5只各18% + 3只各3.3%：HHI ≈ **0.17** → 集中度偏高
                - 单只100%：HHI = **1.0** → 极端集中（不建议）

                **示例2：VaR 解读**
                - 日VaR(95%) = -2.5%：正常市场下，95%的交易日亏损不超过2.5%
                - 若组合100万 → 单日最大预期亏损约 2.5万（95%概率）

                **示例3：变现天数**
                - 持仓200万，该股日均成交额40万 → 变现天数 = 200/40 = **5天**
                - 5天 > 3天阈值 → 该股流动性不足，建议减仓或替换
                """)

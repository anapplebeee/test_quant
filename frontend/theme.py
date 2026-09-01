"""Gradio 前端视觉系统与共享展示组件。

页面只负责组合业务组件；颜色、间距、卡片、标题和状态语义集中在这里，
避免各页面继续扩散内联 CSS。所有 helper 均为纯函数，便于单元测试。
"""
from __future__ import annotations

from collections.abc import Iterable
from html import escape

import gradio as gr

CUSTOM_CSS = r"""
:root {
    --quart-navy: #0f2747;
    --quart-blue: #2563eb;
    --quart-cyan: #0891b2;
    --quart-green: #15803d;
    --quart-red: #dc2626;
    --quart-amber: #d97706;
    --quart-purple: #7c3aed;
    --quart-slate: #475569;
    --quart-border: rgba(148, 163, 184, 0.28);
    --quart-shadow: 0 10px 30px rgba(15, 39, 71, 0.08);
}

body {
    background:
        radial-gradient(circle at 8% 0%, rgba(37, 99, 235, 0.07), transparent 26rem),
        var(--body-background-fill);
}

.gradio-container {
    max-width: 1560px !important;
    padding: 20px 24px 36px !important;
    font-family: Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif !important;
}

/* 平台级页眉 */
.platform-shell {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 24px;
    padding: 18px 22px;
    margin-bottom: 14px;
    color: #f8fafc;
    background: linear-gradient(115deg, #0b1f3a 0%, #123b68 58%, #075985 100%);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 18px;
    box-shadow: 0 16px 44px rgba(15, 39, 71, 0.20);
}
.platform-brand { display: flex; align-items: center; gap: 14px; min-width: 0; }
.platform-logo {
    display: grid;
    place-items: center;
    width: 42px;
    height: 42px;
    flex: 0 0 42px;
    border-radius: 12px;
    background: rgba(255, 255, 255, 0.14);
    border: 1px solid rgba(255, 255, 255, 0.22);
    font-size: 22px;
    font-weight: 800;
}
.platform-title { margin: 0; font-size: 1.18rem; font-weight: 750; letter-spacing: .02em; }
.platform-subtitle { margin-top: 3px; color: #bfdbfe; font-size: .84rem; }
.platform-meta { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.platform-chip {
    padding: 6px 10px;
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 999px;
    color: #dbeafe;
    background: rgba(255, 255, 255, 0.08);
    font-size: .76rem;
    white-space: nowrap;
}

/* 顶层 Tab：固定在视口上缘并允许横向滚动，13 个页面不再挤压换行。 */
#platform-tabs > .tab-nav,
#platform-tabs [role="tablist"] {
    position: sticky;
    top: 0;
    z-index: 50;
    display: flex;
    gap: 4px;
    overflow-x: auto;
    padding: 8px;
    margin-bottom: 14px;
    border: 1px solid var(--quart-border);
    border-radius: 14px;
    background: color-mix(in srgb, var(--background-fill-primary) 92%, transparent);
    box-shadow: 0 8px 24px rgba(15, 39, 71, 0.07);
    backdrop-filter: blur(14px);
}
#platform-tabs [role="tab"] {
    flex: 0 0 auto;
    min-height: 36px;
    padding: 8px 13px;
    border-radius: 9px;
    font-size: .86rem;
    font-weight: 620;
    white-space: nowrap;
}

/* 页面标题：压缩高度，把首屏留给关键数据。 */
.page-header {
    position: relative;
    overflow: hidden;
    padding: 22px 24px;
    margin: 4px 0 18px;
    border: 1px solid rgba(37, 99, 235, 0.16);
    border-radius: 16px;
    background:
        linear-gradient(110deg, rgba(37, 99, 235, 0.10), rgba(8, 145, 178, 0.04)),
        var(--background-fill-primary);
}
.page-header::after {
    content: "";
    position: absolute;
    right: -50px;
    top: -80px;
    width: 230px;
    height: 230px;
    border-radius: 50%;
    background: rgba(37, 99, 235, 0.07);
}
.page-eyebrow {
    position: relative;
    z-index: 1;
    margin-bottom: 6px;
    color: var(--quart-blue);
    font-size: .70rem;
    font-weight: 800;
    letter-spacing: .14em;
    text-transform: uppercase;
}
.page-header h1 {
    position: relative;
    z-index: 1;
    margin: 0;
    color: var(--body-text-color);
    font-size: clamp(1.35rem, 2vw, 1.75rem);
    line-height: 1.25;
    font-weight: 760;
}
.page-header p {
    position: relative;
    z-index: 1;
    max-width: 920px;
    margin: 7px 0 0;
    color: var(--body-text-color-subdued);
    font-size: .92rem;
    line-height: 1.65;
}

.section-heading { margin: 20px 0 10px; }
.section-kicker { color: var(--quart-blue); font-size: .68rem; font-weight: 800; letter-spacing: .12em; }
.section-heading h2 { margin: 3px 0 0; font-size: 1.05rem; font-weight: 730; }
.section-heading p { margin: 4px 0 0; color: var(--body-text-color-subdued); font-size: .84rem; }

/* 指标卡：白底、细边框、语义色顶边，适合长时间研究阅读。 */
.metric-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px;
    margin: 8px 0 14px;
}
.metric-flex { display: flex; flex-wrap: wrap; gap: 10px; margin: 8px 0 14px; }
.metric-card {
    min-width: 0;
    padding: 14px 15px 13px;
    border: 1px solid var(--quart-border);
    border-top: 3px solid var(--metric-accent, var(--quart-blue));
    border-radius: 12px;
    color: var(--body-text-color);
    background: var(--background-fill-primary);
    box-shadow: 0 4px 14px rgba(15, 39, 71, 0.05);
}
.metric-card h3 {
    margin: 0;
    color: var(--body-text-color-subdued);
    font-size: .74rem;
    font-weight: 650;
    line-height: 1.25;
}
.metric-card p {
    margin: 6px 0 0;
    overflow: hidden;
    color: var(--body-text-color);
    font-size: clamp(1.08rem, 1.8vw, 1.46rem);
    font-weight: 760;
    line-height: 1.2;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.metric-card small { display: block; margin-top: 6px; color: var(--body-text-color-subdued); font-size: .69rem; }
.metric-blue { --metric-accent: var(--quart-blue); }
.metric-green { --metric-accent: var(--quart-green); }
.metric-red { --metric-accent: var(--quart-red); }
.metric-purple { --metric-accent: var(--quart-purple); }
.metric-teal { --metric-accent: var(--quart-cyan); }
.metric-orange { --metric-accent: var(--quart-amber); }
.metric-gray { --metric-accent: var(--quart-slate); }

.info-card,
.content-card,
.assumption-panel {
    padding: 16px 18px;
    margin: 8px 0 14px;
    border: 1px solid var(--quart-border);
    border-radius: 13px;
    background: var(--background-fill-primary);
    box-shadow: 0 5px 18px rgba(15, 39, 71, 0.05);
}
.info-card-title { margin-bottom: 8px; font-weight: 720; }
.muted { color: var(--body-text-color-subdued); }
.microcopy { color: var(--body-text-color-subdued); font-size: .78rem; line-height: 1.55; }
.quick-nav-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px;
}
.quick-nav-card {
    padding: 13px;
    border: 1px solid var(--quart-border);
    border-radius: 11px;
    background: var(--background-fill-primary);
    cursor: pointer;
    transition: border-color .16s ease, box-shadow .16s ease;
}
.quick-nav-card:hover { border-color: rgba(37, 99, 235, .45); box-shadow: 0 6px 18px rgba(15, 39, 71, .08); }
.quick-nav-icon { margin-bottom: 5px; font-size: 1.15rem; }
.quick-nav-label { font-size: .84rem; font-weight: 700; }
.quick-nav-detail { margin-top: 2px; color: var(--body-text-color-subdued); font-size: .72rem; }

.assumption-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 8px 18px;
}
.assumption-item { min-width: 0; padding: 8px 0; border-bottom: 1px dashed var(--quart-border); }
.assumption-item span { display: block; color: var(--body-text-color-subdued); font-size: .70rem; }
.assumption-item strong { display: block; margin-top: 3px; font-size: .84rem; font-weight: 680; }

.risk-banner {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px 14px;
    margin: 8px 0;
    border: 1px solid var(--quart-border);
    border-left: 4px solid currentColor;
    border-radius: 10px;
    font-size: .86rem;
    font-weight: 550;
}
.risk-low { color: var(--quart-green); background: rgba(21, 128, 61, .07); }
.risk-mid { color: var(--quart-amber); background: rgba(217, 119, 6, .08); }
.risk-high { color: var(--quart-red); background: rgba(220, 38, 38, .07); }

.status-indicator {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 9px;
    border: 1px solid currentColor;
    border-radius: 999px;
    font-size: .72rem;
    font-weight: 700;
}
.status-active { color: var(--quart-green); background: rgba(21, 128, 61, .07); }
.status-inactive { color: var(--quart-slate); background: rgba(71, 85, 105, .07); }
.status-warning { color: var(--quart-amber); background: rgba(217, 119, 6, .08); }
.status-error { color: var(--quart-red); background: rgba(220, 38, 38, .07); }

.step-indicator {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    margin: 8px 0 12px;
    border-left: 3px solid var(--quart-blue);
    border-radius: 0 9px 9px 0;
    color: var(--body-text-color);
    background: rgba(37, 99, 235, .07);
    font-weight: 680;
}
.step-number {
    display: inline-grid;
    place-items: center;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    color: white;
    background: var(--quart-blue);
    font-size: .72rem;
}

/* Gradio 原生组件统一格式 */
.gradio-dataframe,
[data-testid="dataframe"] {
    overflow: hidden;
    border: 1px solid var(--quart-border) !important;
    border-radius: 12px !important;
    box-shadow: 0 4px 16px rgba(15, 39, 71, .045);
}
.gradio-dataframe th { font-weight: 700 !important; }
.gradio-dataframe td,
.gradio-dataframe th { font-variant-numeric: tabular-nums; }

button {
    border-radius: 9px !important;
    font-weight: 650 !important;
    transition: border-color .16s ease, background .16s ease, box-shadow .16s ease !important;
}
button:hover { box-shadow: 0 4px 12px rgba(15, 39, 71, .10) !important; }

.gradio-accordion {
    margin-bottom: 10px;
    overflow: hidden;
    border-color: var(--quart-border) !important;
    border-radius: 12px !important;
    box-shadow: 0 3px 14px rgba(15, 39, 71, .04);
}
.gradio-accordion > .label-wrap { font-weight: 680; }

.plot-container,
.gradio-plot {
    overflow: hidden;
    border: 1px solid var(--quart-border);
    border-radius: 12px;
    background: var(--background-fill-primary);
}

hr {
    height: 1px;
    margin: 20px 0;
    border: 0;
    background: var(--quart-border);
}

.app-footer {
    padding: 16px 4px 0;
    color: var(--body-text-color-subdued);
    font-size: .73rem;
    text-align: center;
}

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { border-radius: 8px; background: rgba(100, 116, 139, .42); }

@media (max-width: 760px) {
    .gradio-container { padding: 10px 10px 24px !important; }
    .platform-shell { align-items: flex-start; padding: 15px; border-radius: 14px; }
    .platform-meta { display: none; }
    .page-header { padding: 17px 16px; }
    .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .metric-card { padding: 12px; }
    .assumption-grid { grid-template-columns: 1fr; }
}
"""


def metric_card(label: str, value: str, color: str = "blue", helper: str = "") -> str:
    """返回统一指标卡 HTML；文本转义，避免数据值注入页面结构。"""
    helper_html = f"<small>{escape(str(helper))}</small>" if helper else ""
    return (
        f'<div class="metric-card metric-{escape(str(color))}">'
        f"<h3>{escape(str(label))}</h3>"
        f"<p title=\"{escape(str(value), quote=True)}\">{escape(str(value))}</p>"
        f"{helper_html}</div>"
    )


def metric_grid(cards: Iterable[str]) -> str:
    """把若干 ``metric_card`` 组合成响应式等宽网格。"""
    return '<div class="metric-grid">' + "".join(cards) + "</div>"


def page_header(title: str, subtitle: str = "", eyebrow: str = "QUANT WORKSPACE") -> str:
    """创建紧凑、统一的页面标题。"""
    subtitle_html = f"<p>{escape(str(subtitle))}</p>" if subtitle else ""
    return (
        '<div class="page-header">'
        f'<div class="page-eyebrow">{escape(str(eyebrow))}</div>'
        f"<h1>{escape(str(title))}</h1>{subtitle_html}</div>"
    )


def section_header(title: str, description: str = "", eyebrow: str = "") -> str:
    """页面内二级信息区标题。"""
    kicker = f'<div class="section-kicker">{escape(str(eyebrow))}</div>' if eyebrow else ""
    desc = f"<p>{escape(str(description))}</p>" if description else ""
    return f'<div class="section-heading">{kicker}<h2>{escape(str(title))}</h2>{desc}</div>'


def platform_shell() -> str:
    """应用级品牌与运行边界说明。"""
    return """
    <div class="platform-shell">
      <div class="platform-brand">
        <div class="platform-logo">Q</div>
        <div>
          <div class="platform-title">Quart 量化研究平台</div>
          <div class="platform-subtitle">数据 · 研究 · 回测 · 风控 · 手动执行</div>
        </div>
      </div>
      <div class="platform-meta">
        <span class="platform-chip">A 股日频</span>
        <span class="platform-chip">T+1 执行</span>
        <span class="platform-chip">PIT / OOS 审计</span>
      </div>
    </div>
    """


def app_footer() -> str:
    return (
        '<div class="app-footer">研究与模拟工具 · 历史结果不代表未来表现 · '
        "任何策略上线前必须通过数据、成本、容量与样本外门禁</div>"
    )


def info_card(content: str, title: str = "") -> str:
    """创建信息卡；``content`` 允许传入受控 HTML。"""
    head = f'<div class="info-card-title">{escape(str(title))}</div>' if title else ""
    return f'<div class="info-card">{head}{content}</div>'


def step_header(step_num: int, title: str) -> str:
    return (
        '<div class="step-indicator">'
        f'<span class="step-number">{int(step_num)}</span>'
        f"<span>{escape(str(title))}</span></div>"
    )


def risk_banner(level: str, detail: str) -> str:
    labels = {"low": "低风险", "mid": "中风险", "high": "高风险"}
    safe_level = level if level in labels else "mid"
    return (
        f'<div class="risk-banner risk-{safe_level}"><b>{labels[safe_level]}</b>'
        f"<span>{escape(str(detail))}</span></div>"
    )


def status_badge(status: str) -> str:
    labels = {
        "active": "正常",
        "inactive": "未运行",
        "warning": "需关注",
        "error": "异常",
    }
    safe_status = status if status in labels else "inactive"
    return (
        f'<span class="status-indicator status-{safe_status}">'
        f"{labels[safe_status]}</span>"
    )


DEMO_BANNER = (
    '<div class="risk-banner risk-mid"><b>演示数据</b>'
    "<span>本页当前展示占位或随机数据，请勿用于交易决策。</span></div>"
)


def soft_theme() -> gr.themes.Base:
    """偏研究终端风格的浅色/深色兼容主题。"""
    return gr.themes.Soft(
        primary_hue="blue",
        secondary_hue="slate",
        neutral_hue="slate",
        spacing_size="md",
        radius_size="md",
        text_size="md",
    ).set(
        body_background_fill="#f6f8fc",
        body_background_fill_dark="#0b1220",
        background_fill_primary="#ffffff",
        background_fill_primary_dark="#111827",
        background_fill_secondary="#f1f5f9",
        background_fill_secondary_dark="#172033",
        block_border_color="#dbe3ee",
        block_border_color_dark="#2b3950",
        block_shadow="0 4px 16px rgba(15, 39, 71, 0.05)",
        button_primary_background_fill="#2563eb",
        button_primary_background_fill_hover="#1d4ed8",
        button_primary_border_color="#2563eb",
        button_transform_hover="none",
        button_transform_active="translateY(1px)",
        input_border_color="#cbd5e1",
        input_border_color_dark="#3b4a62",
        input_border_color_focus="#2563eb",
        table_row_focus="rgba(37, 99, 235, 0.08)",
        table_row_focus_dark="rgba(96, 165, 250, 0.12)",
    )


__all__ = [
    "CUSTOM_CSS",
    "DEMO_BANNER",
    "app_footer",
    "info_card",
    "metric_card",
    "metric_grid",
    "page_header",
    "platform_shell",
    "risk_banner",
    "section_header",
    "soft_theme",
    "status_badge",
    "step_header",
]

"""前端主题和共享组件"""
from __future__ import annotations

import gradio as gr

CUSTOM_CSS = """
/* 主容器 */
.gradio-container {
    max-width: 1400px !important;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

/* 指标卡片 */
.metric-card {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    padding: 1.2rem;
    border-radius: 0.8rem;
    color: white;
    text-align: center;
    margin: 0.5rem 0;
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.metric-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 12px rgba(0, 0, 0, 0.15);
}
.metric-card h3 { margin: 0; font-size: 0.9rem; opacity: 0.9; font-weight: 500; }
.metric-card p { margin: 0.5rem 0 0 0; font-size: 1.8rem; font-weight: bold; }
.metric-blue { background: linear-gradient(135deg, #1E88E5 0%, #1565C0 100%); }
.metric-green { background: linear-gradient(135deg, #43A047 0%, #2E7D32 100%); }
.metric-red { background: linear-gradient(135deg, #E53935 0%, #C62828 100%); }
.metric-purple { background: linear-gradient(135deg, #8E24AA 0%, #6A1B9A 100%); }
.metric-teal { background: linear-gradient(135deg, #00897B 0%, #00695C 100%); }
.metric-orange { background: linear-gradient(135deg, #FB8C00 0%, #EF6C00 100%); }
.metric-gray { background: linear-gradient(135deg, #78909C 0%, #546E7A 100%); }

/* 页面头 */
.page-header {
    background: linear-gradient(135deg, #1A1A2E 0%, #16213E 100%);
    color: white;
    padding: 1.5rem;
    border-radius: 0.8rem;
    margin-bottom: 1.5rem;
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
}
.page-header h1 { margin: 0; color: white; font-size: 1.5rem; font-weight: 600; }
.page-header p { margin: 0.5rem 0 0 0; color: #B0BEC5; font-size: 0.95rem; }

/* 信息卡片 */
.info-card {
    background: white;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    padding: 1rem;
    margin: 0.5rem 0;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05);
}
.info-card:hover {
    border-color: #90CAF9;
    box-shadow: 0 4px 8px rgba(33, 150, 243, 0.1);
}

/* 风险等级横幅 */
.risk-banner {
    padding: 0.8rem 1rem;
    border-radius: 4px;
    margin: 0.5rem 0;
    font-weight: 500;
}
.risk-low { background: #E8F5E9; border-left: 4px solid #43A047; color: #2E7D32; }
.risk-mid { background: #FFF3E0; border-left: 4px solid #FB8C00; color: #E65100; }
.risk-high { background: #FFEBEE; border-left: 4px solid #E53935; color: #C62828; }

/* 状态指示器 */
.status-indicator {
    display: inline-flex;
    align-items: center;
    padding: 0.25rem 0.75rem;
    border-radius: 12px;
    font-size: 0.85rem;
    font-weight: 500;
}
.status-active { background: #E8F5E9; color: #2E7D32; }
.status-inactive { background: #F5F5F5; color: #757575; }
.status-warning { background: #FFF3E0; color: #E65100; }
.status-error { background: #FFEBEE; color: #C62828; }

/* 隐藏 Gradio 框架标识（底部 Runs/API/Gradio logo） */
footer { visibility: hidden !important; }

/* 隐藏 Plotly 交互工具栏（看板为只读展示，避免 Zoom/Pan 等按钮干扰） */
.js-plotly-plot .modebar,
.modebar-container,
.modebar { display: none !important; }

/* 表格样式优化 */
.gradio-dataframe table {
    border-collapse: collapse;
    width: 100%;
}
.gradio-dataframe th {
    background: #f5f5f5;
    font-weight: 600;
    padding: 0.75rem;
    border-bottom: 2px solid #e0e0e0;
}
.gradio-dataframe td {
    padding: 0.75rem;
    border-bottom: 1px solid #f0f0f0;
}
.gradio-dataframe tr:hover {
    background: #f8f9fa;
}

/* 按钮样式优化 */
.gradio-button {
    border-radius: 6px;
    font-weight: 500;
    transition: all 0.2s ease;
}
.gradio-button:hover {
    transform: translateY(-1px);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
}

/* Accordion 样式 */
.gradio-accordion {
    border-radius: 8px;
    overflow: hidden;
}
"""


def metric_card(label: str, value: str, color: str = "blue") -> str:
    """创建指标卡片 HTML"""
    return f"""
    <div class="metric-card metric-{color}">
        <h3>{label}</h3>
        <p>{value}</p>
    </div>
    """


def page_header(title: str, subtitle: str = "") -> str:
    """创建页面头部 HTML"""
    return f"""
    <div class="page-header">
    <h1>{title}</h1>
    <p>{subtitle}</p>
    </div>
    """


def info_card(content: str) -> str:
    """创建信息卡片 HTML"""
    return f'<div class="info-card">{content}</div>'


def risk_banner(level: str, detail: str) -> str:
    """创建风险等级横幅 HTML
    
    Args:
        level: 风险等级 ('low', 'mid', 'high')
        detail: 风险描述
    """
    labels = {"low": "🟢 低", "mid": "🟡 中", "high": "🔴 高"}
    label = labels.get(level, level)
    return f'<div class="risk-banner risk-{level}"><b>{label} 风险</b> — {detail}</div>'


def status_badge(status: str) -> str:
    """创建状态徽章 HTML
    
    Args:
        status: 状态 ('active', 'inactive', 'warning', 'error')
    """
    labels = {
        "active": "✅ 运行中",
        "inactive": "⏸️ 已停止",
        "warning": "⚠️ 警告",
        "error": "❌ 错误"
    }
    label = labels.get(status, status)
    return f'<span class="status-indicator status-{status}">{label}</span>'


DEMO_BANNER = """
<div style="background:#FFF3E0;border:2px solid #FF9800;border-radius:8px;
padding:10px 16px;margin:8px 0;color:#E65100;font-weight:600;">
⚠️ 演示数据：本页当前展示的是占位/随机数据，非真实回测或因子计算结果。
请勿据此页面内容做任何交易决策。真实结果见 reports/ 目录输出。
</div>
"""


def soft_theme() -> gr.themes.Soft:
    """统一主题"""
    return gr.themes.Soft(
        primary_hue="blue",
        secondary_hue="gray",
    )

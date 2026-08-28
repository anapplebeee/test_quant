"""前端主题和共享组件"""
from __future__ import annotations

import gradio as gr

CUSTOM_CSS = """
/* 主容器 */
.gradio-container {
    max-width: 1400px !important;
}

/* 指标卡片 */
.metric-card {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    padding: 1.2rem;
    border-radius: 0.8rem;
    color: white;
    text-align: center;
    margin: 0.5rem 0;
}
.metric-card h3 { margin: 0; font-size: 0.9rem; opacity: 0.9; }
.metric-card p { margin: 0.5rem 0 0 0; font-size: 1.8rem; font-weight: bold; }
.metric-blue { background: linear-gradient(135deg, #1E88E5 0%, #1565C0 100%); }
.metric-green { background: linear-gradient(135deg, #43A047 0%, #2E7D32 100%); }
.metric-red { background: linear-gradient(135deg, #E53935 0%, #C62828 100%); }
.metric-purple { background: linear-gradient(135deg, #8E24AA 0%, #6A1B9A 100%); }
.metric-teal { background: linear-gradient(135deg, #00897B 0%, #00695C 100%); }
.metric-orange { background: linear-gradient(135deg, #FB8C00 0%, #EF6C00 100%); }

/* 页面头 */
.page-header {
    background: linear-gradient(135deg, #1A1A2E 0%, #16213E 100%);
    color: white;
    padding: 1.5rem;
    border-radius: 0.8rem;
    margin-bottom: 1.5rem;
}
.page-header h1 { margin: 0; color: white; }
.page-header p { margin: 0.5rem 0 0 0; color: #B0BEC5; }

/* 隐藏 Gradio 框架标识（底部 Runs/API/Gradio logo） */
footer { visibility: hidden !important; }

/* 隐藏 Plotly 交互工具栏（看板为只读展示，避免 Zoom/Pan 等按钮干扰） */
.js-plotly-plot .modebar,
.modebar-container,
.modebar { display: none !important; }
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

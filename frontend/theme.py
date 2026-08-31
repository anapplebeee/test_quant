"""前端主题和共享组件"""
from __future__ import annotations

import gradio as gr

CUSTOM_CSS = """
/* 主容器 */
.gradio-container {
    max-width: 1400px !important;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: linear-gradient(180deg, #f5f7fa 0%, #e4e8ec 100%);
}

/* 指标卡片 */
.metric-card {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    padding: 1.2rem;
    border-radius: 12px;
    color: white;
    text-align: center;
    margin: 0.5rem 0;
    box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
    transition: transform 0.3s ease, box-shadow 0.3s ease;
}
.metric-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 8px 25px rgba(0, 0, 0, 0.2);
}
/* 新增：指标卡容器从 flex 改为等宽 grid（消除参差），响应式 2-4 列 */
.metric-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
    margin: 0.5rem 0 1rem 0;
}
/* 旧 flex 保留用于兼容，但新代码使用 .metric-grid 以获得等宽对齐 */
.metric-flex { display: flex; gap: 12px; flex-wrap: wrap; margin: 0.5rem 0 1rem 0; }
.metric-card h3 { margin: 0; font-size: 0.85rem; opacity: 0.9; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }
.metric-card p { margin: 0.5rem 0 0 0; font-size: 2rem; font-weight: bold; }
.metric-blue { background: linear-gradient(135deg, #1E88E5 0%, #1565C0 100%); }
.metric-green { background: linear-gradient(135deg, #43A047 0%, #2E7D32 100%); }
.metric-red { background: linear-gradient(135deg, #E53935 0%, #C62828 100%); }
.metric-purple { background: linear-gradient(135deg, #8E24AA 0%, #6A1B9A 100%); }
.metric-teal { background: linear-gradient(135deg, #00897B 0%, #00695C 100%); }
.metric-orange { background: linear-gradient(135deg, #FB8C00 0%, #EF6C00 100%); }
.metric-gray { background: linear-gradient(135deg, #78909C 0%, #546E7A 100%); }

/* 页面头 */
.page-header {
    background: linear-gradient(135deg, #1A1A2E 0%, #16213E 50%, #0F3460 100%);
    color: white;
    padding: 2rem;
    border-radius: 16px;
    margin-bottom: 1.5rem;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.15);
    position: relative;
    overflow: hidden;
}
.page-header::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: radial-gradient(circle at 30% 50%, rgba(255,255,255,0.1) 0%, transparent 50%);
}
.page-header h1 { margin: 0; color: white; font-size: 1.8rem; font-weight: 700; position: relative; }
.page-header p { margin: 0.5rem 0 0 0; color: #B0BEC5; font-size: 1rem; position: relative; }

/* 信息卡片 */
.info-card {
    background: white;
    border: 1px solid #E0E0E0;
    border-radius: 12px;
    padding: 1.2rem;
    margin: 0.5rem 0;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
    transition: all 0.3s ease;
}
.info-card:hover {
    border-color: #90CAF9;
    box-shadow: 0 4px 16px rgba(33, 150, 243, 0.15);
    transform: translateY(-2px);
}

/* 风险等级横幅 */
.risk-banner {
    padding: 1rem 1.2rem;
    border-radius: 8px;
    margin: 0.5rem 0;
    font-weight: 500;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}
.risk-low { background: linear-gradient(90deg, #E8F5E9 0%, #C8E6C9 100%); border-left: 4px solid #43A047; color: #2E7D32; }
.risk-mid { background: linear-gradient(90deg, #FFF3E0 0%, #FFE0B2 100%); border-left: 4px solid #FB8C00; color: #E65100; }
.risk-high { background: linear-gradient(90deg, #FFEBEE 0%, #FFCDD2 100%); border-left: 4px solid #E53935; color: #C62828; }

/* 状态指示器 */
.status-indicator {
    display: inline-flex;
    align-items: center;
    padding: 0.3rem 0.8rem;
    border-radius: 16px;
    font-size: 0.85rem;
    font-weight: 500;
    gap: 0.3rem;
}
.status-active { background: linear-gradient(90deg, #E8F5E9, #C8E6C9); color: #2E7D32; }
.status-inactive { background: linear-gradient(90deg, #F5F5F5, #E0E0E0); color: #616161; }
.status-warning { background: linear-gradient(90deg, #FFF3E0, #FFE0B2); color: #E65100; }
.status-error { background: linear-gradient(90deg, #FFEBEE, #FFCDD2); color: #C62828; }

/* 步骤指示器 */
.step-indicator {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    background: linear-gradient(90deg, #E3F2FD 0%, #BBDEFB 100%);
    border-radius: 8px;
    margin-bottom: 1rem;
    font-weight: 600;
    color: #1565C0;
}

/* 隐藏 Gradio 框架标识（底部 Runs/API/Gradio logo） */
footer { visibility: hidden !important; }

/* 隐藏 Plotly 交互工具栏（看板为只读展示，避免 Zoom/Pan 等按钮干扰） */
.js-plotly-plot .modebar,
.modebar-container,
.modebar { display: none !important; }

/* 表格样式优化 */
.gradio-dataframe {
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
}
.gradio-dataframe table {
    border-collapse: collapse;
    width: 100%;
}
.gradio-dataframe th {
    background: linear-gradient(180deg, #f8f9fa 0%, #e9ecef 100%);
    font-weight: 600;
    padding: 0.8rem 1rem;
    border-bottom: 2px solid #dee2e6;
    text-align: left;
}
.gradio-dataframe td {
    padding: 0.75rem 1rem;
    border-bottom: 1px solid #f0f0f0;
}
.gradio-dataframe tr:hover {
    background: linear-gradient(90deg, #f8f9fa 0%, #e3f2fd 100%);
}

/* 按钮样式优化 */
.gradio-button {
    border-radius: 8px;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
}
.gradio-button:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
}
.gradio-button:active {
    transform: translateY(0);
}

/* Accordion 样式 */
.gradio-accordion {
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
    margin-bottom: 1rem;
}

/* Tab 样式 */
.gradio-tabs {
    border-radius: 12px;
    overflow: hidden;
}

/* 下拉框样式 */
.gradio-dropdown {
    border-radius: 8px;
}

/* 输入框样式 */
.gradio-textbox, .gradio-number {
    border-radius: 8px;
}

/* 分隔线 */
hr {
    border: none;
    height: 1px;
    background: linear-gradient(90deg, transparent 0%, #dee2e6 50%, transparent 100%);
    margin: 1.5rem 0;
}

/* 滚动条样式 */
::-webkit-scrollbar {
    width: 8px;
    height: 8px;
}
::-webkit-scrollbar-track {
    background: #f1f1f1;
    border-radius: 4px;
}
::-webkit-scrollbar-thumb {
    background: #c1c1c1;
    border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
    background: #a1a1a1;
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


def step_header(step_num: int, title: str) -> str:
    """创建步骤标题 HTML"""
    return f"""
    <div class="step-indicator">
        <span style="font-size: 1.2rem;">{step_num}️⃣</span>
        <span>{title}</span>
    </div>
    """


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
<div style="background: linear-gradient(90deg, #FFF3E0, #FFE0B2); border: 2px solid #FF9800;
border-radius: 12px; padding: 1rem 1.5rem; margin: 1rem 0; color: #E65100; font-weight: 600;
display: flex; align-items: center; gap: 0.5rem;">
    <span style="font-size: 1.5rem;">⚠️</span>
    <div>
        <div style="font-weight: 700; margin-bottom: 0.25rem;">演示数据</div>
        <div style="font-size: 0.9rem; font-weight: 400;">本页当前展示的是占位/随机数据，非真实回测或因子计算结果。请勿据此页面内容做任何交易决策。</div>
    </div>
</div>
"""


def soft_theme() -> gr.themes.Soft:
    """统一主题"""
    return gr.themes.Soft(
        primary_hue="blue",
        secondary_hue="gray",
    )

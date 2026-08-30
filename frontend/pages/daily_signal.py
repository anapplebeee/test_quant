"""每日信号页面"""
from __future__ import annotations

import gradio as gr

from common import reports_dir
from frontend.theme import page_header


def _load_signal(date: str) -> str:
    """加载信号报告（路径走 common.reports_dir()，避免配置根目录漂移）"""
    from common import safe_path, valid_date8

    if not valid_date8(date):
        return "非法日期格式"
    path = safe_path(reports_dir(), f"signal_{date}.md")
    if path is not None and path.exists():
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "未找到信号报告"


def _snapshot():
    """扫描信号文件，返回 (日期选项, 最新日期, 最新内容)"""
    signal_files = sorted([
        f.replace("signal_", "").replace(".md", "")
        for f in reports_dir().glob("signal_*.md")
    ])
    if not signal_files:
        return [], None, "暂无信号报告，运行 scripts/daily_signal.py 生成"
    latest = signal_files[-1]
    return signal_files, latest, _load_signal(latest)


def render():
    """渲染每日信号 Tab"""
    with gr.Tab("📋 每日信号"):
        gr.HTML(page_header("📋 每日信号", "持仓建议 / 调仓信号 / ML预测分数"))

        gr.Markdown("> ⚠️ 信号仅供研究参考，不构成投资建议")

        # 动态快照 + 定时刷新（修复：新信号生成后页面停留在启动时的旧列表）
        choices, latest, content = _snapshot()
        signal_date = gr.Dropdown(label="选择日期", choices=choices, value=latest)
        signal_content = gr.Markdown(value=content)
        signal_date.change(_load_signal, inputs=signal_date, outputs=signal_content)

        def _refresh():
            c, v, txt = _snapshot()
            return gr.update(choices=c, value=v), txt

        refresh_btn = gr.Button("🔄 刷新信号列表", size="sm")
        refresh_btn.click(_refresh, outputs=[signal_date, signal_content])
        gr.Timer(30).tick(_refresh, outputs=[signal_date, signal_content])

"""Quart 量化研究平台 - Gradio 主入口（精简版）

架构：
- frontend/theme.py       主题和共享组件
- frontend/cache.py       TTL 缓存
- frontend/pages/         各页面模块
- api/                    数据/任务/回测 API 层
"""
from __future__ import annotations

import gradio as gr

from frontend.theme import CUSTOM_CSS, soft_theme
from frontend.pages import (
    attribution,
    backtest,
    backtest_diagnostics,
    daily_signal,
    data_overview,
    factor_ecology,
    factor_research,
    glossary,
    home,
    risk_management,
    strategy_monitor,
)


def create_app() -> gr.Blocks:
    """创建 Gradio 应用"""
    # 按顺序注册各页面
    page_modules = [
        home,
        data_overview,
        factor_research,
        backtest,
        daily_signal,
        strategy_monitor,
        attribution,
        risk_management,
        factor_ecology,
        backtest_diagnostics,
        glossary,
    ]

    with gr.Blocks(title="Quart 量化研究平台") as app:
        for module in page_modules:
            module.render()

    return app


if __name__ == "__main__":
    app = create_app()
    app.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
        css=CUSTOM_CSS,
        theme=soft_theme(),
    )

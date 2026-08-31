"""回测诊断页面：Walk-Forward 样本外检验（真实数据优先）。

2026-08-31 架构检视：原页面硬编码"演示 WFA 数据"且与回测中心的真实
`render_wfa_panel()` 并存，容易误导。现统一读取 ArtifactStore 的最新 WFA
制品，按 run_id 追溯参数、数据版本与逐折结果。
"""
from __future__ import annotations

import gradio as gr
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from api.artifacts_api import latest_wfa, read_table
from frontend.theme import page_header


def _latest_wfa() -> tuple[pd.DataFrame | None, dict | None]:
    """读取最新 WFA manifest 与 folds 制品。"""
    metadata = latest_wfa()
    if metadata is None:
        return None, None
    folds = read_table(metadata["run_id"], "folds")
    return folds, metadata


def _decay_ratio(row: pd.Series) -> float | None:
    is_s = float(row.get("is_sharpe") or 0.0)
    # IS 夏普非正时，OOS/IS 的负/负比值会伪装成“稳健”；此时衰减比无定义。
    if is_s <= 1e-9:
        return None
    return float(row.get("oos_sharpe") or 0.0) / is_s


def render():
    """渲染回测诊断 Tab"""
    wfa, metadata = _latest_wfa()

    with gr.Tab("🔍 回测诊断"):
        gr.HTML(page_header("🔍 回测诊断", "Walk-Forward / 过拟合检验 / 参数稳健性"))

        if wfa is None or wfa.empty:
            gr.Markdown(
                "### 🚶 Walk-Forward 检验\n"
                "> ⚠️ 暂无 WFA 结果。请在 **回测中心 → 运行 Walk-Forward 验证** 生成，"
                "或用命令行：\n\n"
                "```powershell\n"
                ".venv/Scripts/python.exe scripts/walk_forward.py --strategy lowvol_indz "
                "--grid top_k=30,50 --grid rebalance_days=45,60\n"
                "```"
            )
            return

        gr.Markdown(
            f"### 🚶 Walk-Forward 检验\n"
            f"> ✅ **运行制品**：`{metadata['run_id']}` · 数据指纹 `{metadata['fingerprint']}`"
            "（样本外/样本内夏普比，越接近 1 越稳健）"
        )

        table = pd.DataFrame(
            {
                "折": wfa["fold"],
                "训练段": wfa["train"],
                "验证段": wfa["test"],
                "最优top_k": wfa.get("best_top_k", wfa.get("best_top_k", "-")),
                "最优调仓(日)": wfa.get("best_rebalance_days", "-"),
                "IS夏普": wfa["is_sharpe"].round(3),
                "OOS夏普": wfa["oos_sharpe"].round(3),
                "OOS年化": (wfa["oos_cagr"] * 100).round(2).astype(str) + "%",
                "OOS回撤": (wfa["oos_mdd"] * 100).round(1).astype(str) + "%",
                "成交笔数": wfa.get("n_trades", "-"),
            }
        )
        table["衰减比"] = wfa.apply(_decay_ratio, axis=1).round(2)
        gr.Dataframe(value=table, interactive=False)

        # 逐折 OOS 夏普柱状图（红 = 负值，符合 A 股红涨绿跌习惯）
        oos = wfa["oos_sharpe"].astype(float)
        fig = go.Figure(go.Bar(
            x=wfa["fold"].astype(str), y=oos,
            marker_color=["#E53935" if v >= 0 else "#43A047" for v in oos],
            text=oos.round(3), textposition="outside",
        ))
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        fig.update_layout(title="逐折样本外夏普（OOS）", height=320,
                          margin=dict(l=0, r=0, t=40, b=0),
                          template="plotly_white")
        gr.Plot(value=fig)

        # 过拟合诊断
        ratios = wfa.apply(_decay_ratio, axis=1).dropna()
        if not ratios.empty:
            gr.Markdown("### 📊 过拟合诊断")
            gr.Markdown(
                f"- **平均衰减比 (OOS/IS 夏普)**：**{ratios.mean():.2f}**"
                f"（≥0.8 稳健；0.4~0.8 存在过拟合；<0.4 基本在挑噪声）\n"
                f"- **正样本外折数**：{(oos > 0).sum()} / {len(oos)}"
            )
            fig2 = px.histogram(ratios, nbins=10, labels={"value": "衰减比"},
                                title="衰减比分布")
            fig2.update_layout(height=280, margin=dict(l=0, r=0, t=40, b=0),
                                template="plotly_white")
            gr.Plot(value=fig2)
        else:
            gr.Markdown(
                "### 📊 过拟合诊断\n"
                "> 衰减比无法计算：样本内夏普均值非正或没有有效折；"
                "请直接查看逐折 OOS 夏普和累计净值。"
            )

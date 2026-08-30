"""因子生态页面：IC 时序 / 拥挤度 / 失效预警（真实因子数据优先）。

2026-08-31 架构检视：原页面为硬编码假 IC/拥挤度数据且未标注，容易误导。
改为读取 `reports/factor_research_ext*.csv` 真实 RankIC 结果；
无真实数据时明确提示运行因子研究，拥挤度/预警等尚无数据源的模块
明确标注"规划中"，不再展示假数字。
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import gradio as gr

from common import reports_dir
from frontend.theme import page_header


def _load_real_factors() -> pd.DataFrame | None:
    """复用因子研究的真实 RankIC 结果（与 factor_research 页同源）。"""
    frames = []
    for path in sorted(reports_dir().glob("factor_research_ext*.csv")):
        try:
            df = pd.read_csv(path)
            first_col = df.columns[0]
            df = df.rename(columns={
                first_col: "因子", "ic": "IC", "icir": "ICIR",
                "pos%": "正率%", "ls_bp": "多空bp",
            })
            keep = [c for c in ["因子", "IC", "ICIR", "正率%", "多空bp"] if c in df.columns]
            if "因子" in keep and len(keep) >= 3:
                frames.append(df[keep])
        except Exception:
            continue
    if not frames:
        return None
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset="因子", keep="last")
    return merged.sort_values("ICIR", key=lambda s: s.abs(), ascending=False)


def render():
    """渲染因子生态 Tab"""
    factors = _load_real_factors()

    with gr.Tab("🌿 因子生态"):
        gr.HTML(page_header("🌿 因子生态监控", "IC 强弱 / 拥挤度 / 失效预警"))

        if factors is None:
            gr.Markdown(
                "### 📊 因子 RankIC（真实数据）\n"
                "> ⚠️ 暂无因子研究结果。请先运行：\n\n"
                "```powershell\n"
                ".venv/Scripts/python.exe scripts/factor_research.py\n"
                "```\n\n"
                "因子生态的**拥挤度 / 失效预警**依赖截面离散度与滚动 IC 时序，"
                "属于规划中模块（README Roadmap 待开发项），暂不展示占位数据。"
            )
            return

        gr.Markdown(
            "> ✅ **真实数据**：`reports/factor_research_ext*.csv` 全市场 RankIC"
            "（月度截面，fwd5d）。拥挤度/失效预警为规划中模块。"
        )

        gr.Markdown("### 📊 因子 IC 强度（按 |ICIR| 排序）")
        show = factors.copy()
        show["IC"] = show["IC"].round(4)
        show["ICIR"] = show["ICIR"].round(3)
        gr.Dataframe(value=show, interactive=False)

        # IC 横截面图（红 = 负 IC，绿 = 正 IC，符合 A 股习惯）
        fig = go.Figure(go.Bar(
            x=factors["因子"], y=factors["IC"],
            marker_color=["#E53935" if v < 0 else "#43A047" for v in factors["IC"]],
            text=factors["IC"].round(3), textposition="outside",
        ))
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        fig.update_layout(title="因子 RankIC（fwd5d，全市场）", height=380,
                          xaxis_tickangle=-45, margin=dict(l=0, r=0, t=40, b=0),
                          template="plotly_white")
        gr.Plot(value=fig)

        gr.Markdown("### 📝 读法说明")
        gr.Markdown("""
        - **IC 为负**（红）：因子值与未来收益负相关，选股时取负向排序（如低波类）
        - **IC 为正**（绿）：因子值与未来收益正相关，取正向排序
        - **|ICIR| ≥ 0.5**：因子时序稳定；**|IC| ≥ 0.03**：达到最低有效标准
        - 实证结论（README 2026-08 终审）：动量族负 IC 且成本后不可挽救；
          低波族 IC 显著为正但超额集中在空头端，纯多头需配合低频/缓冲带变现
        """)

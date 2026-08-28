"""因子研究页面"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import gradio as gr

from frontend.theme import DEMO_BANNER, page_header


FACTOR_DEFS = pd.DataFrame({
    "因子名": ["mom60", "mom120", "sharpe_mom60", "rev5", "high_lag250",
              "vol20_neg", "downvol_ratio_neg", "amp20_neg", "amp_expand20",
              "net_flow20", "vwap_dev20", "pv_corr20_neg", "trend_eff_dir",
              "lottery20_neg", "gap_avg"],
    "类别": ["动量", "动量", "动量(风险调整)", "短期反转", "52周高点距离",
            "波动率", "下行波动", "振幅", "振幅异动",
            "量价确认", "量价确认", "量价确认", "趋势效率",
            "彩票效应", "隔夜跳空"],
    "逻辑": ["60日收益率", "120日收益率", "60日收益/波动",
            "5日反转(负收益)", "距52周高点距离",
            "20日波动率(负向)", "下行波动占比(负向)",
            "20日平均振幅(负向)", "20日/120日均额比",
            "20日净流入占比", "20日VWAP偏离",
            "20日量价相关(负向)", "60日趋势效率",
            "20日最大涨幅(负向)", "20日平均跳空"],
})

# 因子研究结果：优先读真实研究输出（reports/factor_research*.csv），无则回退演示数据

def _load_real_results() -> pd.DataFrame | None:
    """合并 factor_research_ext.csv / ext2.csv 的真实 RankIC 结果"""
    import glob
    frames = []
    for path in sorted(glob.glob("reports/factor_research_ext*.csv")):
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
    # 同名因子取最新（ext2 覆盖 ext）
    merged = merged.drop_duplicates(subset="因子", keep="last")
    return merged.sort_values("ICIR", key=lambda s: s.abs(), ascending=False)


REAL_RESULTS = _load_real_results()

# 演示数据（仅在无真实输出时展示）
FACTOR_RESULTS = pd.DataFrame({
    "因子": ["vol20_neg", "amp20_neg", "lottery20_neg", "rev5", "mom60",
             "sharpe_mom60", "pv_corr20_neg", "net_flow20", "downvol_ratio_neg",
             "high_lag250", "trend_eff_dir", "vwap_dev20", "gap_avg",
             "amp_expand20", "mom120"],
    "IC": [-0.068, -0.065, -0.064, 0.042, 0.031, 0.028, -0.025, 0.022,
           -0.020, 0.018, 0.015, -0.012, 0.010, 0.008, 0.005],
    "ICIR": [-2.8, -2.6, -2.5, 1.8, 1.5, 1.3, -1.1, 1.0,
             -0.9, 0.8, 0.7, -0.5, 0.4, 0.3, 0.2],
    "正率%": [72, 70, 69, 62, 58, 56, 45, 55, 44, 54, 52, 46, 51, 50, 49],
    "多空bp": [85, 78, 75, 42, 35, 30, -22, 25, -18, 20, 15, -12, 10, 8, 5],
}).sort_values("ICIR", key=abs, ascending=False)


def render():
    """渲染因子研究 Tab"""
    with gr.Tab("🔬 因子研究"):
        gr.HTML(page_header("🔬 因子研究", "因子IC/ICIR分析 / 选股能力评估"))
        if REAL_RESULTS is not None:
            gr.Markdown("> ✅ **真实数据**：以下为本项目因子研究脚本的全市场 RankIC 输出"
                        "（reports/factor_research_ext*.csv，2020-02~2026-07 月度截面，fwd5d）")
        else:
            gr.HTML(DEMO_BANNER)

        with gr.Accordion("📖 当前因子列表（15个价量因子）", open=False):
            gr.Dataframe(value=FACTOR_DEFS, interactive=False)

        gr.Markdown("### 📊 因子 ICIR")
        results = REAL_RESULTS if REAL_RESULTS is not None else FACTOR_RESULTS
        colors = ["#e74c3c" if x < 0 else "#2ecc71" for x in results["ICIR"]]
        fig = go.Figure(go.Bar(
            x=results["因子"], y=results["ICIR"],
            marker_color=colors, text=results["ICIR"].round(2),
            textposition="outside",
        ))
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        fig.add_hline(y=0.5, line_dash="dot", line_color="green", annotation_text="有效阈值")
        fig.update_layout(title="因子 ICIR（按绝对值排序）", height=400,
                          margin=dict(l=0, r=0, t=40, b=0))
        gr.Plot(value=fig)

        gr.Markdown("### 详细指标")
        gr.Dataframe(value=results, interactive=False)

        gr.Markdown("### 💡 因子方向说明")
        gr.Markdown("""
        - **负向因子**（如 vol20_neg）：因子值越大越好 → 实际选股时取负值排序
        - **正向因子**（如 mom60）：因子值越大越好
        - **ICIR > 0.5** 表示因子稳定有效；**|IC| > 0.03** 为最低有效标准
        """)

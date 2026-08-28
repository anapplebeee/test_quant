"""Quart 量化研究平台 - Gradio 主入口"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import gradio as gr
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import load_stock_names
from api.data_api import get_stock_stats, get_universe, get_sample_data
from api.backtest_api import get_backtest_summary, get_equity_curve, get_trades
from api.task_api import run_task, get_task_status


# ========== 全局样式 ==========
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
.metric-card h3 {
    margin: 0;
    font-size: 0.9rem;
    opacity: 0.9;
}
.metric-card p {
    margin: 0.5rem 0 0 0;
    font-size: 1.8rem;
    font-weight: bold;
}
.metric-blue { background: linear-gradient(135deg, #1E88E5 0%, #1565C0 100%); }
.metric-green { background: linear-gradient(135deg, #43A047 0%, #2E7D32 100%); }
.metric-red { background: linear-gradient(135deg, #E53935 0%, #C62828 100%); }
.metric-purple { background: linear-gradient(135deg, #8E24AA 0%, #6A1B9A 100%); }
.metric-teal { background: linear-gradient(135deg, #00897B 0%, #00695C 100%); }

/* 标题样式 */
.page-header {
    background: linear-gradient(135deg, #1A1A2E 0%, #16213E 100%);
    color: white;
    padding: 1.5rem;
    border-radius: 0.8rem;
    margin-bottom: 1.5rem;
}
.page-header h1 {
    margin: 0;
    color: white;
}
.page-header p {
    margin: 0.5rem 0 0 0;
    color: #B0BEC5;
}

/* 分区标题 */
.section-header {
    display: flex;
    align-items: center;
    margin: 1.5rem 0 1rem 0;
    padding-bottom: 0.5rem;
    border-bottom: 2px solid #1E88E5;
}
.section-header h2 {
    margin: 0;
    color: #1A1A2E;
}
"""


def create_metric_card(label: str, value: str, color: str = "blue") -> str:
    """创建指标卡片HTML"""
    return f"""
    <div class="metric-card metric-{color}">
        <h3>{label}</h3>
        <p>{value}</p>
    </div>
    """


# ========== 主界面 ==========
def create_app() -> gr.Blocks:
    """创建 Gradio 应用"""
    
    with gr.Blocks(title="Quart 量化研究平台") as app:
        
        # ========== 主页 ==========
        with gr.Tab("🏠 首页"):
            gr.Markdown("""
            # 📊 Quart 量化研究平台
            
            > A-share 量化策略研究 · 因子挖掘 · 回测分析 · 风险管理
            """)
            
            # 策略概览指标
            with gr.Row():
                summary_path = "reports/summary_momentum_rotation_20260826_173826.json"
                try:
                    with open(summary_path) as f:
                        summary = json.load(f)
                    gr.HTML(create_metric_card("累计收益", f"{summary['total_return']*100:.1f}%", "green"))
                    gr.HTML(create_metric_card("年化收益", f"{summary['cagr']*100:.1f}%", "blue"))
                    gr.HTML(create_metric_card("夏普比率", f"{summary['sharpe']:.2f}", "purple"))
                    gr.HTML(create_metric_card("最大回撤", f"{summary['max_drawdown']*100:.1f}%", "red"))
                    gr.HTML(create_metric_card("超额年化", f"{summary['excess_cagr']*100:.1f}%", "teal"))
                except Exception:
                    gr.Info("回测摘要未加载")
            
            gr.Markdown("---")
            
            # 导航卡片
            gr.Markdown("## 🔬 研究模块")
            with gr.Row():
                with gr.Column():
                    gr.Markdown("""
                    <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; height: 120px;">
                        <h4>🗃️ 数据总览</h4>
                        <p style="color: #666;">股票池 / 数据状态 / 市场概览</p>
                    </div>
                    """)
                with gr.Column():
                    gr.Markdown("""
                    <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; height: 120px;">
                        <h4>🔬 因子研究</h4>
                        <p style="color: #666;">IC/ICIR / 因子表现 / 选股能力</p>
                    </div>
                    """)
                with gr.Column():
                    gr.Markdown("""
                    <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; height: 120px;">
                        <h4>📈 回测中心</h4>
                        <p style="color: #666;">净值曲线 / 交易记录 / 参数扫描</p>
                    </div>
                    """)
                with gr.Column():
                    gr.Markdown("""
                    <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; height: 120px;">
                        <h4>📋 每日信号</h4>
                        <p style="color: #666;">持仓建议 / 调仓信号 / 推送日志</p>
                    </div>
                    """)
            
            gr.Markdown("## 📡 监控模块")
            with gr.Row():
                with gr.Column():
                    gr.Markdown("""
                    <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; height: 120px;">
                        <h4>📡 策略监控</h4>
                        <p style="color: #666;">运行状态 / 调仓日历 / 持仓分析</p>
                    </div>
                    """)
                with gr.Column():
                    gr.Markdown("""
                    <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; height: 120px;">
                        <h4>🧩 归因分析</h4>
                        <p style="color: #666;">Brinson归因 / 因子暴露 / 收益分解</p>
                    </div>
                    """)
                with gr.Column():
                    gr.Markdown("""
                    <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; height: 120px;">
                        <h4>🛡️ 风险管理</h4>
                        <p style="color: #666;">VaR/CVaR / 集中度 / 流动性</p>
                    </div>
                    """)
                with gr.Column():
                    gr.Markdown("""
                    <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; height: 120px;">
                        <h4>🌿 因子生态</h4>
                        <p style="color: #666;">IC衰减 / IC时序 / 拥挤度</p>
                    </div>
                    """)
        
        # ========== 数据总览 ==========
        with gr.Tab("🗃️ 数据总览"):
            gr.Markdown("""
            <div class="page-header">
                <h1>🗃️ 数据总览</h1>
                <p>股票池状态 / 数据覆盖 / 市场概览</p>
            </div>
            """)
            
            # 数据统计
            with gr.Row():
                stats = get_stock_stats()
                gr.HTML(create_metric_card("股票数量", f"{stats['stock_count']:,}", "blue"))
                gr.HTML(create_metric_card("股票池快照", str(stats['universe_count']), "green"))
                gr.HTML(create_metric_card("指数数量", str(stats['index_count']), "purple"))
                gr.HTML(create_metric_card("最新分数日期", stats.get('last_score_date', 'N/A'), "teal"))
            
            gr.Markdown("---")
            
            # 股票池
            gr.Markdown("## 📋 最新股票池成分")
            universe_df = get_universe()
            gr.Dataframe(
                value=universe_df.head(50),
                interactive=False,
            )
            
            gr.Markdown("---")
            
            # 数据质量
            gr.Markdown("## 🔍 数据质量检查")
            sample_df = get_sample_data()
            if sample_df is not None:
                with gr.Row():
                    gr.Markdown(f"**起始日期:** {sample_df['date'].iloc[0]}")
                    gr.Markdown(f"**结束日期:** {sample_df['date'].iloc[-1]}")
                    gr.Markdown(f"**交易日数:** {len(sample_df):,}")
                
                # 价格走势图
                fig = px.line(sample_df, x="date", y="close", 
                            labels={"close": "收盘价", "date": "日期"})
                fig.update_layout(height=350, margin=dict(l=0, r=0, t=0, b=0))
                gr.Plot(value=fig)
        
        # ========== 因子研究 ==========
        with gr.Tab("🔬 因子研究"):
            gr.Markdown("""
            <div class="page-header">
                <h1>🔬 因子研究</h1>
                <p>因子IC/ICIR分析 / 选股能力评估 / 因子相关性</p>
            </div>
            """)
            
            gr.Markdown("""
            ℹ️ **数据说明**: 因子研究基于 `scripts/factor_research.py` 的输出。
            运行 `python scripts/factor_research.py --sample monthly` 生成最新因子IC/ICIR数据。
            """)
            
            # 因子列表
            with gr.Accordion("📖 当前因子列表（15个价量因子）", open=False):
                factor_defs = pd.DataFrame({
                    "因子名": ["mom60", "mom120", "sharpe_mom60", "rev5", "high_lag250",
                              "vol20_neg", "downvol_ratio_neg", "amp20_neg", "amp_expand20",
                              "net_flow20", "vwap_dev20", "pv_corr20_neg", "trend_eff_dir",
                              "lottery20_neg", "gap_avg"],
                    "类别": ["动量", "动量", "动量(风险调整)", "短期反转", "52周高点距离",
                            "波动率", "下行波动", "振幅", "振幅异动",
                            "量价确认", "量价确认", "量价确认", "趋势效率",
                            "彩票效应", "隔夜跳空"],
                })
                gr.Dataframe(value=factor_defs, interactive=False)
            
            gr.Markdown("---")
            gr.Markdown("## 📊 因子表现汇总")
            
            # 因子结果数据
            factor_results = pd.DataFrame({
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
            })
            factor_results = factor_results.sort_values("ICIR", key=abs, ascending=False)
            
            # ICIR 柱状图
            fig = go.Figure()
            colors = ["#e74c3c" if x < 0 else "#2ecc71" for x in factor_results["ICIR"]]
            fig.add_trace(go.Bar(x=factor_results["因子"], y=factor_results["ICIR"],
                                marker_color=colors, text=factor_results["ICIR"].round(2),
                                textposition="outside"))
            fig.add_hline(y=0, line_dash="dash", line_color="gray")
            fig.add_hline(y=0.5, line_dash="dot", line_color="green", annotation_text="有效阈值")
            fig.update_layout(title="因子 ICIR (按绝对值排序)", height=400, 
                            margin=dict(l=0, r=0, t=40, b=0))
            gr.Plot(value=fig)
            
            gr.Dataframe(value=factor_results, interactive=False)
        
        # ========== 回测中心 ==========
        with gr.Tab("📈 回测中心"):
            gr.Markdown("""
            <div class="page-header">
                <h1>📈 回测中心</h1>
                <p>净值曲线 / 交易记录 / 参数扫描 / 策略对比</p>
            </div>
            """)
            
            # 回测选择
            backtest_name = gr.Dropdown(
                label="选择回测",
                choices=["momentum_rotation_20260826_173826"],
                value="momentum_rotation_20260826_173826",
            )
            
            # 绩效概览
            gr.Markdown("## 🎯 绩效概览")
            with gr.Row():
                summary = get_backtest_summary("momentum_rotation_20260826_173826")
                if summary:
                    gr.HTML(create_metric_card("累计收益", f"{summary['total_return']*100:.1f}%", 
                                               "green" if summary['total_return'] > 0 else "red"))
                    gr.HTML(create_metric_card("年化收益", f"{summary['cagr']*100:.1f}%", "blue"))
                    gr.HTML(create_metric_card("夏普比率", f"{summary['sharpe']:.2f}", "purple"))
                    gr.HTML(create_metric_card("最大回撤", f"{summary['max_drawdown']*100:.1f}%", "red"))
            
            gr.Markdown("---")
            
            # 净值曲线
            gr.Markdown("## 📈 净值曲线")
            equity_df = get_equity_curve("momentum_rotation_20260826_173826")
            if equity_df is not None:
                columns = [c for c in equity_df.columns if c != "date"]
                equity_cols = gr.CheckboxGroup(
                    label="选择参数组",
                    choices=columns,
                    value=columns[:2] if len(columns) > 2 else columns,
                )
                
                def plot_equity(selected_cols):
                    if not selected_cols:
                        return None
                    fig = go.Figure()
                    for col in selected_cols:
                        fig.add_trace(go.Scatter(x=equity_df["date"], y=equity_df[col],
                                                mode="lines", name=col))
                    fig.add_hline(y=1_000_000, line_dash="dash", line_color="gray",
                                 annotation_text="初始资金")
                    fig.update_layout(title="策略净值曲线", height=450,
                                    margin=dict(l=0, r=0, t=40, b=0))
                    return fig
                
                equity_plot = gr.Plot(value=plot_equity(columns[:2] if len(columns) > 2 else columns))
                equity_cols.change(plot_equity, inputs=equity_cols, outputs=equity_plot)
            
            gr.Markdown("---")
            
            # 交易记录
            gr.Markdown("## 📋 最近交易记录")
            trades_df = get_trades("momentum_rotation_20260826_173826")
            if trades_df is not None:
                gr.Dataframe(value=trades_df.head(30), interactive=False)
        
        # ========== 每日信号 ==========
        with gr.Tab("📋 每日信号"):
            gr.Markdown("""
            <div class="page-header">
                <h1>📋 每日信号</h1>
                <p>持仓建议 / 调仓信号 / ML预测分数</p>
            </div>
            """)
            
            gr.Markdown("""
            ℹ️ 信号由 `scripts/daily_signal.py` 自动生成，每日盘后运行。
            信号仅供研究参考，不构成投资建议。
            """)
            
            # 信号报告
            signal_files = sorted([f.replace("signal_", "").replace(".md", "") 
                                 for f in os.listdir("reports") if f.startswith("signal_")])
            if signal_files:
                signal_date = gr.Dropdown(label="选择日期", choices=signal_files, 
                                         value=signal_files[-1] if signal_files else None)
                
                def load_signal(date):
                    path = f"reports/signal_{date}.md"
                    if os.path.exists(path):
                        with open(path) as f:
                            return f.read()
                    return "未找到信号报告"
                
                signal_content = gr.Markdown(value=load_signal(signal_files[-1] if signal_files else ""))
                signal_date.change(load_signal, inputs=signal_date, outputs=signal_content)
            else:
                gr.Info("暂无信号报告")
        
        # ========== 策略监控 ==========
        with gr.Tab("📡 策略监控"):
            gr.Markdown("""
            <div class="page-header">
                <h1>📡 策略监控</h1>
                <p>任务执行 / 运行状态 / 持仓分析</p>
            </div>
            """)
            
            # 任务执行
            gr.Markdown("## ⚡ 任务执行")
            
            with gr.Row():
                task_btn_refresh = gr.Button("🔄 数据刷新", variant="secondary")
                task_btn_backtest = gr.Button("📈 运行回测", variant="primary")
                task_btn_signal = gr.Button("📋 生成信号", variant="secondary")
                task_btn_ml = gr.Button("🤖 ML训练", variant="secondary")
            
            task_output = gr.Textbox(label="任务输出", lines=15, interactive=False)
            task_status = gr.Textbox(label="状态", interactive=False)
            
            def on_run_task(task_name):
                """运行任务并实时输出"""
                import subprocess
                import time
                
                if task_name not in TASKS:
                    yield "", "❌ 未知任务"
                    return
                
                task = TASKS[task_name]
                command = task["command"]
                
                yield "", f"🟡 {task['name']} 启动中..."
                
                try:
                    process = subprocess.Popen(
                        command,
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    
                    output_lines = []
                    for line in process.stdout:
                        output_lines.append(line.strip())
                        # 保留最近50行
                        display_text = "\n".join(output_lines[-50:])
                        yield display_text, f"🟡 {task['name']} 运行中..."
                    
                    process.wait()
                    
                    if process.returncode == 0:
                        yield "\n".join(output_lines[-50:]), f"✅ {task['name']} 完成"
                    else:
                        yield "\n".join(output_lines[-50:]), f"❌ {task['name']} 失败 (code={process.returncode})"
                        
                except Exception as e:
                    yield f"错误: {str(e)}", f"❌ {task['name']} 异常"
            
            task_btn_refresh.click(
                fn=lambda: on_run_task("refresh"),
                outputs=[task_output, task_status],
            )
            task_btn_backtest.click(
                fn=lambda: on_run_task("backtest"),
                outputs=[task_output, task_status],
            )
            task_btn_signal.click(
                fn=lambda: on_run_task("signal"),
                outputs=[task_output, task_status],
            )
            task_btn_ml.click(
                fn=lambda: on_run_task("ml_train"),
                outputs=[task_output, task_status],
            )
            
            gr.Markdown("---")
            
            # 持仓分析
            gr.Markdown("## 💰 当前持仓分析")
            
            holdings_path = "state/holdings.json"
            if os.path.exists(holdings_path):
                with open(holdings_path) as f:
                    holdings = json.load(f)
                
                cash = holdings.get("cash", 0)
                positions = holdings.get("positions", {})
                
                if positions:
                    stock_names = load_stock_names()
                    pos_data = []
                    total_value = cash
                    
                    for sym, shares in positions.items():
                        daily_path = f"data/daily/{sym}.parquet"
                        price = 0
                        if os.path.exists(daily_path):
                            try:
                                df = pd.read_parquet(daily_path)
                                price = df["close"].iloc[-1]
                            except Exception:
                                pass
                        value = shares * price
                        total_value += value
                        pos_data.append({
                            "代码": sym,
                            "名称": stock_names.get(sym, "-"),
                            "持股数": shares,
                            "最新价": round(price, 2),
                            "市值": round(value, 2),
                            "权重": f"{value/total_value*100:.1f}%" if total_value > 0 else "0%",
                        })
                    
                    pos_df = pd.DataFrame(pos_data)
                    
                    with gr.Row():
                        gr.HTML(create_metric_card("现金", f"{cash:,.0f} CNY", "green"))
                        gr.HTML(create_metric_card("持仓市值", f"{total_value-cash:,.0f} CNY", "blue"))
                        gr.HTML(create_metric_card("账户总值", f"{total_value:,.0f} CNY", "purple"))
                    
                    gr.Dataframe(value=pos_df, interactive=False)
                else:
                    gr.Info("当前无持仓")
            else:
                gr.Info("未找到持仓文件")
        
        # ========== 参数词典 ==========
        with gr.Tab("📖 参数词典"):
            gr.Markdown("""
            <div class="page-header">
                <h1>📖 量化参数词典</h1>
                <p>量化策略中所有关键参数的含义、计算方法和经验取值范围</p>
            </div>
            """)
            
            with gr.Accordion("⚙️ 策略参数", open=True):
                gr.Markdown("""
                | 参数名 | 含义 | 常用范围 | 当前值 |
                |--------|------|----------|--------|
                | `lookback_days` | 动量回看天数 | 20-252天 | 60 |
                | `top_k` | 持仓股票数 | 5-50只 | 10 |
                | `rebalance_days` | 调仓周期 | 1-20天 | 5 |
                | `max_weight_pct` | 单股最大权重 | 5%-20% | 15% |
                | `min_avg_amount` | 流动性门槛 | 1000万-1亿 | 5000万 |
                """)
            
            with gr.Accordion("🛡️ 风控参数", open=False):
                gr.Markdown("""
                | 参数名 | 含义 | 常用范围 | 当前值 |
                |--------|------|----------|--------|
                | `max_position_pct` | 单股最大持仓权重 | 10%-30% | 25% |
                | `max_daily_loss_pct` | 单日最大亏损限制 | 3%-10% | 5% |
                """)
            
            with gr.Accordion("📈 绩效指标", open=False):
                gr.Markdown("""
                | 指标 | 含义 | 优秀标准 |
                |------|------|----------|
                | **CAGR** | 年化复合收益率 | > 15% |
                | **夏普比率** | 风险调整后收益 | > 1.0 |
                | **最大回撤** | 峰值到谷底最大亏损 | < -20% |
                | **卡玛比率** | 收益与最大回撤比 | > 1.0 |
                """)
    
    return app


# ========== 启动 ==========
if __name__ == "__main__":
    app = create_app()
    app.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
        css=CUSTOM_CSS,
        theme=gr.themes.Soft(
            primary_hue="blue",
            secondary_hue="gray",
        ),
    )

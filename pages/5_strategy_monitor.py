"""策略监控 - 运行状态/调仓日历/实时P&L"""
from __future__ import annotations

import os
import glob
import json
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import load_stock_names

st.set_page_config(page_title="策略监控 - Quart", page_icon="📡", layout="wide")

st.title("📡 策略监控")

# ========== 运行状态 ==========
st.subheader("任务运行状态")

logs_dir = "logs"
log_files = {
    "数据刷新": "refresh.log",
    "全量更新": "fullmarket.log",
    "ML训练": "train_ml.log",
    "数据回填": "backfill.log",
}

col1, col2, col3, col4 = st.columns(4)
cols = [col1, col2, col3, col4]

for idx, (name, logfile) in enumerate(log_files.items()):
    log_path = os.path.join(logs_dir, logfile)
    err_path = os.path.join(logs_dir, logfile.replace(".log", ".err"))
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        last_line = lines[-1].strip() if lines else "无日志"
        # 检查是否有错误
        has_error = os.path.exists(err_path) and os.path.getsize(err_path) > 0
        status = "🔴 异常" if has_error else "🟢 正常"
        cols[idx].metric(name, status, last_line[-30:] if len(last_line) > 30 else last_line)
    except FileNotFoundError:
        cols[idx].metric(name, "⚪ 未运行", "-")

st.divider()

# ========== 调仓日历 ==========
st.subheader("调仓日历")

try:
    # 读取策略配置中的调仓周期
    cfg_path = "config/settings.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        import yaml
        cfg = yaml.safe_load(f)

    rebalance_days = cfg.get("strategy", {}).get("rebalance_days", 5)

    # 计算下次调仓日（基于最近一个交易日 + rebalance_days）
    # 简化：假设今天是交易日，下次调仓 = 今天 + rebalance_days 个工作日
    today = datetime.now().date()
    next_rebalance = today
    days_added = 0
    while days_added < rebalance_days:
        next_rebalance += timedelta(days=1)
        if next_rebalance.weekday() < 5:  # 跳过周末
            days_added += 1

    days_until = (next_rebalance - today).days

    c1, c2, c3 = st.columns(3)
    c1.metric("调仓周期", f"{rebalance_days} 个交易日")
    c2.metric("下次调仓日", next_rebalance.strftime("%Y-%m-%d"))
    c3.metric("距调仓日", f"{days_until} 天")

except Exception as e:
    st.error(f"加载配置失败: {e}")

st.divider()

# ========== 持仓分析 ==========
st.subheader("当前持仓分析")

holdings_path = "state/holdings.json"
stock_names = load_stock_names()

try:
    with open(holdings_path, encoding="utf-8") as f:
        holdings = json.load(f)

    cash = holdings.get("cash", 0)
    positions = holdings.get("positions", {})

    if positions:
        # 获取最新价格
        # 简化：从回测equity文件获取最近日期
        equity_files = sorted(glob.glob("reports/sweep_equity_*.csv"))
        if equity_files:
            eq = pd.read_csv(equity_files[-1])
            last_date = eq["date"].iloc[-1]
        else:
            last_date = "N/A"

        # 持仓明细
        pos_data = []
        total_value = cash

        # 尝试从日数据获取最新收盘价
        for sym, shares in positions.items():
            price = 0
            daily_path = f"data/daily/{sym}.parquet"
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
        pos_df = pos_df.sort_values("市值", ascending=False)

        # 汇总
        c1, c2, c3 = st.columns(3)
        c1.metric("现金", f"{cash:,.0f} CNY")
        c2.metric("持仓市值", f"{total_value - cash:,.0f} CNY")
        c3.metric("账户总值", f"{total_value:,.0f} CNY")

        st.dataframe(
            pos_df.style.format({"最新价": "{:.2f}", "市值": "{:,.0f}"}),
            use_container_width=True,
            hide_index=True,
        )

        # 持仓权重饼图
        if len(pos_df) > 0:
            fig = go.Figure(data=[go.Pie(
                labels=pos_df["名称"].tolist() + ["现金"],
                values=pos_df["市值"].tolist() + [cash],
                hole=0.4,
            )])
            fig.update_layout(title="持仓分布", height=350, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("当前无持仓")

except FileNotFoundError:
    st.info("未找到持仓文件，请先运行每日信号生成")
except Exception as e:
    st.error(f"加载持仓失败: {e}")

st.divider()

# ========== 风控提示 ==========
st.subheader("风控规则说明")

st.markdown(f"""
| 参数 | 当前值 | 含义 |
|------|--------|------|
| `max_position_pct` | {cfg.get('risk', {}).get('max_position_pct', 0.25)} | 单只股票最大持仓权重 |
| `max_daily_loss_pct` | {cfg.get('risk', {}).get('max_daily_loss_pct', 0.05)} | 单日最大亏损比例（触发止损） |
| `min_avg_amount` | {cfg.get('strategy', {}).get('min_avg_amount', 50000000):,.0f} | 最低日均成交额过滤（流动性门槛） |
| `max_weight_pct` | {cfg.get('strategy', {}).get('max_weight_pct', 0.15)} | 策略单股最大目标权重 |
""")

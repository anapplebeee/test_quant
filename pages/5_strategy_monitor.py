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
from task_runner import TASKS, run_task

st.set_page_config(page_title="策略监控 - Quart", page_icon="📡", layout="wide")

st.title("📡 策略监控")

# ========== 任务执行面板 ==========
st.subheader("任务执行")

# 初始化 session state 用于日志输出
if "task_log" not in st.session_state:
    st.session_state.task_log = []
if "task_running" not in st.session_state:
    st.session_state.task_running = False
if "task_result" not in st.session_state:
    st.session_state.task_result = None


def _on_output(line: str):
    """回调：收到输出"""
    st.session_state.task_log.append(line)


def _on_complete(returncode: int):
    """回调：任务完成"""
    st.session_state.task_running = False
    st.session_state.task_result = returncode


# 任务按钮网格
task_ids = list(TASKS.keys())
# 每行4个按钮
for i in range(0, len(task_ids), 4):
    batch = task_ids[i : i + 4]
    cols_list = st.columns(len(batch))
    for j, tid in enumerate(batch):
        t = TASKS[tid]
        with cols_list[j]:
            if st.button(
                f"{t['icon']} {t['name']}",
                key=f"btn_{tid}",
                disabled=st.session_state.task_running,
                use_container_width=True,
            ):
                # 清空日志并开始执行
                st.session_state.task_log = []
                st.session_state.task_running = True
                st.session_state.task_result = None
                st.session_state["_current_task"] = tid
                run_task(
                    tid,
                    on_output=_on_output,
                    on_complete=_on_complete,
                )
                st.rerun()

# 显示执行日志
if st.session_state.task_log or st.session_state.task_running:
    task_name = TASKS.get(st.session_state.get("_current_task", ""), {}).get("name", "任务")
    status_text = "🟡 运行中..." if st.session_state.task_running else (
        f"✅ 完成 (code={st.session_state.task_result})" if st.session_state.task_result == 0
        else f"❌ 失败 (code={st.session_state.task_result})" if st.session_state.task_result is not None
        else ""
    )
    st.markdown(f"**{task_name} 输出** {status_text}")

    # 日志输出区域
    log_text = "\n".join(st.session_state.task_log[-100:])  # 最多显示最后100行
    st.code(log_text, language=None, line_numbers=False)

    # 运行时自动刷新
    if st.session_state.task_running:
        st.rerun()

st.divider()

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


def _check_task_status(log_path: str, err_path: str) -> tuple[str, str]:
    """检查任务状态，返回 (状态, 详情)"""
    import re

    # 检查 log 文件
    if not os.path.exists(log_path):
        if os.path.exists(err_path):
            return "⚪ 仅错误日志", "无标准日志"
        return "⚪ 未运行", "无日志文件"

    with open(log_path, encoding="utf-8") as f:
        log_lines = f.readlines()

    if not log_lines:
        return "⚪ 空日志", "日志为空"

    last_line = log_lines[-1].strip()

    # 检查 .err 文件是否有真正的 ERROR 级别日志
    has_real_error = False
    error_count = 0
    if os.path.exists(err_path) and os.path.getsize(err_path) > 0:
        with open(err_path, encoding="utf-8", errors="ignore") as f:
            err_content = f.read()
        # 统计真正的 ERROR 行（排除进度条、INFO等）
        for line in err_content.split("\n"):
            if re.search(r"\b(ERROR|Exception|Traceback|FAILED)\b", line, re.IGNORECASE):
                has_real_error = True
                error_count += 1

    # 检查 log 最后一行是否包含成功标志
    success_keywords = ["完成", "成功", "done", "success", "finished", "saved", "exported"]
    has_success = any(kw in last_line.lower() for kw in success_keywords)

    if has_real_error:
        return "⚠️ 有错误", f"{error_count}个错误 | {last_line[-25:]}"
    elif has_success:
        return "🟢 完成", last_line[-30:]
    elif len(log_lines) > 0:
        return "🟢 运行过", last_line[-30:]
    else:
        return "⚪ 未知", "-"


for idx, (name, logfile) in enumerate(log_files.items()):
    log_path = os.path.join(logs_dir, logfile)
    err_path = os.path.join(logs_dir, logfile.replace(".log", ".err"))
    status, detail = _check_task_status(log_path, err_path)
    cols[idx].metric(name, status, detail)

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

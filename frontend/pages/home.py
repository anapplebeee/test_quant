"""首页页面：交易日工作台 + 回测结果查看。

工作台风格：
- 顶部：当前日期 / 交易日状态 / 数据新鲜度 / 下一交易日倒计时
- 中部：账户概览快捷区 + 最新回测指标
- 底部：功能模块导航
"""
from __future__ import annotations

from datetime import date

import gradio as gr
import pandas as pd

import data_bus
from api.backtest_api import get_backtest_summary, get_window_stats, scan_summaries
from api.data_api import get_freshness, get_next_trade_date
from api.manual_trading_api import get_account_summary
from api.research_api import latest_sweep_headlines
from api.strategy_api import strategy_catalog
from frontend.theme import info_card, metric_card, page_header, section_header, status_badge


def _fmt_pct(v, digits: int = 1) -> str:
    """统一百分比格式：+12.3% / -4.5%；None/NaN → '-'"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    return f"{v*100:+.{digits}f}%"


def _fmt_num(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    return f"{v:.{digits}f}"


def _color_by_sign(v) -> str:
    """A 股配色：红涨绿跌（与沪深市场习惯一致）。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "gray"
    return "red" if v > 0 else ("green" if v < 0 else "gray")


def _today_status_card() -> str:
    """生成顶部工作台状态卡：日期、交易日状态、数据新鲜度、账户。"""
    today = date.today()
    weekday_cn = ["周二", "周三", "周四", "周五", "周六", "周日", "周一"]
    wd_label = weekday_cn[today.weekday()]
    date_str = today.strftime("%Y-%m-%d")

    # 是否为交易日（工作日即视为交易日，精确判断需交易日库）
    is_weekday = today.weekday() < 5

    # 数据新鲜度（UI-001 DR-03：统一走 data_api）
    stale_text = "-"
    try:
        days = get_freshness()
        if days is None:
            stale_text = "无数据"
            stale_color = "red"
        elif days <= 1:
            stale_text = f"最新 ({days}天)"
            stale_color = "green"
        elif days <= 5:
            stale_text = f"滞后 {days} 天"
            stale_color = "orange"
        else:
            stale_text = f"过期 {days} 天"
            stale_color = "red"
    except Exception:
        stale_text = "无法检测"
        stale_color = "gray"

    # 下一交易日
    next_td_text = "-"
    countdown_text = "-"
    nxt = get_next_trade_date(today)
    if nxt:
        try:
            nxt_date = date.fromisoformat(nxt)
            next_td_text = nxt
            countdown_text = str((nxt_date - today).days) + " 天"
        except ValueError:
            pass

    # 账户快照（UI-001 DR-03：单一 API 调用，消除二次查询）
    cash_text = "-"
    total_text = "-"
    account = get_account_summary(today.isoformat())
    if account.get("cash") is not None:
        cash_text = f"{account['cash']:,.0f}"
    if account.get("total") is not None:
        total_text = f"{account['total']:,.0f}"

    trade_badge = status_badge("active") if is_weekday else status_badge("inactive")

    return f"""
    <div style="background: linear-gradient(135deg, #ffffff 0%, #f0f4ff 100%);
                border-radius: 16px; padding: 1.5rem; margin-bottom: 1.5rem;
                color: #1a2332; box-shadow: 0 4px 20px rgba(0,0,0,0.06); border: 1px solid #e2e8f0;">
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem;">
            <div style="text-align: center;">
                <div style="font-size: 0.95rem; color: #64748b; font-weight: 500;">今天</div>
                <div style="font-size: 1.5rem; font-weight: 700; letter-spacing: 0.5px; color: #1e293b;">{date_str} {wd_label}</div>
                <div style="margin-top: 0.3rem;">{trade_badge}</div>
            </div>
            <div style="text-align: center;">
                <div style="font-size: 0.95rem; color: #64748b; font-weight: 500;">数据新鲜度</div>
                <div style="font-size: 1.4rem; font-weight: 700; color: {'#15803d' if stale_color=='green' else '#c2410c' if stale_color=='orange' else '#dc2626' if stale_color=='red' else '#64748b'};">{stale_text}</div>
            </div>
            <div style="text-align: center;">
                <div style="font-size: 0.95rem; color: #64748b; font-weight: 500;">下一交易日</div>
                <div style="font-size: 1.4rem; font-weight: 700; letter-spacing: 0.5px; color: #1e293b;">{next_td_text}</div>
                <div style="font-size: 0.9rem; color: #94a3b8;">{countdown_text}</div>
            </div>
            <div style="text-align: center;">
                <div style="font-size: 0.95rem; color: #64748b; font-weight: 500;">账户现金 (CNY)</div>
                <div style="font-size: 1.4rem; font-weight: 700; letter-spacing: 0.5px; color: #1e293b;">{cash_text}</div>
            </div>
            <div style="text-align: center;">
                <div style="font-size: 0.95rem; color: #64748b; font-weight: 500;">总资产 (CNY)</div>
                <div style="font-size: 1.4rem; font-weight: 700; letter-spacing: 0.5px; color: #1e293b;">{total_text}</div>
            </div>
        </div>
    </div>
    """


def _summary_html(name: str | None) -> str:
    """所选回测结果的完整摘要 HTML（统一卡片口径，供下拉切换刷新）。"""
    if not name:
        return info_card("*暂无回测结果，请先在回测中心运行回测。*")
    s = get_backtest_summary(name) or {}
    ws = get_window_stats(name) or {}
    strategy = name.rsplit("_", 1)[0] if "_" in name else name
    cat = {r["name"]: r for r in strategy_catalog()}
    meta = cat.get(strategy)
    desc = f"{meta['label']}：{meta['desc']}" if meta else f"未知策略 `{strategy}`（未在 REGISTRY 注册）"

    total = s.get("total_return")
    bench_total = s.get("bench_total_return")
    excess = s.get("bench_excess_cagr")
    w1y = ws.get("last_1y") or {}
    w6m = ws.get("last_6m") or {}

    # 基本信息卡片
    info_html = f"""
    <div class="info-card" style="margin-bottom: 1rem;">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
            <div>
                <div style="font-size: 0.85rem; color: #666; margin-bottom: 0.25rem;">所选结果</div>
                <div style="font-size: 1.1rem; font-weight: 600; color: #333;">`{name}`</div>
            </div>
            <div>
                <div style="font-size: 0.85rem; color: #666; margin-bottom: 0.25rem;">策略</div>
                <div style="font-size: 1.1rem; font-weight: 600; color: #1565C0;">{desc}</div>
            </div>
            <div>
                <div style="font-size: 0.85rem; color: #666; margin-bottom: 0.25rem;">回测区间</div>
                <div style="font-size: 1rem; color: #333;">{s.get('start', '-')} ~ {s.get('end', '-')}</div>
            </div>
            <div>
                <div style="font-size: 0.85rem; color: #666; margin-bottom: 0.25rem;">基准</div>
                <div style="font-size: 1rem; color: #333;">沪深300</div>
            </div>
        </div>
    </div>
    """

    rows = [
        (
            "📌 全周期（区间口径）",
            [
                metric_card("累计收益", _fmt_pct(total), _color_by_sign(total)),
                metric_card("基准同期", _fmt_pct(bench_total), _color_by_sign(bench_total)),
                metric_card("超额年化", _fmt_pct(excess), _color_by_sign(excess)),
                metric_card("最大回撤", _fmt_pct(s.get("max_drawdown")), "red"),
            ],
        ),
        (
            "📏 近 1 年（252 交易日，与沪深300 同期可比）",
            [
                metric_card("近1年收益", _fmt_pct(w1y.get("return")), _color_by_sign(w1y.get("return"))),
                metric_card("近1年回撤", _fmt_pct(w1y.get("mdd")), "red"),
                metric_card("近1年基准收益", _fmt_pct(w1y.get("bench_return")), _color_by_sign(w1y.get("bench_return"))),
                metric_card("近1年基准回撤", _fmt_pct(w1y.get("bench_mdd")), "red"),
            ],
        ),
        (
            "📏 近半年（126 交易日）",
            [
                metric_card("近半年收益", _fmt_pct(w6m.get("return")), _color_by_sign(w6m.get("return"))),
                metric_card("近半年回撤", _fmt_pct(w6m.get("mdd")), "red"),
                metric_card("近半年基准收益", _fmt_pct(w6m.get("bench_return")), _color_by_sign(w6m.get("bench_return"))),
                metric_card("近半年基准回撤", _fmt_pct(w6m.get("bench_mdd")), "red"),
            ],
        ),
        (
            "📌 风险指标（全周期）",
            [
                metric_card("年化收益", _fmt_pct(s.get("cagr")), _color_by_sign(s.get("cagr"))),
                metric_card("夏普比率", _fmt_num(s.get("sharpe")), "purple"),
                metric_card("年化波动", _fmt_pct(s.get("annual_vol")), "gray"),
                metric_card("日胜率(持仓)", _fmt_pct(s.get("invested_win_rate", s.get("daily_win_rate"))), "teal"),
            ],
        ),
    ]

    parts = [info_html]
    for title, cards in rows:
        parts.append(f'<div style="margin: 1rem 0 0.5rem 0; font-weight: 600; color: #333;">{title}</div>')
        parts.append('<div class=\"metric-grid\">' + "".join(cards) + "</div>")
    return "\n".join(parts)


def _sweep_table() -> pd.DataFrame:
    """最新验证结果表（格式化后的 DataFrame；无数据时返回空表）"""
    heads = latest_sweep_headlines()
    if heads is None or heads.empty:
        return pd.DataFrame()
    show = heads.copy()
    for c in ("CAGR", "最大回撤"):
        if c in show.columns:
            show[c] = show[c].map(_fmt_pct)
    if "夏普" in show.columns:
        show["夏普"] = show["夏普"].map(lambda v: _fmt_num(v))
    if "换手x" in show.columns:
        show["换手x"] = show["换手x"].map(lambda v: "-" if pd.isna(v) else f"{v:.1f}x")
    return show


def render():
    """渲染首页 Tab（交易日工作台）"""
    with gr.Tab("🏠 首页"):
        gr.HTML(page_header(
            "交易与研究工作台",
            "先确认交易日、数据新鲜度和账户状态，再进入研究、回测、信号与执行流程。",
            "DAILY WORKSPACE",
        ))
        # ===== 工作台状态区 =====
        status_html = gr.HTML(value=_today_status_card())

        # ===== 回测结果查看：搜索 + 筛选 + 表格选择（与回测中心同源）=====
        full_df = scan_summaries()
        if full_df.empty:
            gr.Info("暂无回测结果")
            return

        default_name = full_df["name"].iloc[0]
        home_df_state = gr.State(full_df)
        home_filtered_state = gr.State(full_df)

        # ---- 辅助函数（必须在组件前定义，否则 Gradio build 阶段找不到）----
        def _home_fmt(df: pd.DataFrame) -> pd.DataFrame:
            visible = ["运行", "日期", "区间", "CAGR", "夏普", "最大回撤", "波动", "卡玛"]
            if df is None or df.empty:
                return pd.DataFrame(columns=visible)
            df = df.copy()
            for col in ("CAGR", "最大回撤", "波动"):
                if col in df:
                    df[col] = df[col].apply(lambda v: f"{v*100:.2f}%" if pd.notna(v) else "")
            for col in ("夏普", "卡玛"):
                if col in df:
                    df[col] = df[col].apply(lambda v: f"{v:.2f}" if pd.notna(v) else "")
            keep = ["label", "run_date", "区间", "CAGR", "夏普", "最大回撤", "波动", "卡玛"]
            return df[[c for c in keep if c in df.columns]].rename(
                columns={"label": "运行", "run_date": "日期"}
            )

        def _home_filter(kw: str, st: str, full: pd.DataFrame):
            f = full.copy()
            if st and st != "全部":
                f = f[f["strategy"] == st]
            if kw:
                kw = kw.lower()
                mask = (
                    f["label"].str.lower().str.contains(kw, na=False)
                    | f["name"].str.lower().str.contains(kw, na=False)
                    | f["区间"].str.lower().str.contains(kw, na=False)
                )
                f = f[mask]
            f = f.reset_index(drop=True)
            return _home_fmt(f), f

        def _home_select(evt: gr.SelectData, fdf: pd.DataFrame):
            if evt.index is None:
                return gr.update()
            idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
            try:
                name = str(fdf["name"].iloc[idx])
            except Exception:
                return "❌ 未找到匹配的回测"
            return _summary_html(name)

        summary_html = gr.HTML(value=_summary_html(default_name))

        with gr.Accordion("📌 回测结果查看（搜索/筛选后点击行查看详情，默认最新）", open=True):
            with gr.Row():
                home_search = gr.Textbox(
                    label="搜索（标签 / 策略 / 区间）",
                    placeholder="输入关键字过滤…留空显示全部", scale=3,
                )
                home_strat = gr.Dropdown(
                    label="策略筛选",
                    choices=["全部", *sorted(full_df["strategy"].unique().tolist())],
                    value="全部",
                )

            home_table = gr.Dataframe(
                value=_home_fmt(full_df),
                interactive=False, max_height=300, wrap=True,
                datatype=["str"] * len(_home_fmt(full_df).columns),
                label="运行列表 · 点击行查看详情",
                show_search="filter", pinned_columns=1, buttons=["fullscreen", "copy"],
            )

            home_search.change(_home_filter, [home_search, home_strat, home_df_state], [home_table, home_filtered_state])
            home_strat.change(_home_filter, [home_search, home_strat, home_df_state], [home_table, home_filtered_state])
            home_table.select(_home_select, [home_filtered_state], [summary_html])

        gr.Markdown("---")

        # 最新验证结果：每个策略最新一次参数扫描的最优行
        with gr.Accordion("🔬 最新验证结果（来自各策略最新参数扫描，按 CAGR 排序）", open=False):
            sweep_table = gr.Dataframe(value=_sweep_table(), interactive=False)

        with gr.Accordion("📚 策略库（由后端 REGISTRY 驱动，与回测中心/策略监控同源）", open=False):
            md_rows = ["| 策略 | 名称 | 状态 | 默认换手/持仓 | 说明 |", "|------|------|------|------|------|"]
            for r in strategy_catalog():
                md_rows.append(
                    f"| `{r['name']}` | {r['label']} | {r['status']} | "
                    f"{r['default_rebalance']}日 / Top{r['default_top_k']} | {r['desc']} |"
                )
            gr.Markdown("\n".join(md_rows))

        # ===== 跨页联动：任务完成 → 自动刷新本页数据 =====
        seen_state = gr.State(data_bus.current())

        def _poll_data_version(seen_val: int, kw: str, st: str):
            """跨页联动：数据/回测变化时刷新表格、筛选器选项、状态卡片、扫描结果。"""
            changed, cur = data_bus.poll(seen_val)
            if not changed:
                return (gr.skip(),) * 5 + (cur,)
            new_full = scan_summaries()
            if new_full.empty:
                empty_fmt = _home_fmt(new_full)
                return (
                    _today_status_card(), empty_fmt, new_full,
                    gr.update(choices=["全部"], value="全部"),
                    _sweep_table(), cur,
                )
            new_choices = ["全部", *sorted(new_full["strategy"].unique().tolist())]
            fmt_df, raw_df = _home_filter(kw, st, new_full)
            return (
                _today_status_card(),
                fmt_df, raw_df,
                gr.update(choices=new_choices, value=st if st in new_choices else "全部"),
                _sweep_table(),
                cur,
            )

        gr.Timer(5).tick(
            _poll_data_version,
            inputs=[seen_state, home_search, home_strat],
            outputs=[status_html, home_table, home_filtered_state, home_strat,
                     sweep_table, seen_state],
        )

        # ===== 功能区快捷导航 =====
        # 用 [role=tab] 匹配标签页按钮并点击（避免硬编码下标和断链的转义）
        def _nav(label: str, emoji: str, desc: str, tab_label: str) -> str:
            js = (
                "Array.from(document.querySelectorAll('[role=tab]'))"
                ".find(function(t){return t.textContent.includes('" + tab_label + "')})"
                "?.click();"
            )
            return (
                f'<div class="quick-nav-card" '
                f'onclick="{js}">'
                f'<div class="quick-nav-icon">{emoji}</div>'
                f'<div class="quick-nav-label">{label}</div>'
                f'<div class="quick-nav-detail">{desc}</div></div>'
            )

        nav_entries = [
            ("每日信号", "📋", "查看 T+1 交易计划", "每日信号"),
            ("操作中心", "🧰", "执行回测/信号/刷新", "操作中心"),
            ("手动交易", "💼", "账户/委托/对账", "手动交易"),
            ("策略监控", "📡", "任务队列/持仓", "策略监控"),
            ("因子研究", "🔬", "IC/ICIR 分析", "因子研究"),
            ("风险管理", "🛡️", "VaR/CVaR/集中度", "风险管理"),
        ]
        cards = "".join(_nav(*e) for e in nav_entries)
        gr.HTML(section_header("功能区快捷入口", "按日常工作流进入对应模块。", "NAVIGATION"))
        gr.HTML(f'<div class="quick-nav-grid">{cards}</div>')

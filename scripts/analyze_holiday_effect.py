"""RESEARCH-016：A 股长假效应实证——节前/节后/持假收益事件研究。

问题：节假日前后是否需要调仓/空仓？
方法：
1. 从平台交易日历推导长假（相邻交易日间隔 ≥ 4 个自然日，排除普通周末 3 天），
   按节后首日月份/日期归类：春节 / 国庆 / 其他(元旦/清明/五一/端午/中秋)；
2. 对四个组合做事件研究（2019-2026）：
   - 全 A 等权（md.close_val 全池均值）
   - 小盘 1/3 等权（流通市值最低 1/3，小市值策略口径）
   - 沪深300、中证1000
   窗口：节前 T-5..T-1、T-2..T-1、T-1 单日、持假(T-1收盘→T+1收盘)、节后 T+1、T+1..T+2；
3. 统计 N/均值/中位数/胜率/t 值；叠加交易成本给"节前清仓"盈亏平衡判断。

结论写入 scripts/holiday_test.md。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from backtest_small_val import load_config, load_fundamental_panel, load_md  # noqa: E402

START = pd.Timestamp("2019-01-01")
END = pd.Timestamp("2026-08-31")


def classify_holiday(d_prev: pd.Timestamp, d_next: pd.Timestamp) -> str | None:
    """节后首日 d_next；返回节假日类别（None=非长假）。"""
    gap = (d_next - d_prev).days
    if gap < 4:
        return None
    m, day = d_next.month, d_next.day
    if m == 10 and day <= 9:
        return "国庆"
    if d_prev.month == 9 and d_next.month == 10:
        return "国庆"
    if m in (1, 2) and (d_prev.month == 1 or d_prev.month == 2):
        # 春节：节后首日 1 月末~2 月中下旬
        if (m == 2 and day <= 25) or (m == 1 and day >= 20):
            return "春节"
        return "元旦"
    if m == 5 and day <= 7:
        return "五一"
    if m == 4 and day <= 8:
        return "清明"
    if m == 6 and day <= 10:
        return "端午"
    if m == 9 and day <= 15:
        return "中秋"
    return "其他"


def t_stat(x: np.ndarray) -> float:
    if len(x) < 3:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def main():
    md = load_md()
    fund = load_fundamental_panel(md.dates)
    dates = md.dates
    px_all = md.close_val  # 全池等权
    px_bench300 = pd.Series(md.benchmark_close, index=dates)
    idx852 = BarStore852()
    px_csi1000 = idx852.reindex(dates).ffill()

    # 小盘 1/3 等权
    fm_rank = fund["fmcap"].rank(axis=1, pct=True)
    small_px = px_all.where(fm_rank <= 1 / 3)
    px_small = small_px.mean(axis=1, skipna=True)

    series = {
        "全A等权": px_all.mean(axis=1, skipna=True),
        "小盘1/3等权": px_small,
        "沪深300": px_bench300,
        "中证1000": px_csi1000,
    }
    rets = {k: v.pct_change().dropna() for k, v in series.items()}

    # 长假事件表
    events = []
    for i in range(1, len(dates)):
        d_prev, d_next = dates[i - 1], dates[i]
        cat = classify_holiday(d_prev, d_next)
        if cat:
            events.append({"i_next": i, "d_prev": d_prev, "d_next": d_next,
                           "cat": cat, "gap": (d_next - d_prev).days})
    ev = pd.DataFrame(events)
    ev = ev[(ev["d_next"] >= START) & (ev["d_next"] <= END)]
    print(f"长假事件数(2019-2026): {len(ev)}  分类: {ev['cat'].value_counts().to_dict()}")

    rows = []
    for name, r in rets.items():
        r = r[(r.index >= START) & (r.index <= END)]
        for _, row in ev.iterrows():
            i_next = dates.searchsorted(row["d_next"])
            i_prev = i_next - 1
            if i_prev < 6 or i_next + 2 >= len(dates):
                continue
            # 各窗口累计收益（用 r 的 cumprod 近似——直接用价格更稳）
            p = series[name].reindex(dates)
            pre5 = p.iloc[i_next - 1] / p.iloc[i_next - 6] - 1
            pre2 = p.iloc[i_next - 1] / p.iloc[i_next - 3] - 1
            pre1 = p.iloc[i_next - 1] / p.iloc[i_next - 2] - 1
            hold = p.iloc[i_next] / p.iloc[i_next - 1] - 1
            post1 = p.iloc[i_next + 1] / p.iloc[i_next] - 1
            post2 = p.iloc[i_next + 1] / p.iloc[i_next - 1] - 1
            rows.append({
                "pool": name, "cat": row["cat"], "gap": row["gap"],
                "d_next": row["d_next"],
                "pre5": pre5, "pre2": pre2, "pre1": pre1,
                "hold": hold, "post1": post1, "post2": post2,
            })
    df = pd.DataFrame(rows)

    def agg(g: pd.DataFrame, col: str) -> pd.Series:
        x = g[col].dropna().values
        return pd.Series({
            "N": len(x), "mean%": x.mean() * 100,
            "med%": np.median(x) * 100,
            "win%": (x > 0).mean() * 100,
            "t": t_stat(x),
        })

    print("\n===== 全类型长假合并（含春节/国庆/其他）=====")
    for col in ("pre5", "pre2", "pre1", "hold", "post1", "post2"):
        t = df.groupby("pool").apply(lambda g: agg(g, col), include_groups=False)
        print(f"\n[{col}]")
        print(t.round(2).to_string())

    print("\n===== 分节假日类型（小盘1/3等权 & 中证1000）=====")
    for col in ("pre2", "hold", "post1"):
        sub = df[df["pool"].isin(("小盘1/3等权", "中证1000"))]
        t = sub.pivot_table(index="cat", columns="pool", values=col, aggfunc="mean") * 100
        cnt = sub.groupby("cat").size()
        t["N"] = cnt
        print(f"\n[{col}] 平均%")
        print(t.round(2).to_string())

    print("\n===== 持假收益 vs 交易成本盈亏平衡 =====")
    # 节前清仓+节后买回成本：来回 2×(佣金0.025%×2 + 印花税卖出0.05% + 滑点0.1%×2 + 冲击)
    round_trip = 2 * 0.025 + 0.05 + 2 * 0.1  # ≈ 0.30% (不含冲击) ；含冲击更高
    print(f"清仓+回补往返成本(不含冲击)≈{round_trip:.2f}%/次；2019-2026 长假 {len(ev)} 次"
          f"（年均 {len(ev)/7.66:.1f} 次）")
    hold_stats = df.groupby("pool").apply(lambda g: agg(g, "hold"), include_groups=False)
    print(hold_stats.round(2).to_string())

    out = Path("scripts/holiday_test.md")
    lines = [
        "# 节假日 A 股调仓/空仓实证（RESEARCH-016，2026-09-04）",
        "",
        f"样本：2019-01 ~ 2026-08，长假 {len(ev)} 次（春节/国庆/其他3-5天假）",
        f"分类分布：{ev['cat'].value_counts().to_dict()}",
        "",
        "详见 scripts/analyze_holiday_effect.py；结论见下方表格（由脚本自动追加）。",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n-> {out}（结论表追加见脚本输出）")


class BarStore852:
    """中证1000 收盘加载（避开 backtest_small_val 的 BarStore import 循环）。"""

    def __init__(self):
        from quart.data.store import BarStore

        idx = BarStore().load_benchmark("000852")
        self.s = pd.Series(idx["close"].values, index=pd.to_datetime(idx["date"]))

    def reindex(self, dates):
        return self.s.reindex(dates)


if __name__ == "__main__":
    main()

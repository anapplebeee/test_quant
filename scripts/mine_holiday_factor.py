"""RESEARCH-016b：节日因子挖掘 v2（修正成分跳变口径）。

方法论修正：v1 用"等权价格均值序列"的 pct_change，成分变化（科创板开板/新股进
小盘池）会产生假跳变（2019"其他"类 +38% 即此伪影）。v2 改用**日收益均值序列**：
r_t = mean_i( close_i,t / close_i,t-1 - 1 )（每日再平衡等权，新股首日 NaN 自动跳过），
净值 = cumprod(r)。假期收益天然合并进节后首日 r：持假收益 = r[T+1]。

F1 持假因子分类型×分年；F2 窗口矩阵 T-k→T+m；F3 条件收益差（Welch t）；
F4 五一节后重入时机（3.3 预注册输入）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from analyze_holiday_effect import classify_holiday  # noqa: E402
from backtest_small_val import BarStore, load_fundamental_panel, load_md  # noqa: E402

START = pd.Timestamp("2019-01-01")
END = pd.Timestamp("2026-08-31")
SEG1_END = pd.Timestamp("2022-12-31")


def t_stat(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    if len(x) < 3 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def main():
    md = load_md()
    fund = load_fundamental_panel(md.dates)
    dates = md.dates
    fm_rank = fund["fmcap"].rank(axis=1, pct=True)

    # 日收益均值序列（每日再平衡等权；成分切换无跳变）
    rets = {
        "小盘1/3等权": md.close_val.where(fm_rank <= 1 / 3).pct_change().mean(axis=1, skipna=True),
        "中证1000": pd.Series(md.benchmark_close, index=dates).pct_change(),
        "全A等权": md.close_val.pct_change().mean(axis=1, skipna=True),
    }
    navs = {k: (1 + v.fillna(0)).cumprod() for k, v in rets.items()}

    events = []
    for i in range(1, len(dates)):
        cat = classify_holiday(dates[i - 1], dates[i])
        if cat:
            events.append({"i_next": i, "d_prev": dates[i - 1], "d_next": dates[i], "cat": cat})
    ev = pd.DataFrame(events)
    ev = ev[(ev.d_next >= START) & (ev.d_next <= END)].reset_index(drop=True)
    print(f"长假事件 {len(ev)} 次（2019-2026）")

    # ---------- F1 持假因子：r[T+1] 分类型 × 分年 ----------
    print("\n===== F1 持假因子（=节后首日日收益 r[T+1]）分类型 × 分年：小盘1/3等权 =====")
    rs = rets["小盘1/3等权"]
    hold = []
    for _, row in ev.iterrows():
        inx = int(row["i_next"])
        if inx >= len(dates):
            continue
        hold.append({"cat": row["cat"], "year": row["d_next"].year, "ret": rs.iloc[inx]})
    hdf = pd.DataFrame(hold)
    tab = (hdf.pivot_table(index="cat", columns="year", values="ret", aggfunc="mean") * 100).round(2)
    tab["N"] = hdf.groupby("cat").size()
    tab["总均值%"] = (hdf.groupby("cat")["ret"].mean() * 100).round(2)
    tab["t"] = hdf.groupby("cat")["ret"].apply(lambda x: t_stat(x.values)).round(2)
    tab["胜率%"] = (hdf.groupby("cat")["ret"].apply(lambda x: (x > 0).mean()) * 100).round(0)
    print(tab.to_string())

    print("\n-- 分年全类型合并 --")
    ytab = pd.DataFrame({
        "均值%": hdf.groupby("year")["ret"].mean() * 100,
        "中位%": hdf.groupby("year")["ret"].median() * 100,
        "胜率%": hdf.groupby("year")["ret"].apply(lambda x: (x > 0).mean()) * 100,
        "N": hdf.groupby("year").size(),
    }).round(2)
    print(ytab.to_string())

    # ---------- F2 窗口矩阵（cumprod 日收益） ----------
    print("\n===== F2 窗口矩阵：节前T-k收盘买入→节后T+m收盘卖出（小盘1/3等权）=====")
    rows = []
    arr = rs.values
    for k in (1, 2, 3, 5):
        for m in (1, 2, 3):
            vals, segs1, segs2 = [], [], []
            for _, row in ev.iterrows():
                ip, inx = int(row["i_next"]) - 1, int(row["i_next"])
                lo, hi = ip - k + 1, inx + m - 1
                if lo < 1 or hi >= len(arr):
                    continue
                v = float(np.prod(1 + arr[lo:hi + 1]) - 1)
                vals.append(v)
                (segs1 if row["d_next"] <= SEG1_END else segs2).append(v)
            rows.append({
                "窗口": f"T-{k}→T+{m}", "N": len(vals),
                "均值%": np.mean(vals) * 100, "胜率%": (np.array(vals) > 0).mean() * 100,
                "t": t_stat(np.array(vals)),
                "19-22%": np.mean(segs1) * 100 if segs1 else np.nan,
                "23-26%": np.mean(segs2) * 100 if segs2 else np.nan,
            })
    print(pd.DataFrame(rows).round(2).to_string(index=False))

    # ---------- F3 条件收益差 ----------
    print("\n===== F3 条件收益差：窗口日 vs 其他交易日（Welch t）=====")
    prev_dates, next_dates = set(), set()
    for _, row in ev.iterrows():
        ip, inx = int(row["i_next"]) - 1, int(row["i_next"])
        prev_dates.update(dates[max(ip - 1, 0):ip + 1])
        next_dates.update(dates[inx:min(inx + 2, len(dates))])
    for name, r in rets.items():
        r = r[(r.index >= START) & (r.index <= END)].dropna()
        arr = r.values
        in_prev = np.array([d in prev_dates for d in r.index])
        in_next = np.array([d in next_dates for d in r.index])
        base = arr[~(in_prev | in_next)]
        for tag, mask in (("节前T-2,T-1", in_prev), ("节后T+1,T+2", in_next)):
            x = arr[mask]
            diff = x.mean() - base.mean()
            se = np.sqrt(x.var(ddof=1) / len(x) + base.var(ddof=1) / len(base))
            tt = diff / se if se > 0 else np.nan
            print(f"  {name:<10} {tag:<12} 窗口日均 {x.mean()*100:+.3f}%  其他 {base.mean()*100:+.3f}%  "
                  f"差 {diff*100:+.3f}%/日  t={tt:+.2f}")

    # ---------- F4 五一节后重入时机 ----------
    print("\n===== F4 五一节后重入（4月空仓期结束=5月首日=五一节后首日）=====")
    wuyi = ev[ev["cat"] == "五一"]
    for name, r in rets.items():
        r = r[(r.index >= START) & (r.index <= END)]
        pos = {d: i for i, d in enumerate(r.index)}
        first_day, second_day = [], []
        for _, row in wuyi.iterrows():
            d = row["d_next"]
            if d not in pos:
                continue
            i = pos[d]
            vals = r.values
            first_day.append(vals[i])
            if i + 1 < len(vals):
                second_day.append(vals[i + 1])
        f, s = np.array(first_day), np.array(second_day)
        print(f"  {name}: 节后首日 {f.mean()*100:+.2f}%（N={len(f)}）  次日 {s.mean()*100:+.2f}%  "
              f"重入差（首日买-次日买） {(f.mean()-s.mean())*100:+.2f}pp")
    print("  判据（预注册）：重入差绝对值 <1pp 或方向不稳 → 维持 5 月首日重入，不改规则。")


if __name__ == "__main__":
    main()

"""研究产物 API：参数扫描（sweep_*.csv）与研究报告（*.md）的统一读取层。

背景（2026-08-28 审计）：新验证的策略结果（18 组合终版 sweep、调仓周期曲线、
缓冲带对照、随机基线定标）只存在于 reports/ 的 csv/md 中，前端任何页面都
看不到——"新验证的策略结果在哪"由此而来。本模块供回测中心/首页取数。
"""
from __future__ import annotations

import glob
import os
import re

import pandas as pd
from loguru import logger


def _warn(where: str, exc: Exception) -> None:
    logger.warning("research_api[{}] degraded: {}", where, exc)


def _safe_name(name: str) -> bool:
    return bool(name) and not re.search(r"[\\/]", name) and name not in (".", "..")


# ---------------- 参数扫描 ----------------

# 展示列（存在才展示，顺序即展示顺序）
SWEEP_SHOW_COLS = [
    "label", "cagr", "sharpe", "max_drawdown", "calmar",
    "bench_excess_cagr", "turnover", "n_trades",
]


def list_sweeps() -> list[str]:
    """参数扫描结果文件名列表（不含 equity 曲线文件），文件名升序=时间升序。"""
    files = [
        os.path.basename(f)
        for f in glob.glob(os.path.join("reports", "sweep_*.csv"))
        if not os.path.basename(f).startswith("sweep_equity_")
    ]
    return sorted(files)


def load_sweep(fname: str) -> pd.DataFrame | None:
    if not _safe_name(fname) or not fname.startswith("sweep_") or fname.startswith("sweep_equity_"):
        return None
    path = os.path.join("reports", fname)
    if not os.path.isfile(path):
        return None
    try:
        return pd.read_csv(path)
    except Exception as e:
        _warn("load_sweep", e)
        return None


def sweep_headline(df: pd.DataFrame | None, top: int = 8) -> pd.DataFrame | None:
    """取扫描结果的关键列，按 CAGR 降序截取前 top 行。"""
    if df is None or df.empty:
        return None
    cols = [c for c in SWEEP_SHOW_COLS if c in df.columns]
    out = df[cols].copy()
    for c in ("cagr", "sharpe", "max_drawdown", "bench_excess_cagr", "turnover"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "cagr" in out.columns:
        out = out.sort_values("cagr", ascending=False)
    out = out.head(top)
    # 年度列（纯数字列名）附在后面，便于看年度稳定性
    year_cols = [c for c in df.columns if re.fullmatch(r"\d{4}", str(c))]
    if year_cols:
        out = out.join(df[year_cols])
    return out.reset_index(drop=True)


def _parse_sweep_name(fname: str) -> tuple[str, str] | None:
    """sweep_{strategy}_{YYYYMMDD}_{HHMMSS}.csv → (strategy, stamp)。"""
    stem = fname[len("sweep_"):-len(".csv")] if fname.endswith(".csv") else fname
    parts = stem.rsplit("_", 2)
    if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
        return None
    return parts[0], parts[1] + parts[2]


def latest_sweep_headlines() -> pd.DataFrame:
    """每个策略取其最新一次扫描的最优行（CAGR 最高），供首页「最新验证结果」。"""
    rows: list[dict] = []
    for fname in list_sweeps():
        parsed = _parse_sweep_name(fname)
        if not parsed:
            continue
        strategy, stamp = parsed
        df = load_sweep(fname)
        head = sweep_headline(df, top=1)
        if head is None or head.empty:
            continue
        r = head.iloc[0].to_dict()
        rows.append(
            {
                "策略": strategy,
                "最优组合": r.get("label", "-"),
                "CAGR": pd.to_numeric(pd.Series([r.get("cagr")]), errors="coerce").iloc[0],
                "夏普": pd.to_numeric(pd.Series([r.get("sharpe")]), errors="coerce").iloc[0],
                "最大回撤": pd.to_numeric(pd.Series([r.get("max_drawdown")]), errors="coerce").iloc[0],
                "换手x": pd.to_numeric(pd.Series([r.get("turnover")]), errors="coerce").iloc[0],
                "扫描文件": fname,
                "_stamp": stamp,
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("_stamp").drop_duplicates("策略", keep="last")
    return df.drop(columns="_stamp").sort_values("CAGR", ascending=False).reset_index(drop=True)


# ---------------- 研究报告 ----------------

def list_research_reports() -> list[str]:
    """reports/ 下的研究/审计报告（md），文件名升序。"""
    files = [os.path.basename(f) for f in glob.glob(os.path.join("reports", "*.md"))]
    return sorted(files)


def load_research_report(fname: str) -> str:
    if not _safe_name(fname) or not fname.endswith(".md"):
        return "⚠️ 非法文件名"
    path = os.path.join("reports", fname)
    if not os.path.isfile(path):
        return "⚠️ 文件不存在"
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        _warn("load_research_report", e)
        return "⚠️ 读取失败"
    # 超长报告截断（gr.Markdown 渲染保护）
    limit = 60_000
    if len(content) > limit:
        content = content[:limit] + "\n\n*（内容过长已截断，请直接查看 reports/ 下原文件）*"
    return content

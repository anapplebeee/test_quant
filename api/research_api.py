"""研究产物 API：参数扫描（sweep_*.csv）与研究报告（*.md）的统一读取层。

背景（2026-08-28 审计）：新验证的策略结果（18 组合终版 sweep、调仓周期曲线、
缓冲带对照、随机基线定标）只存在于 reports/ 的 csv/md 中，前端任何页面都
看不到——"新验证的策略结果在哪"由此而来。本模块供回测中心/首页取数。

路径一律走 `common.reports_dir()`，不再硬编码 "reports"。
"""
from __future__ import annotations

import re

import pandas as pd

from common import degraded, reports_dir, safe_path, valid_name


def _warn(where: str, exc: BaseException) -> None:
    degraded(f"research_api[{where}]", exc)


def _safe_name(name: str) -> bool:
    """文件名白名单：只允许字母数字下划线与点，禁止路径分隔符与 . .。"""
    if not name or name in (".", "..") or ".." in name:
        return False
    return valid_name(re.sub(r"[^A-Za-z0-9_]", "_", name))


def _resolve(fname: str):
    """在 reports/ 内解析文件名，防目录穿越；非法或不存在返回 None。"""
    if not _safe_name(fname):
        return None
    path = safe_path(reports_dir(), fname)
    return path if path is not None and path.is_file() else None


# ---------------- 参数扫描 ----------------

# 展示列（存在才展示，顺序即展示顺序）
SWEEP_SHOW_COLS = [
    "label", "cagr", "sharpe", "max_drawdown", "calmar",
    "bench_excess_cagr", "turnover", "n_trades",
]


def list_sweeps() -> list[str]:
    """参数扫描结果文件名列表（不含 equity 曲线文件），文件名升序=时间升序。"""
    return sorted(
        p.name
        for p in reports_dir().glob("sweep_*.csv")
        if not p.name.startswith("sweep_equity_")
    )


def load_sweep(fname: str) -> pd.DataFrame | None:
    if not fname.startswith("sweep_") or fname.startswith("sweep_equity_"):
        return None
    path = _resolve(fname)
    if path is None:
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
    return sorted(p.name for p in reports_dir().glob("*.md"))


def load_research_report(fname: str) -> str:
    """读取研究报告内容。

    返回 `str` 是被 Gradio 回调约束的——`rep_dd.change(load_research_report, ...)`
    需要简单类型，失败只能以 ⚠️ 前缀文案表达。

    业务调用方请改用 `load_research_report_result()`：它返回
    `ReportResult`，用字段区分成功与失败，不必靠字符串匹配判断。
    """
    result = load_research_report_result(fname)
    return result.content if result.ok else f"⚠️ {result.error}"


class ReportResult:
    """结构化读取结果（API 层不产出 UI 文案，由调用方决定如何展示）。"""

    __slots__ = ("content", "error")

    def __init__(self, content: str = "", error: str | None = None):
        self.content = content
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None


def load_research_report_result(fname: str) -> ReportResult:
    if not fname.endswith(".md") or not _safe_name(fname):
        return ReportResult(error="非法文件名（仅支持 reports/ 下的 .md）")
    path = _resolve(fname)
    if path is None:
        return ReportResult(error="文件不存在")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        _warn("load_research_report", e)
        return ReportResult(error=f"读取失败: {e}")
    # 超长报告截断（gr.Markdown 渲染保护）
    limit = 60_000
    if len(content) > limit:
        content = content[:limit] + "\n\n*（内容过长已截断，请直接查看 reports/ 下原文件）*"
    return ReportResult(content=content)

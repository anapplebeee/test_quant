"""策略准入自动化门禁（CODEX_PROGRESS 未完成事项 #3）。

背景：策略升级为正式信号（加入 `strategy.live_allowlist`）此前靠人工流程。
本模块把"回测 + 0/1/2 倍成本压力 + WFA 样本外"固化为可自动执行的检查，
并维护 `data/meta/admission_status.csv` 状态台账，供测试强制校验：
**白名单内的每个策略都必须在台账中有 PASS 记录（或历史人工准入的
GRANDFATHERED 标记），否则测试红 —— 防止绕过门禁直接改配置。**

门禁阈值（默认，可在 config `admission:` 覆盖）：
- 2 倍成本下 CAGR > 0            —— 成本压力后仍赚钱
- 1 倍成本下 Sharpe >= 0.5        —— 风险调整后收益达标
- 1 倍成本下最大回撤 >= -45%      —— 尾部风险可控
- 1 倍成本下对等权基准超额 > 0    —— 确认是选股 alpha 而非贝塔
- 交易笔数 >= 30                  —— 统计显著性下限
- WFA OOS CAGR > 0 且回撤 >= -60% —— 样本外不失效（WFA 跳过则门禁不通过）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from loguru import logger

from quart.config import data_root

STATUS_PATH = Path(data_root()) / "meta" / "admission_status.csv"

DEFAULT_THRESHOLDS: dict[str, float] = {
    "cagr_2x_min": 0.0,
    "sharpe_1x_min": 0.5,
    "mdd_1x_max": -0.45,
    "excess_cagr_1x_min": 0.0,
    "min_trades": 30,
    "wfa_oos_cagr_min": 0.0,
    "wfa_oos_mdd_max": -0.60,
}


@dataclass
class GateResult:
    passed: bool
    checks: list[dict] = field(default_factory=list)

    @property
    def failed_reasons(self) -> list[str]:
        return [c["check"] for c in self.checks if c["status"] == "FAIL"]


def evaluate_gates(
    cost_summaries: dict[float, dict],
    wfa_summary: dict | None,
    thresholds: dict[str, float] | None = None,
) -> GateResult:
    """纯函数评估门禁（便于单测）。

    Args:
        cost_summaries: {cost_multiplier: summarize() 结果}，键需含 1.0 和 2.0；
                        缺键时对应检查记 FAIL（数据不足不能放行）。
        wfa_summary: WFA OOS 汇总（summarize 口径）；None = 跳过 WFA → 不通过。
        thresholds: 覆盖默认阈值。
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    checks: list[dict] = []

    def _add(name: str, ok: bool, detail: str) -> None:
        checks.append(
            {"check": name, "status": "PASS" if ok else "FAIL", "detail": detail}
        )

    s1 = cost_summaries.get(1.0) or {}
    s2 = cost_summaries.get(2.0) or {}

    cagr2 = s2.get("cagr")
    _add(
        "cagr_2x_min",
        cagr2 is not None and float(cagr2) > float(th["cagr_2x_min"]),
        f"2x cost CAGR={_fmt(cagr2, 'pct')} (>{_fmt(th['cagr_2x_min'], 'pct')})",
    )
    sharpe1 = s1.get("sharpe")
    _add(
        "sharpe_1x_min",
        sharpe1 is not None and float(sharpe1) >= float(th["sharpe_1x_min"]),
        f"1x cost Sharpe={_fmt(sharpe1)} (>={th['sharpe_1x_min']})",
    )
    mdd1 = s1.get("max_drawdown")
    _add(
        "mdd_1x_max",
        mdd1 is not None and float(mdd1) >= float(th["mdd_1x_max"]),
        f"1x cost MDD={_fmt(mdd1, 'pct')} (>={_fmt(th['mdd_1x_max'], 'pct')})",
    )
    excess1 = s1.get("bench_excess_cagr")
    _add(
        "excess_cagr_1x_min",
        excess1 is not None and float(excess1) > float(th["excess_cagr_1x_min"]),
        f"1x cost excess CAGR={_fmt(excess1, 'pct')}",
    )
    n_trades = s1.get("n_trades")
    _add(
        "min_trades",
        n_trades is not None and int(n_trades) >= int(th["min_trades"]),
        f"trades={n_trades} (>={int(th['min_trades'])})",
    )

    if wfa_summary is None:
        _add("wfa_oos", False, "WFA 未执行 —— 门禁不允许跳过样本外验证")
    else:
        wcagr = wfa_summary.get("cagr")
        wmdd = wfa_summary.get("max_drawdown")
        ok = (
            wcagr is not None
            and float(wcagr) > float(th["wfa_oos_cagr_min"])
            and wmdd is not None
            and float(wmdd) >= float(th["wfa_oos_mdd_max"])
        )
        _add(
            "wfa_oos",
            ok,
            f"OOS CAGR={_fmt(wcagr, 'pct')}, MDD={_fmt(wmdd, 'pct')}",
        )

    return GateResult(passed=all(c["status"] == "PASS" for c in checks), checks=checks)


def _fmt(v, kind: str = "num") -> str:
    if v is None:
        return "N/A"
    if kind == "pct":
        return f"{float(v)*100:+.2f}%"
    return f"{float(v):.3f}"


def write_status(
    strategy: str,
    result: GateResult,
    thresholds_used: dict,
    details_dir: Path | None = None,
) -> Path:
    """把门禁结果写入状态台账（strategy 粒度覆盖写，保留 grandfathered 标记）。"""
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = load_status()

    row = {
        "strategy": strategy,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "passed": result.passed,
        "grandfathered": False,
        "failed": ";".join(result.failed_reasons),
    }
    if strategy in set(existing.get("strategy", [])):
        gf = existing.loc[existing["strategy"] == strategy, "grandfathered"]
        row["grandfathered"] = bool(gf.iloc[0]) if not gf.empty else False
        existing = existing[existing["strategy"] != strategy]

    details_dir = details_dir or STATUS_PATH.parent / "admission_details"
    details_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    details_file = details_dir / f"{strategy}_{stamp}.json"
    with open(details_file, "w", encoding="utf-8") as f:
        json.dump(
            {"strategy": strategy, "passed": result.passed,
             "checks": result.checks, "thresholds": thresholds_used},
            f, ensure_ascii=False, indent=2,
        )
    row["details"] = str(details_file)

    out = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)
    out.to_csv(STATUS_PATH, index=False)
    logger.info("admission status updated: {} passed={} -> {}", strategy, result.passed, STATUS_PATH)
    return STATUS_PATH


def load_status(path: Path | None = None) -> pd.DataFrame:
    """读取准入台账；文件不存在时返回带正确列的空表。"""
    p = path or STATUS_PATH
    cols = ["strategy", "checked_at", "passed", "grandfathered", "failed", "details"]
    if not p.exists():
        return pd.DataFrame(columns=cols)
    try:
        df = pd.read_csv(p)
        for c in cols:
            if c not in df.columns:
                df[c] = False if c in ("passed", "grandfathered") else None
        return df
    except Exception:
        return pd.DataFrame(columns=cols)


def seed_grandfathered(strategies: list[str], path: Path | None = None) -> Path:
    """为存量白名单策略写入 GRANDFATHERED 记录（此前人工准入流程的补登记）。"""
    p = path or STATUS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = load_status(p)
    known = set(existing.get("strategy", []))
    rows = []
    for s in strategies:
        if s in known:
            continue
        rows.append(
            {
                "strategy": s,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "passed": True,
                "grandfathered": True,
                "failed": "",
                "details": "grandfathered: 人工准入（2026-08-31 之前的既有白名单）",
            }
        )
    if rows:
        out = pd.concat([existing, pd.DataFrame(rows)], ignore_index=True)
        out.to_csv(p, index=False)
        logger.info("grandfathered {} strategies: {}", len(rows), [r["strategy"] for r in rows])
    return p


def admission_ok(strategy: str, path: Path | None = None) -> bool:
    """白名单强制校验入口：策略在台账中有 PASS（含 GRANDFATHERED）记录。"""
    df = load_status(path)
    if df.empty:
        return False
    row = df[df["strategy"] == strategy]
    if row.empty:
        return False
    return bool(row["passed"].iloc[0])

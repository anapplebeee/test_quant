"""正式研究审计（RESEARCH-001：OOS/WFA/成本/容量报告可复现）。

背景
----
研究链（DATA-001 → RESEARCH-001 → RESEARCH-002 → Admission Gate）中，
本模块固化"一次正式审计要跑什么、记录什么"：

- **成本压力**：0/1/2 倍成本回测（诚实费用口径，进程内执行）；
- **纯 OOS 冻结验证**：参数不变、只把样本起点前移到冻结日期；
- **WFA 样本外**：子进程执行 `scripts/walk_forward.py`，解析其
  artifacts 的 `oos_summary`；
- **容量代理**：引用最新一次因子审计 run 的 `provisional_baseline`
  （`capacity_proxy_m` 只是流动性代理，见 FACTOR_AUDIT_PROTOCOL）；
- **数据溯源**：DATA-001 内容哈希 snapshot_id + 证券主数据版本 +
  数据区间 + 代码版本，全部写入制品 manifest。

可复现契约
----------
同一次审计的全部结果写入**同一个 Artifact run**（task="research_audit"），
manifest 的 fingerprint = (参数 + 数据版本 + 代码版本) 哈希；数据被修订
→ snapshot_id 变 → 指纹变 → 旧报告自动失效。准入门禁判定复用
`quart.research.admission.evaluate_gates`，不在本模块另设标准。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from loguru import logger

from quart.config import PROJECT_ROOT, load_config
from quart.data.artifacts import ArtifactStore, data_version, git_revision

#: 成本压力默认倍数（0=无成本上界，1=诚实成本，2=压力）
COST_MULTIPLIERS: tuple[float, ...] = (0.0, 1.0, 2.0)


def data_provenance(store=None, snapshot_base: Path | None = None) -> dict:
    """研究数据溯源：数据版本指纹 + 代码版本。

    Args:
        store: BarStore（缺省自动创建）。
        snapshot_base: 快照清单根目录（测试注入用；缺省读真实清单）。
    """
    return {
        "data_version": data_version(store, snapshot_base=snapshot_base),
        "code": git_revision(),
    }


# ---------------- 成本压力回测（进程内） ----------------


def run_single_backtest(strategy: str, cost: float, start: str, end: str | None = None) -> dict:
    """进程内执行单次回测，返回 summarize() 结果 + n_trades。

    与 `scripts/admission_gate.py` 原实现同源：同一策略/参数/数据下
    两次调用必须给出同一结果（确定性由引擎与费用模型保证）。
    """
    from quart.backtest.engine import BacktestEngine
    from quart.backtest.metrics import summarize
    from quart.data.benchmark import equal_weight_benchmark
    from quart.data.market import MarketData
    from quart.data.quality import load_blocklist
    from quart.data.store import BarStore
    from quart.data.universe import filter_for_simulation
    from quart.execution.fees import Fees
    from quart.strategy import build_strategy

    cfg = load_config()
    store = BarStore()
    blocked = load_blocklist()
    bars = store.load(start=start, end=end, exclude_symbols=sorted(blocked))
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[(bench["date"] >= start) & (end is None or bench["date"] <= end)]
    if bars.empty:
        raise SystemExit("本地数据为空，请先运行 scripts/update_data.py")

    data_cfg = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )

    strategy_obj = build_strategy(strategy)
    md = MarketData.from_bars(bars, benchmark=bench)
    fees = Fees.from_config().scaled(cost)
    result = BacktestEngine(md, strategy_obj, fees=fees).run_result()

    bench_close = bench.set_index("date")["close"].reindex(result.equity.index).ffill()
    ew_bench = equal_weight_benchmark(result.equity, bars)
    summary = summarize(
        result.equity, benchmark=bench_close, benchmark2=ew_bench, benchmark2_name="bench2"
    )
    summary["n_trades"] = len(result.trades)
    return summary


def run_cost_stress(
    strategy: str,
    start: str,
    end: str | None = None,
    multipliers: tuple[float, ...] = COST_MULTIPLIERS,
) -> dict[float, dict]:
    """按成本倍数逐一回测，返回 {倍数: summarize 结果}。"""
    out: dict[float, dict] = {}
    for cost in multipliers:
        logger.info("cost stress: {} @ {}x cost [{} ~ {}]", strategy, cost, start, end or "now")
        out[float(cost)] = run_single_backtest(strategy, cost, start, end)
    return out


# ---------------- WFA（子进程） ----------------


def run_wfa_subprocess(strategy: str, start: str, end: str | None = None) -> dict | None:
    """子进程跑 `scripts/walk_forward.py`，解析其 artifacts 的 oos_summary。

    失败（进程退出码非 0 / 找不到制品）返回 None —— 调用方门禁应判不通过，
    不允许把"WFA 没跑"美化成"样本外达标"。
    """
    cmd = [sys.executable, "scripts/walk_forward.py", "--strategy", strategy, "--start", start]
    if end:
        cmd += ["--end", end]
    logger.info("running WFA: {}", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.warning("WFA failed (exit={})", proc.returncode)
        if proc.stdout:
            logger.debug("WFA stdout tail: {}", proc.stdout[-800:])
        return None
    m = re.search(r"artifacts[/\\](wfa_\S+?)[/\\]\s*\(", proc.stdout)
    if not m:
        logger.warning("WFA 完成但未在输出中找到制品目录")
        return None
    manifest = PROJECT_ROOT / "artifacts" / m.group(1) / "oos_summary.json"
    if not manifest.exists():
        logger.warning("未找到 {}", manifest)
        return None
    with open(manifest, encoding="utf-8") as f:
        return json.load(f)


# ---------------- 因子审计引用（容量代理） ----------------


def latest_factor_audit_ref(store: ArtifactStore | None = None) -> dict | None:
    """引用最新一次因子审计 run：run_id / 指纹 / 数据版本 / 容量代理。

    正式审计不重跑因子审计（耗时长且已有独立制品），而是把既有
    run 的指纹钉进本报告 —— 两份制品的数据版本一致才算同一基线。
    无任何因子审计 run 时返回 None（报告中记为缺失）。
    """
    store = store or ArtifactStore()
    run = store.latest(task="factor_audit")
    if run is None:
        return None
    ref: dict = {
        "run_id": run.run_id,
        "fingerprint": run.fingerprint,
        "created_at": run.created_at,
        "data_version": run.data_version,
        "params": run.params,
    }
    baseline = store.read(run.run_id, "provisional_baseline")
    if baseline is not None and not baseline.empty:
        cols = [
            c for c in ("factor", "annual_return", "max_drawdown", "annual_turnover",
                        "top_amount_median_m", "capacity_proxy_m")
            if c in baseline.columns
        ]
        ref["capacity_proxy"] = baseline[cols].to_dict(orient="records")
    return ref


# ---------------- 报告渲染 ----------------


def _pct(v) -> str:
    return "N/A" if v is None else f"{float(v) * 100:+.2f}%"


def _num(v, nd: int = 3) -> str:
    return "N/A" if v is None else f"{float(v):.{nd}f}"


def render_formal_report(
    strategy: str,
    start: str,
    end: str | None,
    oos_start: str | None,
    provenance: dict,
    cost_summaries: dict[float, dict],
    oos_summary: dict | None,
    wfa_summary: dict | None,
    gate_result,
    factor_ref: dict | None,
    run_id: str,
    fingerprint: str = "",
    thresholds: dict | None = None,
) -> str:
    """把正式审计结果渲染为 Markdown（报告正文，指标以制品为准）。"""
    dv = provenance.get("data_version", {})
    snap_ids = dv.get("snapshot_ids", {})
    lines: list[str] = [
        f"# 正式研究审计报告：{strategy}",
        "",
        f"- 报告 run：`{run_id}`（制品指纹 `{fingerprint or '见 manifest'}`）",
        f"- 样本区间：{start} ~ {end or '最新'}；纯 OOS 冻结起点：{oos_start or '未指定'}",
        f"- 代码版本：`{provenance.get('code')}`",
        "",
        "## 数据溯源（DATA-001）",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 股票数 | {dv.get('symbols')} |",
        f"| 数据区间 | {dv.get('first_date')} ~ {dv.get('last_date')} |",
        f"| daily snapshot_id | `{snap_ids.get('daily')}` |",
        f"| index snapshot_id | `{snap_ids.get('index')}` |",
        f"| 证券主数据版本 | `{dv.get('security_master_version')}` |",
        "",
    ]
    if snap_ids.get("daily") is None:
        lines += [
            "> ⚠️ daily 快照未构建：本报告无法识别历史行情修订，"
            "请先运行 `scripts/data_snapshot.py build` 后重跑。",
            "",
        ]

    lines += ["## 成本压力（进程内回测，诚实费用口径）", "",
              "| 成本倍数 | CAGR | Sharpe | 最大回撤 | 超额(vs等权) | 交易笔数 |",
              "|---|---|---|---|---|---|"]
    for cost in sorted(cost_summaries):
        s = cost_summaries[cost]
        lines.append(
            f"| {cost:g}x | {_pct(s.get('cagr'))} | {_num(s.get('sharpe'))} "
            f"| {_pct(s.get('max_drawdown'))} | {_pct(s.get('bench_excess_cagr'))} "
            f"| {s.get('n_trades')} |"
        )
    lines.append("")

    if oos_summary is not None:
        lines += ["## 纯 OOS 冻结验证（参数不变）", "",
                  "| CAGR | Sharpe | 最大回撤 | 超额(vs等权) | 交易笔数 |",
                  "|---|---|---|---|---|",
                  f"| {_pct(oos_summary.get('cagr'))} | {_num(oos_summary.get('sharpe'))} "
                  f"| {_pct(oos_summary.get('max_drawdown'))} "
                  f"| {_pct(oos_summary.get('bench_excess_cagr'))} "
                  f"| {oos_summary.get('n_trades')} |", ""]
    else:
        lines += ["## 纯 OOS 冻结验证", "", "未执行（未提供 --oos-start）。", ""]

    if wfa_summary is not None:
        lines += ["## WFA 样本外", "",
                  "| CAGR | Sharpe | 最大回撤 |",
                  "|---|---|---|",
                  f"| {_pct(wfa_summary.get('cagr'))} | {_num(wfa_summary.get('sharpe'))} "
                  f"| {_pct(wfa_summary.get('max_drawdown'))} |", ""]
    else:
        lines += ["## WFA 样本外", "", "未执行或失败 —— 门禁不允许跳过样本外验证。", ""]

    lines += ["## 容量与因子审计引用", ""]
    if factor_ref is None:
        lines += ["无可用因子审计 run（先运行 `scripts/factor_audit.py`）。", ""]
    else:
        lines += [f"- 因子审计 run：`{factor_ref['run_id']}`（指纹 `{factor_ref['fingerprint']}`）", ""]
        proxy = factor_ref.get("capacity_proxy")
        if proxy:
            lines += ["| 因子 | Top篮子年化 | 年化换手 | 成交额中位数(百万) | 容量代理(百万) |",
                      "|---|---|---|---|---|"]
            for row in proxy:
                lines.append(
                    f"| {row.get('factor')} | {_pct(row.get('annual_return'))} "
                    f"| {_num(row.get('annual_turnover'), 2)} "
                    f"| {_num(row.get('top_amount_median_m'), 1)} "
                    f"| {_num(row.get('capacity_proxy_m'), 1)} |"
                )
            lines += ["", "> `capacity_proxy_m` = Top 篮子 20 日成交额中位数 × 10%，"
                      "是流动性代理，不是可投资容量。", ""]

    lines += ["## 准入门禁判定", ""]
    for c in gate_result.checks:
        mark = "✅" if c["status"] == "PASS" else "❌"
        lines.append(f"- {mark} `{c['check']}`：{c['detail']}")
    verdict = "**PASS —— 可申请加入 live_allowlist**" if gate_result.passed else "**FAIL —— 禁止晋级实盘**"
    lines += ["", f"门禁结论：{verdict}", ""]
    if thresholds:
        lines += ["阈值：" + "; ".join(f"{k}={v}" for k, v in thresholds.items()), ""]
    return "\n".join(lines)


__all__ = [
    "COST_MULTIPLIERS",
    "data_provenance",
    "latest_factor_audit_ref",
    "render_formal_report",
    "run_cost_stress",
    "run_single_backtest",
    "run_wfa_subprocess",
]

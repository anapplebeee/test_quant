"""正式研究审计统一入口（RESEARCH-001）。

一条命令产出可复现的正式审计报告：成本压力（0/1/2x）+ 纯 OOS 冻结验证
+ WFA 样本外 + 因子审计容量代理引用 + Admission Gate 判定，全部写入
同一个 Artifact run（task="research_audit"），并在 reports/ 落兼容副本。

可复现契约
----------
- manifest 记录参数 + 数据版本（含 DATA-001 snapshot_id）+ 代码版本指纹；
- 数据修订 → snapshot_id 变 → 指纹变 → 旧报告自动失效；
- 建议先运行 `scripts/data_snapshot.py build` 让快照进入内容哈希口径。

用法：
    uv run python scripts/research_audit.py --strategy lowvol_indz \
        --start 2024-03-01 --oos-start 2025-09-01
    uv run python scripts/research_audit.py --strategy lowvol_indz \
        --param size_weight=0.3 --param turnover_weight=0.2 \
        --start 2024-03-01 --oos-start 2025-09-01
    uv run python scripts/research_audit.py --strategy lowvol_indz \
        --start 2022-01-01 --oos-start 2025-01-01 --skip-wfa
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common
from quart.config import load_config
from quart.data.artifacts import ArtifactStore
from quart.research.admission import DEFAULT_THRESHOLDS, evaluate_gates
from quart.research.formal_audit import (
    data_provenance,
    latest_factor_audit_ref,
    render_formal_report,
    run_cost_stress,
    run_wfa_subprocess,
)

console = Console()


def _parse_value(raw: str):
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(description="正式研究审计（OOS/WFA/成本/容量）")
    parser.add_argument("--strategy", default=load_config()["strategy"]["name"])
    parser.add_argument("--start", default="2024-03-01", help="全样本起点")
    parser.add_argument("--end", default=None)
    parser.add_argument("--oos-start", default=None,
                        help="纯 OOS 冻结起点（参数不变，仅前推样本）")
    parser.add_argument("--skip-wfa", action="store_true",
                        help="跳过 WFA（门禁将判 FAIL，仅用于诊断）")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                        help="候选参数覆盖，可重复（如 --param size_weight=0.3）")
    parser.add_argument("--save-dir", default=str(common.reports_dir()),
                        help="兼容报告输出目录")
    args = parser.parse_args()

    extra_params: dict = {}
    for spec in args.param:
        key, sep, raw = spec.partition("=")
        if not sep:
            raise SystemExit(f"--param 格式应为 KEY=VALUE，收到 {spec!r}")
        extra_params[key.strip()] = _parse_value(raw.strip())

    cfg = load_config()
    thresholds = {**DEFAULT_THRESHOLDS,
                  **{k: float(v) for k, v in cfg.get("admission", {}).items()}}

    store = ArtifactStore()
    params = {
        "strategy": args.strategy,
        "start": args.start,
        "end": args.end,
        "oos_start": args.oos_start,
        "skip_wfa": args.skip_wfa,
        "param_overrides": extra_params or None,
        "thresholds": thresholds,
    }
    run = store.create_run("research_audit", params)
    console.print(f"[blue]research_audit run: {run.manifest.run_id}[/blue]")

    provenance = data_provenance()
    snap_ids = provenance["data_version"].get("snapshot_ids", {})
    if snap_ids.get("daily") is None:
        console.print("[yellow]daily 快照未构建（无法识别历史修订）："
                      "建议先运行 scripts/data_snapshot.py build[/yellow]")

    label = args.strategy
    if extra_params:
        label += " (" + ", ".join(f"{k}={v}" for k, v in sorted(extra_params.items())) + ")"

    console.print(f"[blue]成本压力回测 {label} [0/1/2x] ...[/blue]")
    cost_summaries = run_cost_stress(args.strategy, args.start, args.end,
                                    params=extra_params or None)
    for cost, s in sorted(cost_summaries.items()):
        console.print(f"  {cost:g}x: CAGR={s.get('cagr')}, Sharpe={s.get('sharpe')}, "
                      f"MDD={s.get('max_drawdown')}, trades={s.get('n_trades')}")

    oos_summary = None
    if args.oos_start:
        console.print(f"[blue]纯 OOS 冻结验证 [{args.oos_start} ~ ] @ 1x cost ...[/blue]")
        oos_summary = run_cost_stress(
            args.strategy, args.oos_start, args.end, multipliers=(1.0,),
            params=extra_params or None,
        )[1.0]
        console.print(f"  OOS: CAGR={oos_summary.get('cagr')}, "
                      f"Sharpe={oos_summary.get('sharpe')}, MDD={oos_summary.get('max_drawdown')}")
    else:
        console.print("[yellow]未提供 --oos-start，跳过纯 OOS 冻结验证[/yellow]")

    wfa_summary = None
    if not args.skip_wfa:
        wfa_summary = run_wfa_subprocess(args.strategy, args.start, args.end,
                                         params=extra_params or None)
        if wfa_summary is None:
            console.print("[red]WFA 未执行或失败 —— 门禁将判 FAIL[/red]")
    else:
        console.print("[yellow]--skip-wfa：跳过 WFA（门禁将判 FAIL）[/yellow]")

    factor_ref = latest_factor_audit_ref(store)
    if factor_ref is None:
        console.print("[yellow]无因子审计 run 引用（先运行 scripts/factor_audit.py）[/yellow]")

    result = evaluate_gates(cost_summaries, wfa_summary, thresholds)

    report_md = render_formal_report(
        strategy=label,
        start=args.start,
        end=args.end,
        oos_start=args.oos_start,
        provenance=provenance,
        cost_summaries=cost_summaries,
        oos_summary=oos_summary,
        wfa_summary=wfa_summary,
        gate_result=result,
        factor_ref=factor_ref,
        run_id=run.manifest.run_id,
        fingerprint=run.manifest.fingerprint,
        thresholds=thresholds,
    )

    run.put_json("provenance", provenance)
    run.put_json("cost_stress", {f"{k:g}x": v for k, v in sorted(cost_summaries.items())})
    if oos_summary is not None:
        run.put_json("oos_summary", oos_summary)
    if wfa_summary is not None:
        run.put_json("wfa_oos", wfa_summary)
    if factor_ref is not None:
        run.put_json("factor_audit_ref", factor_ref)
    run.put_json("gate_result", {
        "passed": result.passed,
        "checks": result.checks,
        "thresholds": thresholds,
    })
    run.put_text("report", report_md)
    s1 = cost_summaries.get(1.0, {})
    run.add_metrics(
        cagr_1x=s1.get("cagr"),
        sharpe_1x=s1.get("sharpe"),
        max_drawdown_1x=s1.get("max_drawdown"),
        cagr_2x=cost_summaries.get(2.0, {}).get("cagr"),
        oos_cagr=(oos_summary or {}).get("cagr"),
        gate_passed=result.passed,
    )
    manifest = run.finish()

    out_dir = Path(args.save_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^0-9A-Za-z=_]+", "_", label)
    compat = out_dir / f"research_audit_{slug}_{dt.date.today():%Y%m%d}.md"
    compat.write_text(report_md, encoding="utf-8")

    verdict = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
    console.print(f"门禁结论: {verdict}（{'; '.join(result.failed_reasons) or '全部通过'}）")
    console.print(f"制品: artifacts/{manifest.run_id}/  报告: {compat}")
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()

"""制品 API：按 run_id 查询运行结果。

替代此前 `glob("reports/summary_*.csv") + mtime` 的猜测式产出发现。
前端逐步从 backtest_api/research_api 迁移到本模块。
"""
from __future__ import annotations

import json

import pandas as pd

from common import degraded, safe_path
from quart.data.artifacts import STATUS_FAILED, STATUS_OK, ArtifactStore


def _store() -> ArtifactStore:
    return ArtifactStore()


def list_runs(task: str | None = None, limit: int = 50) -> pd.DataFrame:
    """运行列表（最新在前）。

    返回列：run_id / task / created_at / status / fingerprint /
    symbols / last_date / 关键指标
    """
    try:
        runs = _store().list_runs(task=task)[:limit]
    except Exception as e:
        degraded("artifacts.list_runs", e)
        return pd.DataFrame()

    rows = []
    for m in runs:
        rows.append({
            "run_id": m.run_id,
            "task": m.task,
            "created_at": m.created_at,
            "status": m.status,
            "fingerprint": m.fingerprint[:12],
            "data_symbols": (m.data_version or {}).get("symbols"),
            "data_last_date": (m.data_version or {}).get("last_date"),
            "code": m.code,
            **{f"m_{k}": v for k, v in (m.metrics or {}).items() if not isinstance(v, dict)},
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def latest_run(task: str | None = None) -> dict | None:
    m = _store().latest(task=task, status=STATUS_OK)
    return _manifest_to_dict(m) if m else None


def get_run(run_id: str) -> dict | None:
    if not run_id or "/" in run_id or ".." in run_id:
        return None
    return _manifest_to_dict(_store().load_manifest(run_id))


def _manifest_to_dict(m) -> dict | None:
    if m is None:
        return None
    return {
        "run_id": m.run_id,
        "task": m.task,
        "created_at": m.created_at,
        "status": m.status,
        "params": m.params,
        "data_version": m.data_version,
        "code": m.code,
        "fingerprint": m.fingerprint,
        "metrics": m.metrics,
        "error": m.error,
        "artifacts": [{"name": a.name, "kind": a.kind, "rows": a.rows} for a in m.artifacts],
    }


def read_table(run_id: str, name: str) -> pd.DataFrame | None:
    try:
        return _store().read(run_id, name)
    except Exception as e:
        degraded(f"artifacts.read_table[{run_id}/{name}]", e)
        return None


def read_text(run_id: str, name: str) -> str | None:
    try:
        return _store().read_text(run_id, name)
    except Exception as e:
        degraded(f"artifacts.read_text[{run_id}/{name}]", e)
        return None


def read_json(run_id: str, name: str) -> dict | None:
    """读取 JSON 制品（artifacts 里以 .json 落盘）。"""
    path = _store().path_of(run_id, name)
    if path is None or not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        degraded(f"artifacts.read_json[{run_id}/{name}]", e)
        return None


# ---------------- 便捷视图：给首页/回测中心用 ----------------

_METRIC_COLS = ("cagr", "sharpe", "max_drawdown", "total_return",
                "calmar", "bench_excess_cagr")


def backtest_runs(limit: int = 30) -> pd.DataFrame:
    """回测类运行的横向对比表（一列一个指标）。"""
    df = list_runs(limit=limit)
    if df.empty:
        return df
    df = df[df["task"].astype(str).str.startswith(("backtest_", "wfa_"))]
    if df.empty:
        return df
    cols = ["run_id", "task", "created_at", "data_symbols", "data_last_date"]
    cols += [c for c in df.columns if c.startswith("m_")]
    return df[[c for c in cols if c in df.columns]]


def latest_wfa(task: str | None = None) -> dict | None:
    """最新一次 walk-forward 结果（含过拟合诊断）。"""
    store = _store()
    runs = [m for m in store.list_runs(status=STATUS_OK)
            if m.task.startswith("wfa_") and (task is None or m.task == task)]
    if not runs:
        return None
    m = runs[0]
    return {
        "run_id": m.run_id,
        "task": m.task,
        "created_at": m.created_at,
        "params": m.params,
        "decay": (m.metrics or {}).get("decay"),
        "param_stability": (m.metrics or {}).get("param_stability"),
        "n_folds": (m.metrics or {}).get("n_folds"),
        # 缺失会让"样本外无成交"的告警永远不触发——空仓时衰减比无意义，
        # 必须能告知用户，否则会误读成过拟合
        "n_folds_with_trades": (m.metrics or {}).get("n_folds_with_trades"),
        "oos_cagr": (m.metrics or {}).get("oos_cagr"),
        "oos_sharpe": (m.metrics or {}).get("oos_sharpe"),
        "oos_max_drawdown": (m.metrics or {}).get("oos_max_drawdown"),
        "fingerprint": m.fingerprint,
    }


def failed_runs(limit: int = 20) -> pd.DataFrame:
    """失败的运行——此前失败后只在日志里留痕，前端完全看不到。"""
    return _filter_status(STATUS_FAILED, limit)


# ---------------- 展示层格式化 ----------------
# 放在 api 层而非 frontend/：这些是纯函数（str/DataFrame 进出），
# 放 UI 层会无法测试（frontend 依赖 gradio）。前端组件只负责绑定。

#: 展示时优先呈现的列
_SHOW_COLS = ("run_id", "task", "created_at", "status", "data_symbols", "data_last_date")


def runs_table(limit: int = 100) -> pd.DataFrame:
    """运行列表展示表格（去 m_ 前缀、时间截断）。"""
    df = list_runs(limit=limit)
    if df.empty:
        return df
    cols = [c for c in _SHOW_COLS if c in df.columns]
    cols += [c for c in df.columns if c.startswith("m_")]
    out = df[cols].copy()
    out["created_at"] = out["created_at"].astype(str).str[:19].str.replace("T", " ")
    return out.rename(columns={c: c[2:] for c in cols if c.startswith("m_")})


def run_detail_md(run_id: str) -> str:
    """单次运行的完整上下文（Markdown）。

    回答旧机制答不了的问题：这个数字是哪次运行、用哪套参数、
    跑在哪份数据、什么代码版本下产生的。
    """
    d = get_run(run_id)
    if not d:
        return "*找不到该运行*"

    lines = [
        f"### `{d['run_id']}`",
        "",
        f"- **状态**: {d['status']}" + (f" — `{d['error']}`" if d.get("error") else ""),
        f"- **创建时间**: {str(d.get('created_at', ''))[:19].replace('T', ' ')}",
        f"- **代码版本**: `{d.get('code', 'unknown')}`",
        f"- **指纹**: `{d.get('fingerprint', '')}`",
    ]

    dv = d.get("data_version") or {}
    if dv:
        lines.append(
            f"- **数据版本**: {dv.get('symbols', '?')} 只标的，截至 {dv.get('last_date') or '?'}"
            + (f"（起始 {dv.get('first_date')}）" if dv.get("first_date") else "")
        )

    if d.get("params"):
        lines += ["", "**参数**", "```json",
                  json.dumps(d["params"], ensure_ascii=False, indent=2, default=str), "```"]

    metrics = d.get("metrics") or {}
    if metrics:
        parts = []
        for k, v in metrics.items():
            if isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, float):
                v = f"{v:.4f}"
            parts.append(f"- `{k}`: {v}")
        lines += ["", "**指标**", *parts]

    if d.get("artifacts"):
        lines += ["", "**产出**"]
        for a in d["artifacts"]:
            rows = f"，{a['rows']} 行" if a.get("rows") is not None else ""
            lines.append(f"- `{a['name']}` ({a['kind']}{rows})")

    lines += ["", "---",
              "*指纹 = hash(参数 + 数据版本 + 代码版本)。同指纹即同输入，"
              "可用于判断两次运行是否可比。*"]
    return "\n".join(lines)


def wfa_panel_md() -> str:
    """最近一次 Walk-Forward 的过拟合诊断（Markdown）。"""
    w = latest_wfa()
    if not w:
        return (
            "*暂无 Walk-Forward 结果。*\n\n"
            "运行 `uv run python scripts/walk_forward.py --strategy lowvol_indz` 后刷新。\n\n"
            "WFA 把全样本分成若干折，每折只在 train 段选参数、再在 test 段验证，"
            "用来回答「这些结论在样本外还成立吗」。"
        )

    decay, n_folds = w.get("decay"), w.get("n_folds") or 0
    n_active = w.get("n_folds_with_trades")

    lines = [f"### {w['task']} — {str(w.get('created_at', ''))[:19].replace('T', ' ')}", ""]

    if decay is None:
        lines.append("**衰减比**: 无法计算（无有效折或指标缺失）")
    else:
        verdict = (
            "✅ 参数稳健，样本外未明显衰减" if decay >= 0.8
            else "⚠️ 存在过拟合，实盘应打折预期" if decay >= 0.4
            else "❌ 严重过拟合：参数选择基本在挑噪声"
        )
        lines.append(f"**衰减比 (OOS/IS)**: `{decay:.2f}` — {verdict}")

    if n_active == 0 and n_folds:
        lines.append("\n⚠️ 全部折在样本外均无成交，此时 OOS 指标恒为 0，衰减比无意义。")
    elif n_active is not None and n_folds and n_active < n_folds:
        lines.append(
            f"\n⚠️ {n_folds - n_active}/{n_folds} 折样本外无成交"
            f"（窗口过短或过滤过严），衰减比仅基于有成交的 {n_active} 折。"
        )

    bits = []
    for key, label in (("oos_cagr", "CAGR"), ("oos_sharpe", "夏普"),
                       ("oos_max_drawdown", "最大回撤")):
        v = w.get(key)
        if v is not None:
            bits.append(f"{label} `{v:.4f}`" if isinstance(v, float) else f"{label} `{v}`")
    if bits:
        lines.append("\n**样本外合成净值**: " + " | ".join(bits))

    stab = w.get("param_stability")
    if isinstance(stab, dict) and stab:
        lines.append(
            "\n**参数一致率**: "
            + ", ".join(f"`{k}` {v:.0%}" for k, v in stab.items())
            + "  \n_1.0 = 每折选中同一组参数_"
        )

    if w.get("params"):
        lines += ["", "**运行参数**", "```json",
                  json.dumps(w["params"], ensure_ascii=False, indent=2, default=str), "```"]
    return "\n".join(lines)


def run_choices(limit: int = 100) -> list[str]:
    """运行下拉选项：`task | 时间 | run_id`。"""
    df = list_runs(limit=limit)
    if df.empty:
        return []
    return [
        f"{r['task']} | {str(r.get('created_at', ''))[:16].replace('T', ' ')} | {r['run_id']}"
        for _, r in df.iterrows()
    ]


def run_id_from_choice(choice: str) -> str:
    """从下拉选项串取回 run_id（末段）。"""
    return choice.rsplit("|", 1)[-1].strip() if choice else ""


def _filter_status(status: str, limit: int) -> pd.DataFrame:
    try:
        runs = _store().list_runs(status=status)[:limit]
    except Exception as e:
        degraded("artifacts.filter_status", e)
        return pd.DataFrame()
    return pd.DataFrame([{
        "run_id": m.run_id, "task": m.task, "created_at": m.created_at,
        "error": m.error,
    } for m in runs])


def prune(keep_last: int = 100) -> int:
    """清理旧制品，返回删除数量。"""
    try:
        return _store().prune(keep_last=keep_last)
    except Exception as e:
        degraded("artifacts.prune", e)
        return 0


def artifact_path(run_id: str, name: str):
    """制品绝对路径（供下载）；非法返回 None。"""
    if not run_id or ".." in run_id:
        return None
    return safe_path(_store().root / run_id, f"{name}.parquet")

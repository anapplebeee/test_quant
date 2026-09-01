"""策略目录单一数据源：前端所有策略下拉/描述/默认参数统一从 REGISTRY 派生。

背景（2026-08-28 审计）：首页与策略监控页曾各自硬编码 3 个策略，与后端
REGISTRY（5 个）漂移——新策略 lowvol_indz 上线后前端不可见。商用平台
（聚宽/米筐/QuantConnect）通行做法是策略清单由后端注册表驱动，前端零维护。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from common import reports_dir, safe_path, valid_date8
from quart.data.artifacts import STATUS_OK, ArtifactStore
from quart.data.calendar import TradingCalendar
from quart.strategy import REGISTRY
from quart.strategy.parameters import (
    build_factor_receipt,
    effective_strategy_params,
    encode_parameter_rows,
    strategy_parameter_rows,
)

# 展示元数据：label 用于下拉/表格，desc 用于详情。新增策略只改这里 + REGISTRY。
STRATEGY_META: dict[str, dict[str, str]] = {
    "momentum_rotation": {
        "label": "动量轮动",
        "status": "禁止实盘",
        "desc": "60日动量排名基线。项目样本中全市场动量 IC 为负且成本后显著亏损，仅保留作反例和研究基线。",
    },
    "momentum_path": {
        "label": "路径动量（研报）",
        "status": "研究",
        "desc": "RankMom/带方向 Smooth/剔除涨停日动量的独立研究入口，默认 120/20 日、低频调仓；未进入实盘白名单。",
    },
    "dual_ma": {
        "label": "双均线",
        "status": "研究",
        "desc": "短长均线交叉择时（默认已降频至5日控制成本）。教学/基线用途",
    },
    "ml_rank": {
        "label": "ML排序",
        "status": "研究",
        "desc": "Alpha158因子 + LightGBM打分选股。依赖 ml_train 产出模型",
    },
    "lowvol_composite": {
        "label": "低波复合",
        "status": "观察",
        "desc": "z(-波动率)+z(-振幅)+z(-彩票性)复合排序。全市场IC≈+0.065且七年不衰减",
    },
    "lowvol_indz": {
        "label": "低波·行业内z",
        "status": "候选",
        "desc": "行业内低波复合，采用低频调仓、分散持仓和排名缓冲控制成本。历史样本可用但尚未经过长期模拟盘验收。",
    },
}


def strategy_choices() -> list[str]:
    """前端下拉用：REGISTRY 全量（单一数据源，排序稳定）。"""
    return sorted(REGISTRY.keys())


def default_strategy_name() -> str:
    """返回配置指定的默认策略；配置漂移时安全回退注册表首项。"""
    from quart.config import load_config

    configured = str((load_config().get("strategy") or {}).get("name") or "")
    return configured if configured in REGISTRY else strategy_choices()[0]


def live_signal_choices() -> list[str]:
    """仅返回配置允许生成正式 T+1 计划的策略。"""
    from quart.config import load_config

    allowed = set((load_config().get("strategy") or {}).get("live_allowlist") or [])
    return [name for name in strategy_choices() if not allowed or name in allowed]


def get_strategy_defaults(name: str) -> dict:
    """策略默认参数：overrides.<name> > config.strategy.* > 硬编码兜底。

    与 build_strategy/resolve_params 同一优先级语义，前端预填参数与后端
    实际生效值天然一致（数据关联性）。
    """
    try:
        params = effective_strategy_params(name)
        rebalance = int(params.get("rebalance_days", 5))
        top_k = int(params.get("top_k", params.get("max_names", 10)))
    except Exception:
        rebalance, top_k = 5, 10
    return {"rebalance_days": rebalance, "top_k": top_k}


STRATEGY_PARAMETER_COLUMNS = ["参数", "值", "类型", "分类", "说明"]


def strategy_parameter_table(name: str) -> pd.DataFrame:
    """返回 schema 驱动的高级参数表，供 Gradio 动态编辑。"""
    return pd.DataFrame(strategy_parameter_rows(name), columns=STRATEGY_PARAMETER_COLUMNS)


def encode_strategy_parameter_table(name: str, table) -> list[str]:
    """校验前端参数表并编码为安全的 ``key=value`` 列表。"""
    return encode_parameter_rows(name, table)


def strategy_factor_preview(name: str, table=None) -> dict:
    """返回提交前因子公式预览；表格无效时显式抛错。"""
    overrides = None
    if table is not None:
        assignments = encode_parameter_rows(name, table)
        from quart.strategy.parameters import parse_strategy_assignments

        overrides = parse_strategy_assignments(name, assignments)
    return build_factor_receipt(name, overrides, source="preview")


def strategy_catalog() -> list[dict]:
    """策略库表数据：REGISTRY ∩ META（防 META 漏配时缺行）。"""
    allowlist = live_allowlist()
    out = []
    for name in strategy_choices():
        meta = STRATEGY_META.get(name, {})
        d = get_strategy_defaults(name)
        admitted = not allowlist or name in allowlist
        out.append(
            {
                "name": name,
                "label": meta.get("label", name),
                "status": meta.get("status", "研究"),
                "admitted": "✅ 准入" if admitted else "🔬 研究",
                "desc": meta.get("desc", ""),
                "default_rebalance": d["rebalance_days"],
                "default_top_k": d["top_k"],
            }
        )
    return out


def live_allowlist() -> list[str]:
    """正式信号策略白名单（config.strategy.live_allowlist）。"""
    from quart.config import load_config

    return list((load_config().get("strategy") or {}).get("live_allowlist") or [])


def list_signal_reports() -> list[str]:
    """可用信号日期，优先来自带 run_id 的制品仓库。"""
    dates: set[str] = set()
    try:
        for manifest in ArtifactStore().list_runs(status=STATUS_OK):
            if not manifest.task.startswith("signal_") or manifest.artifact("report") is None:
                continue
            signal_date = str((manifest.params or {}).get("signal_date") or "").replace("-", "")
            if valid_date8(signal_date):
                dates.add(signal_date)
    except Exception:
        pass
    for path in reports_dir().glob("signal_*.md"):
        signal_date = path.stem.removeprefix("signal_")
        if valid_date8(signal_date):
            dates.add(signal_date)
    return sorted(dates)


def load_signal_report(signal_date: str) -> str:
    """按信号日读取报告；前端不接触 artifacts/reports 文件系统。"""
    normalized = str(signal_date or "").replace("-", "")
    if not valid_date8(normalized):
        return "非法日期格式"
    try:
        store = ArtifactStore()
        for manifest in store.list_runs(status=STATUS_OK):
            if not manifest.task.startswith("signal_"):
                continue
            run_date = str((manifest.params or {}).get("signal_date") or "").replace("-", "")
            if run_date == normalized:
                content = store.read_text(manifest.run_id, "report")
                if content:
                    return content
    except Exception:
        pass
    path = safe_path(reports_dir(), f"signal_{normalized}.md")
    if path is not None and path.exists():
        try:
            with open(path, encoding="utf-8") as file:
                return file.read()
        except Exception as exc:
            return f"读取信号报告失败：{exc}"
    return "未找到信号报告"


def signal_snapshot() -> tuple[list[str], str | None, str]:
    dates = list_signal_reports()
    if not dates:
        return [], None, "暂无信号报告，请在操作中心生成 T+1 信号。"
    latest = dates[-1]
    return dates, latest, load_signal_report(latest)


def configured_strategy_schedule(as_of: date | None = None) -> dict:
    """当前默认策略的配置周期与交易日日历估算。"""
    from quart.config import load_config

    strategy_config = load_config().get("strategy") or {}
    name = str(strategy_config.get("name") or strategy_choices()[0])
    rebalance_days = get_strategy_defaults(name)["rebalance_days"]
    current = as_of or date.today()
    calendar = TradingCalendar.from_csv()
    next_date = current
    for _ in range(rebalance_days):
        next_date = calendar.next_after(next_date)
    return {
        "strategy": name,
        "rebalance_days": rebalance_days,
        "estimated_next_date": next_date.isoformat(),
        "calendar_cached": calendar.has_cache,
        "calendar_days": (next_date - current).days,
    }

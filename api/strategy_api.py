"""策略目录单一数据源：前端所有策略下拉/描述/默认参数统一从 REGISTRY 派生。

背景（2026-08-28 审计）：首页与策略监控页曾各自硬编码 3 个策略，与后端
REGISTRY（5 个）漂移——新策略 lowvol_indz 上线后前端不可见。商用平台
（聚宽/米筐/QuantConnect）通行做法是策略清单由后端注册表驱动，前端零维护。
"""
from __future__ import annotations

from quart.strategy import REGISTRY, build_strategy

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
        params = build_strategy(name).params
        rebalance = int(params.get("rebalance_days", 5))
        top_k = int(params.get("top_k", params.get("max_names", 10)))
    except Exception:
        rebalance, top_k = 5, 10
    return {"rebalance_days": rebalance, "top_k": top_k}


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

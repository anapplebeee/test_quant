"""策略目录单一数据源：前端所有策略下拉/描述/默认参数统一从 REGISTRY 派生。

背景（2026-08-28 审计）：首页与策略监控页曾各自硬编码 3 个策略，与后端
REGISTRY（5 个）漂移——新策略 lowvol_indz 上线后前端不可见。商用平台
（聚宽/米筐/QuantConnect）通行做法是策略清单由后端注册表驱动，前端零维护。
"""
from __future__ import annotations

from quart.config import load_config
from quart.strategy import REGISTRY

# 展示元数据：label 用于下拉/表格，desc 用于详情。新增策略只改这里 + REGISTRY。
STRATEGY_META: dict[str, dict[str, str]] = {
    "momentum_rotation": {
        "label": "动量轮动",
        "desc": "60日动量排名持Top等权，5日调仓，MA20择时熊市空仓。终审结论：全市场动量IC为负，无择时-40%/yr，不可上线",
    },
    "dual_ma": {
        "label": "双均线",
        "desc": "短长均线交叉择时（默认已降频至5日控制成本）。教学/基线用途",
    },
    "ml_rank": {
        "label": "ML排序",
        "desc": "Alpha158因子 + LightGBM打分选股。依赖 ml_train 产出模型",
    },
    "lowvol_composite": {
        "label": "低波复合",
        "desc": "z(-波动率)+z(-振幅)+z(-彩票性)复合排序。全市场IC≈+0.065且七年不衰减",
    },
    "lowvol_indz": {
        "label": "低波·行业内z",
        "desc": "低波复合分做行业内z-score（摆脱行业波动率基数差）。当前唯一成本存活的多头配置族：20-45日低频+buffer，45d CAGR +7.4%/Sharpe 0.67/MDD -22.6%（2026-08-28 验证，见 reports/rebalance_period_2026-08-28.md）",
    },
}


def strategy_choices() -> list[str]:
    """前端下拉用：REGISTRY 全量（单一数据源，排序稳定）。"""
    return sorted(REGISTRY.keys())


def get_strategy_defaults(name: str) -> dict:
    """策略默认参数：overrides.<name> > config.strategy.* > 硬编码兜底。

    与 build_strategy/resolve_params 同一优先级语义，前端预填参数与后端
    实际生效值天然一致（数据关联性）。
    """
    rebalance, top_k = 5, 10
    try:
        s = load_config().get("strategy") or {}
        rebalance = int(s.get("rebalance_days", rebalance))
        top_k = int(s.get("top_k", top_k))
        ov = (s.get("overrides") or {}).get(name) or {}
        rebalance = int(ov.get("rebalance_days", rebalance))
        top_k = int(ov.get("top_k", top_k))
    except Exception:
        pass
    return {"rebalance_days": rebalance, "top_k": top_k}


def strategy_catalog() -> list[dict]:
    """策略库表数据：REGISTRY ∩ META（防 META 漏配时缺行）。"""
    out = []
    for name in strategy_choices():
        meta = STRATEGY_META.get(name, {})
        d = get_strategy_defaults(name)
        out.append(
            {
                "name": name,
                "label": meta.get("label", name),
                "desc": meta.get("desc", ""),
                "default_rebalance": d["rebalance_days"],
                "default_top_k": d["top_k"],
            }
        )
    return out

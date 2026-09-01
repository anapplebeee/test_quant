"""策略参数表与因子执行回执。

本模块把 ``PARAMS_SCHEMA`` 变成前端、CLI 与结果审计共享的单一数据源：

* 前端不再为每个新因子硬编码控件；
* CLI 统一接收 ``key=value``，并按策略 schema 做类型与边界校验；
* 回测结果固化实际启用的因子、权重和运行期降级情况。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

CORE_PARAM_KEYS = {"top_k", "max_names", "rebalance_days"}
PROTECTED_WEB_PARAMS = {"scores_path"}

_LOWVOL_FACTORS: dict[str, tuple[str, str]] = {
    "rev_weight": ("短期反转", "20 日反转 z-score"),
    "vg_weight": ("价值成长", "PIT 质量改善、盈利增速、EP、BP"),
    "long_vol_weight": ("长窗口低波", "长窗口收益波动率反向"),
    "downside_weight": ("下行风险", "下行半方差反向"),
    "tail_weight": ("尾部损失", "尾部损失反向"),
    "amount_stability_weight": ("成交额稳定性", "成交额波动率反向"),
    "size_weight": ("小市值", "流通市值对数反向"),
    "turnover_weight": ("低换手", "20 日换手率反向"),
    "value_weight": ("价值", "正盈利股票 EP"),
    "event_crowding_weight": ("事件拥挤反向", "涨停与放量追涨拥挤反向"),
    "candidate_quality_weight": ("财报质量候选", "ROE 稳定、盈利加速、业绩超预期代理"),
}


def _registry_entry(name: str):
    from quart.strategy import REGISTRY

    if name not in REGISTRY:
        raise KeyError(f"未知策略 {name!r}，可用策略: {sorted(REGISTRY)}")
    return REGISTRY[name]


def strategy_schema(name: str) -> dict:
    """返回策略 schema 的浅拷贝。"""
    return dict(_registry_entry(name).PARAMS_SCHEMA)


def effective_strategy_params(
    name: str, overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """返回 schema 默认值 + 配置覆盖 + 显式覆盖后的完整参数。"""
    from quart.strategy import build_strategy

    schema = strategy_schema(name)
    safe_overrides = {
        key: value for key, value in dict(overrides or {}).items() if key in schema
    }
    strategy = build_strategy(name, **safe_overrides)
    params = {key: spec[1] for key, spec in schema.items()}
    params.update(strategy.params)
    return params


def _allows(types: type | tuple[type, ...], candidate: type) -> bool:
    values = types if isinstance(types, tuple) else (types,)
    return candidate in values


def _validate_bounds(key: str, value: Any) -> None:
    if value is None:
        return
    if (
        key.endswith("_weight") or key in {"rev_weight", "max_weight_pct"}
    ) and not 0 <= float(value) <= 1:
        raise ValueError(f"{key} 必须在 0 到 1 之间")
    if key in {
        "limit_breadth_quantile", "limit_breadth_floor", "limit_up_threshold",
    } and not 0 <= float(value) <= 1:
        raise ValueError(f"{key} 必须在 0 到 1 之间")
    if key in {"winsor_z", "rank_buffer", "regime_band"} and float(value) < 0:
        raise ValueError(f"{key} 不能为负数")
    if key.endswith("_days") and int(value) < 0:
        raise ValueError(f"{key} 不能为负数")
    if key in {"top_k", "max_names", "timing_levels", "liquidity_days"} and int(value) < 1:
        raise ValueError(f"{key} 必须为正整数")
    if key == "event_max_limit_hits_20d" and int(value) < 0:
        raise ValueError(f"{key} 不能为负数")


def coerce_strategy_param(name: str, key: str, raw: Any) -> Any:
    """按指定策略的 schema 将 UI/CLI 值安全转换为真实类型。"""
    cls = _registry_entry(name)
    schema = cls.PARAMS_SCHEMA
    if key not in schema:
        raise ValueError(f"策略 {name} 不支持参数 {key!r}")
    types = schema[key][0]

    if not isinstance(raw, str):
        value = raw
    else:
        text = raw.strip()
        lowered = text.lower()
        if lowered in {"null", "none", ""}:
            if not _allows(types, type(None)):
                raise ValueError(f"{key} 不允许为空")
            value = None
        elif _allows(types, bool):
            if lowered in {"true", "1", "yes", "on"}:
                value = True
            elif lowered in {"false", "0", "no", "off"}:
                value = False
            else:
                raise ValueError(f"{key} 必须为 true 或 false")
        elif _allows(types, int) and _allows(types, float):
            numeric = float(text)
            value = int(numeric) if numeric.is_integer() else numeric
        elif _allows(types, int):
            value = int(text)
        elif _allows(types, float):
            value = float(text)
        elif _allows(types, str):
            value = text
        else:
            raise ValueError(f"{key} 使用了不支持的参数类型 {types}")

    try:
        value = cls.validate_params({key: value})[key]
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    _validate_bounds(key, value)
    return value


def parse_strategy_assignments(name: str, assignments: Iterable[str]) -> dict[str, Any]:
    """解析可重复的 ``key=value`` 参数；重复键以后者为准。"""
    parsed: dict[str, Any] = {}
    for assignment in assignments:
        text = str(assignment).strip()
        if "=" not in text:
            raise ValueError(f"策略参数必须为 key=value，收到 {text!r}")
        key, raw = text.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("策略参数名不能为空")
        parsed[key] = coerce_strategy_param(name, key, raw)
    return parsed


def core_strategy_overrides(
    name: str,
    *,
    rebalance_days: int | None = None,
    top_k: int | None = None,
) -> dict[str, Any]:
    """把前端统一核心控件映射到各策略真实 schema 键。"""
    schema = strategy_schema(name)
    values: dict[str, Any] = {}
    if rebalance_days is not None and "rebalance_days" in schema:
        values["rebalance_days"] = coerce_strategy_param(
            name, "rebalance_days", rebalance_days,
        )
    if top_k is not None:
        target = "top_k" if "top_k" in schema else "max_names" if "max_names" in schema else None
        if target:
            values[target] = coerce_strategy_param(name, target, top_k)
    return values


def serialize_strategy_param(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _type_label(types: type | tuple[type, ...]) -> str:
    values = types if isinstance(types, tuple) else (types,)
    labels = {bool: "布尔", int: "整数", float: "小数", str: "文本", type(None): "空"}
    return "/".join(labels.get(item, item.__name__) for item in values)


def _category(key: str) -> str:
    if key.startswith("regime_") or key.startswith("timing_") or key.startswith("limit_breadth_"):
        return "择时与仓位"
    if key in {"max_weight_pct", "weight_mode", "rank_buffer"}:
        return "组合构造"
    if key in {"min_avg_amount", "liquidity_days", "min_price", "event_max_limit_hits_20d"}:
        return "交易过滤"
    return "因子信号"


def strategy_parameter_rows(name: str) -> list[dict[str, str]]:
    """生成前端动态高级参数表；核心参数由独立控件承载。"""
    schema = strategy_schema(name)
    effective = effective_strategy_params(name)
    rows: list[dict[str, str]] = []
    for key, spec in schema.items():
        if key in CORE_PARAM_KEYS or key in PROTECTED_WEB_PARAMS:
            continue
        rows.append({
            "参数": key,
            "值": serialize_strategy_param(effective.get(key, spec[1])),
            "类型": _type_label(spec[0]),
            "分类": _category(key),
            "说明": str(spec[2]),
        })
    return sorted(rows, key=lambda row: (row["分类"], row["参数"]))


def encode_parameter_rows(name: str, rows: Any) -> list[str]:
    """把 Gradio Dataframe 值校验并编码成 CLI assignments。"""
    if rows is None:
        return []
    if hasattr(rows, "to_dict"):
        records = rows.to_dict(orient="records")
    elif isinstance(rows, list):
        if not rows:
            return []
        if isinstance(rows[0], Mapping):
            records = rows
        else:
            records = [
                {"参数": row[0], "值": row[1]}
                for row in rows
                if isinstance(row, (list, tuple)) and len(row) >= 2
            ]
    else:
        raise ValueError("高级参数表格式无效")

    assignments: list[str] = []
    seen: set[str] = set()
    for row in records:
        key = str(row.get("参数", "")).strip()
        if not key:
            continue
        if key in seen:
            raise ValueError(f"参数 {key} 重复")
        if key in CORE_PARAM_KEYS or key in PROTECTED_WEB_PARAMS:
            raise ValueError(f"参数 {key} 不能从高级参数表修改")
        value = coerce_strategy_param(name, key, row.get("值"))
        assignments.append(f"{key}={serialize_strategy_param(value)}")
        seen.add(key)
    return assignments


def _runtime_available(strategy: Any, attr: str) -> bool | None:
    if strategy is None:
        return None
    return getattr(strategy, attr, None) is not None


def build_factor_receipt(
    name: str,
    params: Mapping[str, Any] | None = None,
    *,
    strategy: Any = None,
    source: str = "run",
) -> dict[str, Any]:
    """构建可持久化的因子执行回执。"""
    effective = effective_strategy_params(name, params)
    enabled: list[dict[str, Any]] = []
    disabled: list[dict[str, Any]] = []
    warnings: list[str] = []
    controls: dict[str, Any] = {}

    def add(label: str, key: str, value: Any, detail: str, available: bool | None = True):
        if available is False:
            status = "degraded"
            warnings.append(f"{label} 已请求，但运行期数据不可用，未进入最终打分")
        elif available is None:
            status = "configured"
        else:
            status = "active"
        enabled.append({
            "factor": label,
            "key": key,
            "value": value,
            "status": status,
            "detail": detail,
        })

    if name in {"lowvol_composite", "lowvol_indz"}:
        add("20日低波", "vol20_neg", 1.0, "z(-20日收益波动率)")
        add("20日低振幅", "amp20_neg", 1.0, "z(-20日平均振幅)")
        add("彩票性反向", "lottery20_neg", 1.0, "z(-20日最大单日收益)")
        formula_terms = ["z(-vol20)", "z(-amp20)", "z(-lottery20)"]

        for key, (label, detail) in _LOWVOL_FACTORS.items():
            weight = float(effective.get(key) or 0.0)
            requested = weight > 0
            if not requested:
                disabled.append({"factor": label, "key": key, "value": weight})
                continue
            available: bool | None = True if strategy is not None else None
            if key == "vg_weight":
                available = _runtime_available(strategy, "vg_score")
            elif key == "candidate_quality_weight":
                available = _runtime_available(strategy, "candidate_quality_score")
            elif key == "event_crowding_weight":
                available = _runtime_available(strategy, "event_crowding_score")
            elif key in {"size_weight", "turnover_weight", "value_weight"} and strategy is not None:
                try:
                    from quart.data.fundamental import load_fundamental

                    available = not load_fundamental().empty
                except (FileNotFoundError, KeyError, TypeError, ValueError):
                    available = False
            add(label, key, weight, detail, available)
            formula_terms.append(f"{weight:g}×{key}")

        if bool(effective.get("event_crowding_only", False)):
            available = _runtime_available(strategy, "event_crowding_score")
            add("仅事件拥挤选股", "event_crowding_only", True, "覆盖基础低波复合分", available)
            formula_terms = ["event_crowding_only"]
        if effective.get("event_max_limit_hits_20d") is not None:
            add(
                "涨停次数过滤",
                "event_max_limit_hits_20d",
                effective["event_max_limit_hits_20d"],
                "剔除近20日涨停次数超限股票",
                _runtime_available(strategy, "event_eligible"),
            )
        controls = {
            "industry_z": bool(effective.get("industry_z", False)),
            "weight_mode": effective.get("weight_mode", "equal"),
            "selection": effective.get("selection", "composite"),
            "rank_buffer": effective.get("rank_buffer", 0.0),
            "event_crowding_liq": bool(effective.get("event_crowding_liq", False)),
            "event_orthogonalize": bool(effective.get("event_orthogonalize", True)),
            "limit_breadth_timing": bool(effective.get("limit_breadth_timing", False)),
        }
        formula = " + ".join(formula_terms)
        if controls["industry_z"]:
            formula = f"行业内 z({formula})"
        is_factor_strategy = True
    elif name in {"momentum_rotation", "momentum_path"}:
        mode = effective.get("momentum_mode", "simple")
        lookback = int(effective.get("lookback_days", 60))
        skip = int(effective.get("momentum_skip_days", 0))
        add("路径/价格动量", "momentum_mode", mode, f"{lookback}日回看，跳过最近{skip}日")
        controls = {
            "industry_neutral": bool(effective.get("industry_neutral", False)),
            "regime_mode": effective.get("regime_mode", "ma"),
            "use_regime_filter": bool(effective.get("use_regime_filter", True)),
        }
        formula = f"momentum(mode={mode}, lookback={lookback}, skip={skip})"
        is_factor_strategy = True
    elif name == "factor_portfolio":
        factor_names = str(effective.get("factor_names", "")).split(",")
        for factor_name in filter(None, (item.strip() for item in factor_names)):
            add("研究因子", factor_name, 1.0, "横截面 z-score 后等权合成")
        controls = {
            "top_k": effective.get("top_k"),
            "max_weight_pct": effective.get("max_weight_pct"),
            "min_cash_weight": effective.get("min_cash_weight"),
            "risk_aversion": effective.get("risk_aversion"),
            "turnover_penalty": effective.get("turnover_penalty"),
            "industry_active_bound": effective.get("industry_active_bound"),
            "market_cap_active_bound": effective.get("market_cap_active_bound"),
            "style_active_bounds": effective.get("style_active_bounds"),
        }
        formula = "mean(zscore(factor_i)) → PortfolioConstructor"
        is_factor_strategy = True
    elif name == "ml_rank":
        available = True if strategy is not None and getattr(strategy, "scores", None) is not None else None
        add("ML横截面预测分", "ml_score", 1.0, "离线模型预测分数 Top-K", available)
        controls = {
            "min_score": effective.get("min_score"),
            "stale_days": effective.get("stale_days", 10),
        }
        formula = "ML prediction score"
        is_factor_strategy = True
    else:
        fast = int(effective.get("fast_days", 5))
        slow = int(effective.get("slow_days", 20))
        add("双均线趋势信号", "dual_ma", 1.0, f"MA{fast} > MA{slow}")
        controls = {"fast_days": fast, "slow_days": slow}
        formula = f"MA{fast} > MA{slow}，按强度排序"
        is_factor_strategy = False

    return {
        "strategy": name,
        "source": source,
        "is_factor_strategy": is_factor_strategy,
        "formula": formula,
        "enabled_factors": enabled,
        "disabled_factors": disabled,
        "controls": controls,
        "warnings": warnings,
        "effective_params": effective,
        "enabled_count": sum(item["status"] != "degraded" for item in enabled),
        "degraded_count": sum(item["status"] == "degraded" for item in enabled),
    }


__all__ = [
    "CORE_PARAM_KEYS",
    "PROTECTED_WEB_PARAMS",
    "build_factor_receipt",
    "coerce_strategy_param",
    "core_strategy_overrides",
    "effective_strategy_params",
    "encode_parameter_rows",
    "parse_strategy_assignments",
    "serialize_strategy_param",
    "strategy_parameter_rows",
    "strategy_schema",
]

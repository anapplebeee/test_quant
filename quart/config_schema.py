"""配置校验。

`config.py` 只做 `yaml.safe_load`，配置里少一个键、拼错一节名、数值写成
字符串，都要等到运行到某一行才以 KeyError/TypeError 的形式炸出来——
回测跑了一小时才失败。

本模块在**加载时**做一次结构校验，把错误提前到启动瞬间，并给出缺失键路径。

不引入 pydantic：项目配置结构简单且稳定，一个 60 行的校验器比新增
依赖 + 学习成本更划算。若后续配置嵌套加深再迁移。
"""
from __future__ import annotations

from typing import Any

#: 配置结构契约：键路径 → (类型或类型元组, 是否必填, 说明)
SPEC: dict[str, tuple[type | tuple[type, ...], bool, str]] = {
    "data.root": ((str,), True, "数据仓库根目录"),
    "data.adjust": ((str,), True, "复权方式 qfq/hfq/none"),
    "data.sleep_seconds": ((int, float), True, "采集间隔（防限流）"),
    "data.exclude_st": ((bool,), False, "剔除 ST"),
    "data.exclude_star": ((bool,), False, "剔除科创板"),
    "data.exclude_chinext": ((bool,), False, "剔除创业板"),
    "data.min_list_days": ((int,), False, "次新股过滤（上市不满 N 自然日）"),
    "universe.default_index": ((str,), True, "默认股票池指数"),
    "universe.mode": ((str,), False, "股票池模式 index/all/mainboard"),
    "universe.workers": ((int,), False, "数据刷新并发数（1-32）"),
    "benchmark": ((str,), True, "业绩基准指数"),
    "backtest.initial_cash": ((int, float), True, "初始资金"),
    "backtest.commission_rate": ((int, float), True, "佣金费率"),
    "backtest.commission_min": ((int, float), True, "单笔最低佣金"),
    "backtest.stamp_tax_rate": ((int, float), True, "印花税（卖出）"),
    "backtest.transfer_fee_rate": ((int, float), True, "过户费"),
    "backtest.slippage_rate": ((int, float), True, "基础滑点"),
    "backtest.impact_coef": ((int, float), False, "冲击成本系数（按 ADV 参与率）"),
    "backtest.execution_price_mode": ((str,), False, "回测成交价场景 open/vwap/close"),
    "backtest.max_adv_participation": ((int, float), False, "单笔最大 ADV 成交参与率"),
    "backtest.min_order_value": ((int, float), False, "最小委托名义额"),
    "strategy.name": ((str,), True, "默认策略名"),
    "strategy.live_allowlist": ((list,), False, "正式实盘信号白名单（须有准入台账 PASS；空=无策略准入）"),
    "strategy.paper_allowlist": ((list,), False, "Paper 模拟盘候选白名单（仅 T+1 Paper 信号，禁止实盘晋级）"),
    "strategy.top_k": ((int,), False, "持仓数量（全局默认）"),
    "strategy.lookback_days": ((int,), False, "动量回看天数（全局默认）"),
    "strategy.rebalance_days": ((int,), False, "调仓周期（全局默认）"),
    "strategy.regime_filter_days": ((int,), False, "择时均线窗口"),
    "strategy.use_regime_filter": ((bool,), False, "启用指数择时过滤"),
    "strategy.max_weight_pct": ((int, float), False, "单票权重上限"),
    "strategy.min_avg_amount": ((int, float), False, "流动性门槛（日均成交额）"),
    "strategy.liquidity_days": ((int,), False, "流动性回看窗口"),
    "strategy.min_price": ((int, float), False, "最低价过滤"),
    "strategy.overrides": ((dict,), False, "按策略覆盖参数（子键不逐项校验）"),
    "risk.max_position_pct": ((int, float), True, "单票仓位上限（实盘风控）"),
    "risk.max_daily_loss_pct": ((int, float), False, "单日亏损阈值"),
    "manual_trading.enabled": ((bool,), False, "启用手动交易 T+1 账本"),
    "manual_trading.account_name": ((str,), False, "手动交易账户名"),
    "manual_trading.database": ((str,), False, "手动交易 SQLite 路径"),
    "manual_trading.auto_migrate_holdings": ((bool,), False, "自动迁移 holdings.json"),
    "notify.dingtalk_webhook": ((str,), False, "钉钉 webhook"),
    "notify.dingtalk_secret": ((str,), False, "钉钉加签 secret"),
    "notify.wecom_webhook": ((str,), False, "企业微信群机器人 webhook"),
    "notify.wecom_secret": ((str,), False, "企业微信群机器人加签 secret"),
    "notify.wechat_pushplus_token": ((str,), False, "微信(PushPlus)个人推送 token"),
}


class ConfigError(ValueError):
    """配置结构错误。收集全部问题后一次性抛出。"""


def _get(cfg: dict, path: str) -> tuple[bool, Any]:
    node: Any = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def validate_config(cfg: dict, strict: bool = False) -> list[str]:
    """校验配置结构，返回问题列表（空列表=通过）。

    Parameters
    ----------
    strict:
        True 时把可选键也当必填。默认只校验必填键，避免给渐进演进添堵。
    """
    problems: list[str] = []
    for path, (types, required, doc) in SPEC.items():
        found, value = _get(cfg, path)
        if not found:
            if required or strict:
                problems.append(f"缺失必填配置 `{path}`（{doc}）")
            continue
        # bool 是 int 的子类：`top_k: true` 会静默通过整型校验，
        # 因此显式排除（除非该字段本来就接受 bool）
        type_tuple = types if isinstance(types, tuple) else (types,)
        is_bool = isinstance(value, bool)
        if is_bool and bool not in type_tuple:
            problems.append(
                f"配置 `{path}` 类型错误：期望 {types}，实际 bool={value!r}"
            )
            continue
        if not isinstance(value, types):
            problems.append(
                f"配置 `{path}` 类型错误：期望 {types}，"
                f"实际 {type(value).__name__}={value!r}"
            )

    # 语义校验：费率/权重类字段的取值范围
    for path in ("backtest.commission_rate", "backtest.stamp_tax_rate",
                 "backtest.transfer_fee_rate", "backtest.slippage_rate",
                 "strategy.max_weight_pct", "risk.max_position_pct"):
        found, value = _get(cfg, path)
        if found and isinstance(value, (int, float)) and not 0 <= float(value) < 1:
            problems.append(f"配置 `{path}` 应在 [0, 1) 区间，实际 {value}")

    found, adjust = _get(cfg, "data.adjust")
    if found and adjust not in ("qfq", "hfq", "", None):
        problems.append(f"配置 `data.adjust` 应为 qfq/hfq/空，实际 {adjust!r}")

    return problems


def ensure_valid(cfg: dict, strict: bool = False) -> dict:
    """校验失败时抛出 ConfigError（汇总全部问题）。"""
    problems = validate_config(cfg, strict=strict)
    if problems:
        raise ConfigError(
            "配置校验未通过:\n  - " + "\n  - ".join(problems)
        )
    return cfg


__all__ = ["SPEC", "ConfigError", "ensure_valid", "validate_config"]

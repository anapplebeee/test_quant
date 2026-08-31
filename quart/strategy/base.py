"""策略接口与参数契约。

从 `backtest/engine.py` 迁出，反转依赖方向：
    旧：strategy → backtest.engine
    新：strategy → data.market  (backtest → strategy)

参数契约
--------
`PARAMS_SCHEMA` 是**渐进式**的：子类声明后，`build_strategy` 会拒绝未知
参数名。这直接防住 README 记录过的事故——参数名拼错后静默走默认值，
导致 sweep 跑了根本没生效的参数组合。
未声明 schema 的子类保持宽松行为（便于逐步迁移）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, ClassVar

import pandas as pd

from quart.data.market import MarketData

#: 参数契约条目: (类型, 默认值, 说明)
ParamSpec = tuple[type | tuple[type, ...], Any, str]


class BaseStrategy(ABC):
    """策略基类。

    契约
    ----
    * `prepare(md)` 在每个回测/信号会话开始前调用且**只调用一次**，
      用于预计算向量化因子与状态。
    * `target_weights(i)` 在交易日 i 的**收盘**被调用，只能访问
      `i` 及之前的数据。返回：
        - `{}`      保持当前持仓
        - `{s: w}`  目标权重（会被风控层校验与归一化）
        - `{FLAT: 1.0}` 清仓
    """

    name: str = "base"

    #: 因子计算所需的最少历史交易日数。WFA 会加载这些历史，
    #: 但只把测试起始日之后的收益计入 OOS。
    required_history_days: ClassVar[int] = 0

    #: 参数契约。子类覆盖以启用严格校验。
    PARAMS_SCHEMA: ClassVar[dict[str, ParamSpec]] = {}

    def __init__(self, **params):
        self.params = self.validate_params(params)
        self._md: MarketData | None = None

    @classmethod
    def validate_params(cls, params: dict[str, Any]) -> dict[str, Any]:
        """校验参数名与类型。

        有意**不补齐** schema 里的默认值：策略代码用 `p.get(key, default)`
        读取参数，若这里再补一份默认值，就会出现两处默认值来源，
        改一处漏一处是新的事故源。schema 只负责「名称与类型」这道闸门。

        - 未声明 PARAMS_SCHEMA：原样返回（渐进迁移）
        - 已声明：拒绝未知键、按声明类型强制转换
        """
        schema = cls.PARAMS_SCHEMA
        if not schema:
            return dict(params)

        unknown = sorted(set(params) - set(schema))
        if unknown:
            raise TypeError(
                f"{cls.__name__}: 未知参数 {unknown}；"
                f"可用参数: {sorted(schema)}"
            )

        out: dict[str, Any] = {}
        for key, value in params.items():
            types = schema[key][0]
            out[key] = cls._coerce(key, value, types)
        return out

    @classmethod
    def _coerce(cls, key: str, value: Any, types: type | tuple[type, ...]) -> Any:
        if value is None:
            return None
        if isinstance(value, types) and not (types is int and isinstance(value, bool)):
            return value
        # float 字段允许传 int（yaml 里 5 会被解析成 int）
        if types is float and isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        target = types[0] if isinstance(types, tuple) else types
        try:
            return target(value)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"{cls.__name__}.{key}: 期望 {types}，"
                f"收到 {type(value).__name__}={value!r}"
            ) from exc

    def prepare(self, md: MarketData) -> None:
        """预计算钩子。子类必须调用 `super().prepare(md)` 以保存 md。"""
        self._md = md

    # ---------------- 可恢复状态 ----------------

    def state_dict(self) -> dict[str, Any]:
        """返回策略运行态；有跨日状态的子类应覆盖此方法。"""
        return {}

    def load_state_dict(self, state: Mapping[str, Any] | None) -> None:
        """恢复 ``state_dict`` 返回的状态。默认策略没有运行态。"""
        if state is not None and not isinstance(state, Mapping):
            raise TypeError(f"{type(self).__name__}.load_state_dict 需要 mapping")

    def serialize_state(self) -> dict[str, Any]:
        """返回与策略内部对象解耦的状态副本。"""
        return deepcopy(self.state_dict())

    def restore_state(self, state: Mapping[str, Any] | None) -> None:
        """``load_state_dict`` 的语义化别名。"""
        self.load_state_dict(deepcopy(state) if state is not None else None)

    def _require_md(self) -> MarketData:
        if self._md is None:
            raise RuntimeError(
                f"{type(self).__name__}.prepare(md) 未调用；"
                f"target_weights() 依赖 prepare() 预计算的因子"
            )
        return self._md

    @abstractmethod
    def target_weights(self, i: int) -> dict[str, float]:
        """返回第 i 日收盘的目标权重。"""

    @staticmethod
    def tradable_symbols(md: MarketData, i: int) -> pd.Index:
        """当日有成交（非停牌）的标的。"""
        vol = md.volumes.iloc[i]
        return vol[vol.fillna(0) > 0].index


__all__ = ["BaseStrategy", "ParamSpec"]

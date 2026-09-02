"""通用可审计风险预算层（RESEARCH-011 §6.3 落地）。

设计原则（文档原文）
--------------------
"需要新增一个可审计的通用风险预算层，而不是为某次回测手调阈值"——因此本层
的全部档位采用 RESEARCH-011 §6.3 **预注册规格**，不做逐参数扫描：

- 回撤分级降风险：组合回撤达 8% / 12% / 16% 时目标仓位降至 60% / 40% / 20%，
  恢复使用滞后（回撤收窄 4pp 才解除该档）和冷却期（5 个交易日内不升档）；
- 波动率目标：组合年化波动 12.5%（10~15% 区间中值），仅按比例降仓、不加杠杆；
- 市场状态仓位：基准趋势/市场广度/波动状态（``market_state_vector``）共同控制
  100% / 60% / 20% 三档；
- 风险开关**每日执行**，Alpha 调仓保持低频：引擎每日调用 ``target_weights``，
  Alpha 非调仓日返回 ``{}``（保持持仓），本层在非调仓日对**现有持仓**按当日
  exposure 缩放降仓，调仓日对 Alpha 新目标直接缩放，实现"每日风控 + 低频选股"；
- 所有状态只使用当时可见数据（权益高水位/档位/冷却计数随
  ``serialize_state`` 跨 WFA 折保留）。

组合方式
--------
``RiskBudgetOverlay`` 包装任意已构建的 alpha 策略（decorator）：引擎是鸭子类型
调用，包装器转发 ``prepare/sync_positions/set_portfolio_context`` 等契约，并
在 ``target_weights`` 出口统一施加风险 exposure。``min(三档)`` 取最保守。

成本/容量语义
-------------
exposure < 1 时目标权重总和 = exposure，剩余为现金（只降仓不加杠杆）；降仓在
T+1 开盘按执行引擎的正常约束（涨跌停/流动性/整手）撮合。
"""
from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd
from loguru import logger

from quart.data.market import MarketData
from quart.execution.constraints import FLAT
from quart.strategy.base import BaseStrategy

#: 回撤档位 → 目标仓位（预注册，勿逐回测手调）
DRAWDOWN_LEVELS: tuple[tuple[float, float], ...] = (
    (0.08, 0.60),
    (0.12, 0.40),
    (0.16, 0.20),
)
#: 解除档位的滞后幅度（回撤需收窄 4pp 才降档）
RECOVERY_HYSTERESIS = 0.04
#: 升档后的冷却期（交易日），期内不解除档位
COOLDOWN_DAYS = 5
#: 波动率目标（年化，10~15% 中值）与观测窗口
VOL_TARGET = 0.125
VOL_WINDOW = 20
#: market_state_vector 三态 → 目标仓位（100% / 60% / 20%）
STATE_EXPOSURE = {"risk_on": 1.0, "transition": 0.6, "risk_off": 0.2}


class RiskBudgetOverlay(BaseStrategy):
    """包装任意 alpha 策略，出口统一施加每日风险预算 exposure。"""

    name = "risk_budget_overlay"

    PARAMS_SCHEMA = {
        "vol_target": (float, VOL_TARGET, "组合年化波动率目标（仅降仓）"),
        "vol_window": (int, VOL_WINDOW, "实现波动观测窗口（交易日）"),
        "recovery_hysteresis": (float, RECOVERY_HYSTERESIS, "解除回撤档位的滞后幅度"),
        "cooldown_days": (int, COOLDOWN_DAYS, "升档后的冷却期（交易日）"),
        "risk_on_exposure": (float, 1.0, "市场 risk_on 目标仓位"),
        "transition_exposure": (float, 0.6, "市场 transition 目标仓位"),
        "risk_off_exposure": (float, 0.2, "市场 risk_off 目标仓位"),
        "enable_state": (
            bool, True,
            "是否启用市场状态维度。alpha 已自带趋势打分择时（如 R4 score）时应关闭，"
            "避免双重择时叠加（实测双重择时把 Calmar 从 0.64 拖到 0.59）",
        ),
    }

    def __init__(self, alpha: BaseStrategy, **params):
        super().__init__(**params)
        self.alpha = alpha
        self.name = f"risk_budget[{alpha.name}]"
        self.required_history_days = max(
            int(getattr(alpha, "required_history_days", 0) or 0), VOL_WINDOW + 5
        )
        # 运行态（随 serialize_state 跨 WFA 折保留）
        self._equity_peak: float | None = None
        self._equity_history: deque[float] = deque(maxlen=int(self.params.get("vol_window", VOL_WINDOW)) + 1)
        self._dd_level = 0
        self._cooldown_until_date: pd.Timestamp | None = None
        self._state_exposure: pd.Series | None = None
        self._alpha_targets: dict[str, float] = {}

    # ---------------- 生命周期转发 ----------------

    def prepare(self, md: MarketData) -> None:
        self.alpha.prepare(md)
        p = self.params
        self._vol_target = float(p.get("vol_target", VOL_TARGET))
        self._vol_window = int(p.get("vol_window", VOL_WINDOW))
        self._hysteresis = float(p.get("recovery_hysteresis", RECOVERY_HYSTERESIS))
        self._cooldown = int(p.get("cooldown_days", COOLDOWN_DAYS))
        self._enable_state = bool(p.get("enable_state", True))
        self._state_exposure = self._compute_state_exposure(md) if self._enable_state else None

    def sync_positions(self, positions: dict[str, int]) -> None:
        self.alpha.sync_positions(positions)

    def set_portfolio_context(self, context) -> None:
        self.alpha.set_portfolio_context(context)
        self._portfolio_context = context  # 降仓基数需要现有持仓权重
        if context is None:
            return
        equity = float(getattr(context, "equity", 0.0) or 0.0)
        if equity <= 0:
            return
        # 高水位与回撤档位状态机（每日收盘更新，T+1 开盘生效）
        if self._equity_peak is None:
            self._equity_peak = equity
        else:
            self._equity_peak = max(self._equity_peak, equity)
        self._equity_history.append(equity)
        self._update_drawdown_level(getattr(context, "date", None))

    # ---------------- 风险状态机 ----------------

    def _drawdown(self) -> float:
        if self._equity_peak is None or self._equity_peak <= 0:
            return 0.0
        equity = self._equity_history[-1] if self._equity_history else self._equity_peak
        return max(0.0, 1.0 - float(equity) / float(self._equity_peak))

    def _update_drawdown_level(self, date: pd.Timestamp | None) -> None:
        dd = self._drawdown()
        # 升档：_dd_level=已触发档数；逐档检查下一阈值，达到即生效（降风险不等冷却）
        while (
            self._dd_level < len(DRAWDOWN_LEVELS)
            and dd >= DRAWDOWN_LEVELS[self._dd_level][0]
        ):
            self._dd_level += 1
            if date is not None:
                self._cooldown_until_date = date + pd.Timedelta(days=self._cooldown)
            logger.debug("risk overlay 升档 {} (dd={:.2%})", self._dd_level, dd)
        # 降档：需回撤收窄越过滞后带且已过冷却期；降到 dd 仍满足的最高档
        if self._dd_level > 0 and date is not None:
            in_cooldown = self._cooldown_until_date is not None and date < self._cooldown_until_date
            if not in_cooldown:
                while (
                    self._dd_level > 0
                    and dd <= DRAWDOWN_LEVELS[self._dd_level - 1][0] - self._hysteresis
                ):
                    self._dd_level -= 1
                    logger.debug("risk overlay 降档 {} (dd={:.2%})", self._dd_level, dd)

    def _drawdown_exposure(self) -> float:
        if self._dd_level <= 0:
            return 1.0
        return DRAWDOWN_LEVELS[self._dd_level - 1][1]

    def _volatility_exposure(self) -> float:
        """实现波动超目标时按 target/realized 比例降仓（仅降仓）。"""
        if len(self._equity_history) < self._vol_window + 1:
            return 1.0  # 预热不足不干预（避免用半数据误判）
        arr = np.asarray(self._equity_history, dtype=float)
        daily_ret = np.diff(arr) / arr[:-1]
        daily_std = float(np.std(daily_ret, ddof=0))
        if not np.isfinite(daily_std) or daily_std <= 0:
            return 1.0
        realized_annual = daily_std * float(np.sqrt(252.0))
        if realized_annual <= self._vol_target:
            return 1.0
        return float(min(1.0, self._vol_target / realized_annual))

    def _compute_state_exposure(self, md: MarketData) -> pd.Series:
        """市场状态仓位（基准趋势/广度/波动 → market_state_vector 三态）。"""
        from quart.research.event_factors import market_limit_sentiment
        from quart.research.market_state import (
            RISK_OFF,
            RISK_ON,
            TRANSITION,
            market_state_vector,
        )

        p = self.params
        mapping = {
            RISK_ON: min(1.0, max(0.0, float(p.get("risk_on_exposure", 1.0)))),
            TRANSITION: min(1.0, max(0.0, float(p.get("transition_exposure", 0.6)))),
            RISK_OFF: min(1.0, max(0.0, float(p.get("risk_off_exposure", 0.2)))),
        }
        try:
            signals = market_limit_sentiment(md)
            if md.amounts is not None and not md.amounts.empty:
                signals = signals.assign(amount=md.amounts.sum(axis=1))
            elif md.volumes is not None and not md.volumes.empty:
                signals = signals.assign(amount=md.volumes.sum(axis=1))
            states = market_state_vector(signals, bench_close=md.benchmark_close)
            exposure = states["state"].map(mapping).astype("float64")
        except Exception as exc:  # 状态不可得时不叠加该维度（不静默清仓）
            logger.warning("risk overlay 市场状态不可得，退化为不启用该维度: {}", exc)
            return pd.Series(1.0, index=md.dates)
        return exposure.where(exposure.notna(), mapping[RISK_OFF])

    def _daily_exposure(self, signal_i: int) -> float:
        state = 1.0
        if self._state_exposure is not None and signal_i < len(self._state_exposure):
            state = float(self._state_exposure.iloc[signal_i])
        return min(self._drawdown_exposure(), self._volatility_exposure(), state)

    # ---------------- 出口：每日风险开关 ----------------

    def target_weights(self, i: int) -> dict[str, float]:
        raw = self.alpha.target_weights(i)
        if raw and FLAT in raw:
            self._alpha_targets = {FLAT: 1.0}
            return raw  # Alpha 决定清仓，直接执行
        if raw:
            self._alpha_targets = dict(raw)

        exposure = self._daily_exposure(i)
        if exposure >= 1.0 - 1e-9:
            return raw
        # 降仓基数：调仓日用 Alpha 新目标；非调仓日对现有持仓缩放
        base = raw if raw else dict(getattr(self, "_alpha_targets", None) or {})
        if not base and self._portfolio_context is not None:
            current = getattr(self._portfolio_context, "current_weights", None)
            if current is not None:
                base = {str(k): float(v) for k, v in current.items() if float(v) > 0}
        if not base:
            return raw
        if exposure <= 0.0:
            return {FLAT: 1.0}
        return {symbol: float(weight) * exposure for symbol, weight in base.items()}

    # ---------------- 状态序列化（WFA 跨折） ----------------

    def state_dict(self) -> dict:
        return {
            "alpha": self.alpha.serialize_state(),
            "overlay": {
                "equity_peak": self._equity_peak,
                "dd_level": self._dd_level,
                "cooldown_until": None
                if self._cooldown_until_date is None
                else self._cooldown_until_date.isoformat(),
                "equity_history": list(self._equity_history),
            },
        }

    def load_state_dict(self, state) -> None:
        if state is None:
            return
        if not isinstance(state, dict):
            raise TypeError(f"{type(self).__name__}.load_state_dict 需要 mapping")
        alpha_state = state.get("alpha")
        if alpha_state is not None:
            self.alpha.load_state_dict(alpha_state)
        overlay = state.get("overlay") or {}
        self._equity_peak = overlay.get("equity_peak")
        self._dd_level = int(overlay.get("dd_level", 0))
        cd = overlay.get("cooldown_until")
        self._cooldown_until_date = pd.Timestamp(cd) if cd else None
        self._equity_history = deque(
            overlay.get("equity_history", []),
            maxlen=int(self.params.get("vol_window", VOL_WINDOW)) + 1,
        )

    def construction_receipt(self) -> dict | None:
        return self.alpha.construction_receipt()

    @property
    def params(self):  # 暴露合并视图：引擎/报告读取策略参数
        merged = dict(getattr(self.alpha, "params", {}) or {})
        merged.update(self._params)
        return merged

    @params.setter
    def params(self, value):
        self._params = dict(value)


__all__ = ["RiskBudgetOverlay", "DRAWDOWN_LEVELS", "STATE_EXPOSURE"]

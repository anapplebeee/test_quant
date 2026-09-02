"""三层合成策略：大盘择时 × 行业内选强者 × 个股因子（RESEARCH-009 落地）。

背景
----
BigQuant 短线策略以“大盘/板块/个股”三类特征合成个股 position。RESEARCH-009
实证（横截面审计，2021-2023）修正了其中“板块动量广播”那一层在 Quart 可用口径
（统计聚类，无申万一级个股映射）下无效，真正有效的“板块/个股”横截面表达是
**行业内反转选强者**（``rel_ind_rev20``，IC≈+0.046 / ICIR≈0.40），而“选对赛道”
式板块动量应置于**择时层**而非逐股广播。

因此本策略把“三层”映射为：
- 大盘层：``market_state_vector``（涨跌停广度热度 z + 成交额 z + 基准波动率百分位
  合成 risk_on/transition/risk_off），三态 → 目标仓位档位；
- 板块层：行业内反转 ``rel_ind_rev20``（选板块内相对强者，板块中性），作为
  alpha 合成的“赛道内选股”维度；
- 个股层：低波/拥挤/价值等其余横截面因子（与板块层一起 z 合成 alpha）。

所有个股/板块层因子合成与组合优化仍交给父类 ``FactorPortfolioStrategy`` 的
Constructor 链路；本类只在调仓日把大盘择时状态作为目标仓位缩放（risk_off →
低仓/清仓）。择时关闭（默认）时行为与 ``factor_portfolio`` 完全一致，便于 A/B。

时点安全：大盘状态、因子值都只用 ≤T 数据（T 收盘信号，T+1 可执行），与平台
不变量 1 一致。大盘择时是市场级时序，**不**复制到每只股票，避免伪 IC。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.execution.constraints import FLAT
from quart.strategy.factor_portfolio import FactorPortfolioStrategy


class ThreeLayerStrategy(FactorPortfolioStrategy):
    """在 factor_portfolio 之上叠加 market_state_vector 大盘择时门控。

    父类负责：因子 z 合成 alpha → Constructor 优化建仓（个股/行业内层）。
    本类新增：调仓日按市场状态缩放目标仓位（大盘层）。

    参数（含父类全部）：
    - ``factor_names`` 默认用“行业内反转 + 低波 + 拥挤”合成；
    - ``market_timing``（默认 False）：是否启用大盘择时；关闭时等同 factor_portfolio；
    - ``risk_on_exposure`` / ``transition_exposure`` / ``risk_off_exposure``：
      risk_on / transition / risk_off 三态的目标仓位档位（0~1）；
    - 择时中间窗口参数透传 market_state_vector。
    """

    name = "three_layer"

    PARAMS_SCHEMA = {
        **FactorPortfolioStrategy.PARAMS_SCHEMA,
        "market_timing": (bool, False, "是否启用 market_state_vector 大盘择时"),
        "risk_on_exposure": (float, 1.0, "risk_on 目标仓位"),
        "transition_exposure": (float, 0.5, "transition 目标仓位"),
        "risk_off_exposure": (float, 0.0, "risk_off 目标仓位（0=清仓）"),
        "timing_min_days": (int, 5, "大盘状态最短持续天数（去抖）"),
        "timing_upper_quantile": (float, 0.66, "risk_on 判定分位（研究假设，勿在诊断段调参）"),
        "timing_lower_quantile": (float, 0.33, "risk_off 判定分位"),
    }

    def __init__(self, **params):
        super().__init__(**params)
        # 大盘择时依赖 market_limit_sentiment（内部 z_window=60）+ 去抖窗口，
        # 需要更长的预热历史；WFA/回测按 required_history_days 预载历史。
        timing = bool(self.params.get("market_timing", False))
        self.required_history_days = max(260 if timing else 61, 61)

    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        p = self.params
        self.market_timing = bool(p.get("market_timing", False))
        self.exposure_by_state = {
            "risk_on": min(1.0, max(0.0, float(p.get("risk_on_exposure", 1.0)))),
            "transition": min(1.0, max(0.0, float(p.get("transition_exposure", 0.5)))),
            "risk_off": min(1.0, max(0.0, float(p.get("risk_off_exposure", 0.0)))),
        }
        self._state_exposure: pd.Series | None = None
        if self.market_timing:
            self._state_exposure = self._compute_state_exposure(md)

    def _compute_state_exposure(self, md: MarketData) -> pd.Series:
        """预计算逐日大盘择时 exposure（risk_on/transition/risk_off → 仓位档位）。

        仅使用 ≤T 的市场数据；无 amount/benchmark 时逐级降级（缺 benchmark 只丢
        波动率项，缺 amount 用成交量代理成交额，避免整条择时失效而非静默误判）。
        """
        from quart.research.event_factors import market_limit_sentiment
        from quart.research.market_state import RISK_OFF, RISK_ON, market_state_vector

        signals = market_limit_sentiment(md)
        if md.amounts is not None and not md.amounts.empty:
            total_amount = md.amounts.sum(axis=1)
        elif md.volumes is not None and not md.volumes.empty:
            # 缺成交额时用成交量总和作为“市场量能”的退路代理（仅作量能分档）。
            total_amount = md.volumes.sum(axis=1)
        else:
            total_amount = pd.Series(np.nan, index=md.dates)
        signals = signals.assign(amount=total_amount)

        bench_close = md.benchmark_close
        states = market_state_vector(
            signals,
            bench_close=bench_close,
            min_days=int(self.params.get("timing_min_days", 5)),
            upper_quantile=float(self.params.get("timing_upper_quantile", 0.66)),
            lower_quantile=float(self.params.get("timing_lower_quantile", 0.33)),
        )
        mapping = {RISK_ON: self.exposure_by_state["risk_on"]}
        # transition / risk_off 单独映射；market_state_vector 内部只产出三种状态
        from quart.research.market_state import TRANSITION

        mapping[TRANSITION] = self.exposure_by_state["transition"]
        mapping[RISK_OFF] = self.exposure_by_state["risk_off"]
        exposure = states["state"].map(mapping).astype("float64")
        # 状态信号未就绪（预热期）一律按 risk_off 保守处理，避免半数据误判满仓
        exposure = exposure.where(exposure.notna(), self.exposure_by_state["risk_off"])
        exposure.name = "market_state_exposure"
        return exposure

    def target_weights(self, i: int) -> dict[str, float]:
        weights = super().target_weights(i)
        # 非调仓日 super 返回 {}（保持持仓）：即使市场状态已切换，也留到下次
        # 调仓再调整——避免每个交易日都被择时打断而反复清建。
        if not weights:
            return weights
        if self._state_exposure is None:
            return weights
        exposure = float(self._state_exposure.iloc[i])
        if exposure <= 0:
            return {FLAT: 1.0}
        return {sym: weight * exposure for sym, weight in weights.items()}


__all__ = ["ThreeLayerStrategy"]

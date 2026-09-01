"""多因子指数增强模板（INDEX-001 第一版）。

它继承 ``factor_portfolio`` 的 PIT 因子 Alpha 生成逻辑，但不接受临时等权基准：
每个决策日均从 ``PITExposureStore`` 取当时可用的完整指数权重和暴露快照，再强制
交给 PortfolioConstructor。缺历史快照、未来可得时间或约束数据不全都会失败。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from quart.data.exposure_store import PITExposureStore, exposure_history_path
from quart.strategy.factor_portfolio import FactorPortfolioStrategy


class IndexEnhancementStrategy(FactorPortfolioStrategy):
    """相对基准受控的多因子指数增强策略模板。"""

    name = "index_enhancement"
    PARAMS_SCHEMA = {
        **FactorPortfolioStrategy.PARAMS_SCHEMA,
        "top_k": (int, 50, "候选因子分数前 K 名"),
        "rebalance_days": (int, 20, "调仓周期（交易日）"),
        "max_weight_pct": (float, 0.03, "Constructor 单票硬上限"),
        "max_turnover": ((float, type(None)), 0.20, "Constructor 单次换手硬上限"),
        "industry_active_bound": ((float, type(None)), 0.05, "相对基准的行业主动权重上限"),
        "market_cap_active_bound": ((float, type(None)), 0.30, "相对基准的市值 log-z 主动暴露上限"),
        "benchmark_index": (str, "000300", "基准指数代码"),
        "exposure_history_path": ((str, type(None)), None, "PIT 指数暴露快照文件"),
    }

    def prepare(self, md) -> None:
        super().prepare(md)
        p = self.params
        self.benchmark_index = str(p.get("benchmark_index", "000300"))
        path_value = p.get("exposure_history_path")
        path = Path(path_value) if path_value else exposure_history_path(self.benchmark_index)
        self.exposure_store = PITExposureStore.load(self.benchmark_index, path=path)
        self._last_exposure_metadata: dict | None = None

    def _resolve_exposures(self, context, candidates: pd.Index):
        if context is None:
            raise RuntimeError("index_enhancement 必须由回测或每日信号注入 PortfolioConstructionContext")
        snapshot = self.exposure_store.snapshot_at(context.date)
        symbols = candidates.union(context.current_weights.index)
        inputs = snapshot.resolve(context.date, symbols, self.exposure_limits)
        self._last_exposure_metadata = {
            "benchmark_index": self.benchmark_index,
            "as_of": str(pd.Timestamp(snapshot.as_of).date()),
            "available_at": str(pd.Timestamp(snapshot.available_at).date()),
            "source": inputs.source,
            "version": inputs.version,
        }
        return inputs

    def construction_receipt(self) -> dict | None:
        receipt = super().construction_receipt()
        if receipt is not None:
            receipt["exposure_snapshot"] = self._last_exposure_metadata
        return receipt


__all__ = ["IndexEnhancementStrategy"]

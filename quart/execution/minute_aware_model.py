"""分钟可感知的执行模型（research 级，2026-09-02）。

改进点
------
现有 ``BacktestExecutionModel`` 的涨跌停拒单只看**开盘单点价**是否触限——
把"开盘价 == 涨停价"直接当"一字封死买不进"。两种失真：

1. 盘中封板但非一字（开盘未涨停、盘中拉板）→ 现有逻辑**漏拒**（乐观：以为能买进）；
2. 开盘一字板但盘中**开板**（有低于涨停的成交）→ 现有逻辑**误拒**（保守：本可买进）。

本模型在父类基础上，用当日分钟K线判定“当日是否**真实封死**”：
- BUY 触涨停被拒时：若当日存在某根分钟 low < 涨停价（盘中开过板、有可成交卖单）
  → **放行**（非一字封死）；
- SELL 触跌停被拒时：若当日存在某根分钟 high > 跌停价 → **放行**；
- 否则维持父类拒单（真封死）。

数据缺失/停牌当日无分钟：**fail-closed 回退父类保守拒单**（宁可多拒不误成交）。
分钟粒度默认 5 分钟（Level-2 化之前的最小可得粒度）。

适用范围与硬约束
----------------
- 智兔分钟历史仅回溯至约 **2023-06**；早于该日的执行无法用分钟增强，模型自动
  回退父类（该历史区间仍按“开盘一字”假设）。
- **独立组件，默认不替换引擎的 ``BacktestExecutionModel``**（不破坏既有回测/
  准入一致性）；研究侧按需装配（构造 engine 后替换 ``engine._model`` 或在
  独立回测中直接用）。接入主引擎需先在研究子集上验证增量并更新测试。
"""
from __future__ import annotations

from quart.data.minute_store import MinuteStore
from quart.execution.backtest_model import BacktestExecutionModel
from quart.execution.constraints import is_limit_down, is_limit_up, limit_prices


class MinuteAwareExecutionModel(BacktestExecutionModel):
    """用当日分钟K线升级涨跌停可成交判定；无分钟数据时回退父类（保守）。"""

    def __init__(
        self,
        fees=None,
        enforce_limits: bool = True,
        rule_resolver=None,
        minute_store: MinuteStore | None = None,
        minute_level: str = "5",
    ):
        super().__init__(fees, enforce_limits, rule_resolver)
        self.minute_store = minute_store or MinuteStore()
        if minute_level not in {"5", "15", "30", "60"}:
            raise ValueError(f"minute_level must be 5/15/30/60, got {minute_level!r}")
        self.minute_level = minute_level

    def blocked_reason(
        self,
        symbol: str,
        side: str,
        base_price: float,
        prev_close: float,
    ) -> str | None:
        parent = super().blocked_reason(symbol, side, base_price, prev_close)
        if parent is None:
            return None
        # 只对“开盘一字触限”这类 limit 拒单做盘中开板复核；lifecycle/规则拒单维持。
        code = str(symbol).split(".")[0]
        if self._context is None:
            return parent
        limits = limit_prices(prev_close, code)
        if limits is None:
            return parent
        upper, lower = limits
        minute = self._load_minute(code)
        if minute is None or minute.empty:
            return parent  # fail-closed：无分钟数据 → 保守维持父类拒单
        if side == "BUY" and base_price >= upper - 1e-3:
            # 涨停封单审核：当日曾出现 < 涨停价的成交（开板）则可买
            if float(minute["low"].min()) < upper:
                return None
            return parent
        if side != "BUY" and base_price <= lower + 1e-3:
            if float(minute["high"].max()) > lower:
                return None
            return parent
        return parent

    def _load_minute(self, code: str) -> object | None:
        """取执行日当日分钟 bar；无该日数据/文件不存在返回 None（保守）。"""
        if self._context is None or self._context.date is None:
            return None
        day = self._context.date.normalize()
        try:
            frame = self.minute_store.load(code, level=self.minute_level)
        except Exception:
            return None
        if frame.empty:
            return None
        same_day = frame[frame["ts"].dt.normalize() == day]
        return same_day if not same_day.empty else None


__all__ = ["MinuteAwareExecutionModel"]

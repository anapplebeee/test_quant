"""ETF 动量轮动策略（把独立脚本 quart/strategy/etf.py 正式化为平台策略）。

设计
----
- 标的池 = 一组风险 ETF（宽基/风格/商品/QDII）+ 一只防御债券 ETF（国债）。
- 信号：`score = w_s * 短窗动量 + (1-w_s) * 长窗动量`（动量 = 区间累计涨幅），
  候选需 `score > 0` 且 `收盘 > MA(ma_window)`。
- 调仓：每周第一个交易日，取动量 Top-n 等权持有；无满足条件的票则满仓防御债。
- 个股止损：持仓从开仓价回撤超过 stop_loss → 卖出转防御债（每个交易日检查）。
- 组合风控：内部参考净值回撤超过 port_dd_limit → "半仓模式"（只持 Top1）持续 half_mode_days。

平台契约
--------
继承 BaseStrategy：`prepare(md)` 预计算向量化动量/均线/周频标记；
`target_weights(i)` 在 i 收盘返回目标权重 `{symbol: w}`；非调仓且无止损动作返回 `{}`
（引擎保持当前持仓），发生止损则返回新目标权重让引擎执行调仓。
标的 symbol 直接取 md 面板里属于 `risk_etfs ∪ {defense_etf}` 的列（裸 6 位码）。

口径说明
--------
- 原 etf.py 是逐日自维护净值闭环，本策略在平台引擎里无法拿到"实际成交持仓"，
  组合回撤风控基于"目标权重 × 当日收益"的内部参考净值近似，属研究级近似，
  与实际 T+1 撮合净值略有偏差。
- 成本/滑点由回测引擎 Fees 层处理，策略不内置成本。
"""
from __future__ import annotations

import pandas as pd

from quart.data.market import MarketData
from quart.strategy.base import BaseStrategy


class ETFMomentumStrategy(BaseStrategy):
    """每周动量轮动持仓 Top-n 风险 ETF，否则持防御债；含止损与组合回撤降杠杆。"""

    name = "etf_momentum"
    required_history_days = 62

    PARAMS_SCHEMA = {
        "risk_etfs": (str, "510300,510500,159915,588000,512890,518880,159920,513500",
                      "风险 ETF 代码（逗号分隔）"),
        "defense_etf": (str, "511010", "防御债券 ETF 代码"),
        "top_n": (int, 2, "持有风险 ETF 数量（半仓模式为 1）"),
        "mom_short_days": (int, 20, "短窗动量（交易日）"),
        "mom_long_days": (int, 60, "长窗动量（交易日）"),
        "short_weight": (float, 0.6, "短窗动量在打分中的权重（长窗=1-该值）"),
        "ma_window": (int, 60, "趋势过滤均线窗口"),
        "stop_loss": (float, 0.08, "个股止损（从开仓价回撤比例）"),
        "port_dd_limit": (float, 0.12, "组合回撤触发半仓的阈值"),
        "half_mode_days": (int, 30, "半仓模式持续时间（自然日）"),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.required_history_days = self._warmup()

    def _warmup(self) -> int:
        p = self.params
        return max(
            int(p.get("mom_long_days", 60)),
            int(p.get("ma_window", 60)),
        ) + 1

    # ---------------- prepare ----------------

    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        p = self.params
        self.risk = [
            c for c in str(p.get(
                "risk_etfs", "510300,510500,159915,588000,512890,518880,159920,513500"
            )).replace("，", ",").split(",")
            if c.strip()
        ]
        self.defense = str(p.get("defense_etf", "511010")).strip()
        self.top_n = int(p.get("top_n", 2))
        self.s_short = int(p.get("mom_short_days", 20))
        self.s_long = int(p.get("mom_long_days", 60))
        self.w_short = float(p.get("short_weight", 0.6))
        self.ma_window = int(p.get("ma_window", 60))
        self.stop_loss = float(p.get("stop_loss", 0.08))
        self.dd_limit = float(p.get("port_dd_limit", 0.12))
        self.half_days = int(p.get("half_mode_days", 30))

        closes = md.close_val
        # 只保留 panel 里实际存在的风险+防御列（喂了含这些 ETF 的小 panel 才可跑）
        self.risk_in = [c for c in self.risk if c in closes.columns]
        self.defense = self.defense if self.defense in closes.columns else None
        if not self.risk_in:
            raise ValueError(
                f"ETFMomentumStrategy: md 面板缺少任何风险 ETF "
                f"({self.risk[:3]}...)，请喂含这些标的的 MarketData"
            )
        r = closes[self.risk_in]
        self.score = (
            self.w_short * (r / r.shift(self.s_short) - 1)
            + (1 - self.w_short) * (r / r.shift(self.s_long) - 1)
        )
        self.ma = closes.rolling(self.ma_window).mean()
        # 每周第一个交易日（自然周号变化），跨年 52→1 的 diff 也非 0，仍会触发
        wk = closes.index.to_series().dt.isocalendar().week
        self.weekly = wk.diff().fillna(1) != 0
        self._entry: dict[str, float] = {}
        self._ref_nav = 1.0
        self._peak = 1.0
        self._half_until: pd.Timestamp | None = None
        self._last_target: dict[str, float] = {}

    # ---------------- 每日目标 ----------------

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if i < self._warmup():
            return {}
        d = md.dates[i]
        self._update_ref_nav(i, d)
        if bool(self.weekly.iloc[i]):
            return self._rebalance(i, d)
        return self._daily_stop(i)

    def _update_ref_nav(self, i: int, d: pd.Timestamp) -> None:
        """用上一目标权重 × 当日收益滚动内部参考净值（近似组合回撤）。"""
        md = self._md
        if i < 1:
            return
        prev_row = md.close_val.iloc[i - 1]
        cur_row = md.close_val.iloc[i]
        day_ret = 0.0
        for c, w in self._last_target.items():
            if c not in cur_row.index:
                continue
            prev, cur = prev_row[c], cur_row[c]
            if pd.notna(prev) and prev > 0 and pd.notna(cur):
                day_ret += w * (cur / prev - 1)
        self._ref_nav *= 1 + day_ret
        self._peak = max(self._peak, self._ref_nav)
        if self._ref_nav / self._peak - 1 < -self.dd_limit and self._half_until is None:
            self._half_until = d + pd.Timedelta(days=self.half_days)
        if self._half_until is not None and d > self._half_until:
            self._half_until = None

    def _rebalance(self, i: int, d: pd.Timestamp) -> dict[str, float]:
        """每周首个交易日调仓。"""
        md = self._md
        half_on = self._half_until is not None and d <= self._half_until
        n = 1 if half_on else self.top_n
        row_s = self.score.iloc[i]
        row_c = md.close_val.iloc[i][self.risk_in]
        row_ma = self.ma.iloc[i][self.risk_in]
        ok_mask = (row_s > 0) & (row_c > row_ma) & (row_c.notna())
        cand = row_s[ok_mask].sort_values(ascending=False).head(n)
        picks = cand.index.tolist()

        new_w: dict[str, float] = {}
        for c in picks:
            new_w[c] = 1.0 / self.top_n
            # 仅在"新进入"或"曾清仓"时重置开仓参考价
            if c not in self._last_target or self._last_target.get(c, 0) <= 0:
                self._entry[c] = float(row_c.loc[c])
        used = sum(new_w.values())
        if self.defense is not None and used < 1.0:
            new_w[self.defense] = 1.0 - used
        self._entry = {c: v for c, v in self._entry.items() if c in new_w}
        self._last_target = new_w
        return new_w

    def _daily_stop(self, i: int) -> dict[str, float]:
        """非调仓日的止损检查：跌破开仓价的持仓转防御债（避免平台非调仓日无法减仓）。"""
        if not self._entry:
            return {}
        md = self._md
        row_c = md.close_val.iloc[i]
        out = dict(self._last_target)
        changed = False
        for c in list(self._entry.keys()):
            w = out.get(c, 0.0)
            if w <= 0 or c not in row_c.index:
                continue
            px = row_c[c]
            if pd.notna(px) and px / self._entry[c] - 1 < -self.stop_loss:
                out[c] = 0.0
                if self.defense is not None:
                    out[self.defense] = out.get(self.defense, 0.0) + w
                self._entry.pop(c, None)
                changed = True
        if not changed:
            return {}
        self._last_target = out
        return out

    # ---------------- 可恢复状态 ----------------

    def state_dict(self) -> dict:
        return {
            "entry": dict(self._entry),
            "ref_nav": float(self._ref_nav),
            "peak": float(self._peak),
            "last_target": dict(self._last_target),
            "half_until": self._half_until.isoformat() if self._half_until is not None else None,
        }

    def load_state_dict(self, state) -> None:
        super().load_state_dict(state)
        if not state:
            return
        if "entry" in state:
            self._entry = dict(state["entry"])
        if "ref_nav" in state:
            self._ref_nav = float(state["ref_nav"])
        if "peak" in state:
            self._peak = float(state["peak"])
        if "last_target" in state:
            self._last_target = dict(state["last_target"])
        if state.get("half_until"):
            self._half_until = pd.Timestamp(state["half_until"])


__all__ = ["ETFMomentumStrategy"]

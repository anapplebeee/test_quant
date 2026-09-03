"""hot_rotation.py — 热点龙头轮动策略（正式引擎 BaseStrategy 版）。

把研究端 `quart/research/ht_backtest.run()`（独立模拟器）验证过的"热门板块轮动 +
板块内选龙头"逻辑，适配成平台正式引擎 `BaseStrategy.target_weights(i)` 契约，
使其能在 BacktestEngine / 回测页真正运行。

与独立研究模拟器的差异（适配层语义）
-------------------------------------
* 研究模拟器逐日自算板块热度并月度换仓；本策略在 `prepare(md)` 里对信号面板整体
  预计算板块热度（复用 `ht_backtest.sector_heat_daily`），在 `target_weights(i)`
  里只在"每月最后交易日"返回目标龙头权重，其余日返回 {}（持仓）。
* 龙头分数：`selector='momentum'` 用板块内近 5 日收益排序（无外部依赖，可即跑）；
  `selector='ml_score'` 读 `scores_path`（如 reports/ht_ml_scores.csv，date/symbol/
  score，按月滚动 LightGBM 预生成），与 MLRankStrategy 同模式的"研究离线出分、运行
  只读分"解耦。
* 风控 `stop_loss`：对买入成本回撤超阈值的目标在次日剔除（近似硬止损）。
  引擎的风控/费用/T+1/整手由平台统一保证。

默认参数 = 实证最优配置（单板块 Top-1 + 3 票 + 10% 硬止损）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from quart.config import PROJECT_ROOT, load_config
from quart.strategy.base import BaseStrategy
from quart.research.ht_backtest import rebalance_dates, sector_heat_daily

LOT = 100


def _load_sector_map(path: str | None) -> pd.Series:
    p = Path(path or str(PROJECT_ROOT / "data" / "universe" / "stat_industry.parquet"))
    if not p.exists():
        raise FileNotFoundError(f"sector map not found: {p}")
    df = pd.read_parquet(p)
    return df.set_index("symbol")["cluster"].astype(str)


class HotRotationStrategy(BaseStrategy):
    """热门板块轮动 + 板块内选龙头。

    每个再平衡日(默认月末)选热度 Top-`hot_k` 板块；对每个板块内可购标的按
    `selector`(momentum/ml_score)排序取 `per_sector` 只，跨板块凑够 `n_leaders`；
    返回这些龙头的等权目标权重。其余交易日返回 {}（持仓不动）。
    """

    name = "hot_rotation"
    required_history_days = 25  # 板块热度需 ~20 日 warmup

    PARAMS_SCHEMA = {
        "selector": (str, "momentum", "龙头排序: momentum=板块内动量 / ml_score=外部ML分数"),
        "scores_path": ((str, type(None)), None, "ML 分数文件路径(date,symbol,score)"),
        "sector_map_path": ((str, type(None)), None, "板块映射文件(symbol->cluster)"),
        "freq": (str, "ME", "再平衡频率: ME=月末 / QE=季末 / 20D=每20交易日"),
        "hot_k": (int, 1, "热门板块数(取热度前 hot_k)"),
        "hot_rank": (int, 1, "取热度第几板块(>=1), 与 hot_k 配合用；默认第1"),
        "per_sector": (int, 3, "每板块选龙头只数(>=1)"),
        "n_leaders": (int, 3, "总目标持仓只数"),
        "stop_loss": ((float, type(None)), 0.10, "硬止损: 从成本回撤超该比例则剔出"),
        "momentum_days": (int, 5, "momentum 龙头排序回看天数"),
        "min_amount": ((int, float, type(None)), None, "可选: 当日最低成交额(元)过滤"),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.required_history_days = 25
        self._sector_map: pd.Series | None = None
        self._heat: pd.DataFrame | None = None       # (date, cluster)
        self._heat_dates: set | None = None
        self._rb_days: set | None = None
        self._scores: pd.Series | None = None        # (date,symbol)->score(已对齐 md.dates)
        self._buy_price: dict[str, float] = {}       # symbol->最近调仓成本价(止损基准)
        self._dates = pd.DatetimeIndex([])

    # ---------------- prepare：一次性预计算 ----------------
    def prepare(self, md) -> None:
        super().prepare(md)
        p = self.params
        self.sector_map = _load_sector_map(p.get("sector_map_path"))
        self.freq = str(p.get("freq", "ME"))
        self.hot_k = int(p.get("hot_k", 1))
        self.hot_rank = int(p.get("hot_rank", 1))
        self.per_sector = int(p.get("per_sector", 3))
        self.n_leaders = int(p.get("n_leaders", 3))
        self.selector = str(p.get("selector", "momentum"))
        self.stop_loss = p.get("stop_loss", 0.10)
        self.momentum_days = int(p.get("momentum_days", 5))
        self.min_amount = p.get("min_amount")
        # 尊重平台风控单票上限(否则集中 3 票 ~33%/票会被引擎削到 25% 并记 violation)
        try:
            self.max_pos_pct = float((load_config().get("risk") or {}).get("max_position_pct", 0.25))
        except Exception:
            self.max_pos_pct = 0.25

        dates = pd.DatetimeIndex(md.dates)
        self._dates = dates

        # 由宽面板构建长表 bars(date,symbol,close,amount) 供热度计算
        closes = md.closes
        amounts = md.amounts if md.amounts is not None else pd.DataFrame(0.0, index=dates, columns=closes.columns)
        closes_l = closes.stack(future_stack=True).rename("close").reset_index()
        closes_l.columns = ["date", "symbol", "close"]
        amt_l = amounts.stack(future_stack=True).rename("amount").reset_index()
        amt_l.columns = ["date", "symbol", "amount"]
        bars = closes_l.merge(amt_l, on=["date", "symbol"], how="left")
        bars["symbol"] = bars["symbol"].astype(str)
        # 仅保留有板块归属的股票
        sec = self.sector_map
        bars = bars[bars["symbol"].isin(sec.index)]
        if bars.empty:
            self._heat = pd.DataFrame(columns=["date", "cluster", "heat"])
        else:
            self._heat = sector_heat_daily(bars, sec)

        # 再平衡日集合 = 每月最后交易日
        self._rb_days = set(rebalance_dates(dates, self.freq))

        # ML 分数(可选): (date,symbol)->score
        self._scores = None
        if self.selector == "ml_score":
            path = Path(p.get("scores_path") or str(PROJECT_ROOT / "reports" / "ht_ml_scores.csv"))
            if not path.exists():
                raise FileNotFoundError(f"scores file not found: {path}; 运行 scripts/ht_train.py 生成")
            df = pd.read_csv(path)
            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = df["symbol"].astype(str)
            self._scores = df.set_index(["date", "symbol"])["score"]

    # ---------------- target_weights：每日收盘调用 ----------------
    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if md is None or self._heat is None:
            return {}
        if i < 0 or i >= len(self._dates):
            return {}
        d = self._dates[i]

        # 非再平衡日：检查止损并保持持仓
        if d not in self._rb_days:
            return self._stop_weights(d, i)

        # 再平衡日：选热门板块 + 板块内龙头
        targets = self._select_leaders(d, i)
        # 记录新目标成本(以当日 close 近似)供止损
        self._update_buy_price(targets, d, i)
        return targets

    # ---------------- 内部 ----------------
    def _row_close(self, i: int) -> pd.Series:
        md = self._md
        c = md.close_val.iloc[i]
        return c

    def _select_leaders(self, d: pd.Timestamp, i: int) -> dict[str, float]:
        md = self._md
        day_heat = self._heat[self._heat["date"] == d]
        if day_heat.empty:
            return {}
        ranked = day_heat.sort_values("heat", ascending=False)
        hot_rank_i = max(0, int(self.hot_rank) - 1)
        n_sectors = min(self.hot_k, len(ranked))
        if hot_rank_i + n_sectors > len(ranked):
            n_sectors = max(0, len(ranked) - hot_rank_i)
        if n_sectors <= 0:
            return {}
        hot_sectors = [str(ranked.iloc[hot_rank_i + k]["cluster"]) for k in range(n_sectors)]

        # 当日可购(有量/价) + 板块归属
        row_close = self._row_close(i)
        tradable = row_close[row_close.notna() & (row_close > 0)].index.astype(str)
        vol = md.volumes.iloc[i]
        has_vol = vol[vol.fillna(0) > 0].index.astype(str)
        tradable = tradable.intersection(has_vol)

        target: list[str] = []
        for hs in hot_sectors:
            members = self.sector_map[self.sector_map == hs].index.astype(str)
            cand = [s for s in tradable if s in members]
            if not cand:
                continue
            # 排序键: momentum=近 momentum_days 收益(用 close_val 前视安全: 用 iloc[i - k])；ml_score=外部分数
            key = self._rank_key(d, i)
            score = {s: key.get(s, -np.inf) for s in cand}
            score = {s: v for s, v in score.items() if v != -np.inf}
            top = sorted(score, key=score.get, reverse=True)[: int(self.per_sector)]
            for s in top:
                if s not in target:
                    target.append(s)
        # 不足 n_leaders 时在热门板块池内按分数补足
        if len(target) < int(self.n_leaders):
            pool_all = []
            for hs in hot_sectors:
                pool_all += [s for s in tradable if s in self.sector_map[self.sector_map == hs].index.astype(str)]
            key = self._rank_key(d, i)
            score = {s: key.get(s, -np.inf) for s in set(pool_all) if key.get(s, -np.inf) != -np.inf}
            for s in sorted(score, key=score.get, reverse=True):
                if len(target) >= int(self.n_leaders):
                    break
                if s not in target:
                    target.append(s)
        if not target:
            return {}
        leaders = target[: int(self.n_leaders)]
        w = min(1.0 / len(leaders), self.max_pos_pct)
        return {s: w for s in leaders}

    def _rank_key(self, d: pd.Timestamp, i: int) -> dict[str, float]:
        md = self._md
        if self.selector == "ml_score" and self._scores is not None:
            try:
                row = self._scores.loc[d]
                return {str(s): float(v) for s, v in row.items()}
            except KeyError:
                return {}
        # momentum: close_val[i] / close_val[i-k] - 1
        k = max(1, int(self.momentum_days))
        lo = max(0, i - k)
        c_now = md.close_val.iloc[i]
        c_past = md.close_val.iloc[lo]
        ret = (c_now / c_past.replace(0, np.nan) - 1.0)
        return {str(s): (float(v) if pd.notna(v) else -np.inf) for s, v in ret.items()}

    def _stop_weights(self, d: pd.Timestamp, i: int) -> dict[str, float]:
        """非再平衡日的止损：对持仓中成本回撤超 stop_loss 的目标，次日剔除。"""
        if self.stop_loss is None or not self._buy_price:
            return {}
        pos = getattr(self, "_synced_positions", {}) or {}
        if not pos:
            return {}
        row_close = self._row_close(i)
        drop = []
        for s in pos:
            if s not in self._buy_price:
                continue
            cost = self._buy_price[s]
            px = row_close.get(s)
            if cost is None or cost <= 0 or pd.isna(px) or px <= 0:
                continue
            if px <= cost * (1.0 - float(self.stop_loss)):
                drop.append(s)
        if not drop:
            return {}
        # 返回剔除这些标的的目标(其余维持) -> 触发引擎卖出被剔除项
        ret = {}
        remain = [s for s in pos if s not in drop]
        if remain:
            w = min(1.0 / len(remain), self.max_pos_pct)
            for s in remain:
                ret[s] = w
        return ret

    def _update_buy_price(self, targets: dict[str, float], d: pd.Timestamp, i: int) -> None:
        if not targets:
            return
        row_close = self._row_close(i)
        for s in targets:
            px = row_close.get(s)
            if pd.notna(px) and px > 0:
                self._buy_price[str(s)] = float(px)

    # ---------------- 可恢复状态 ----------------
    def state_dict(self) -> dict:
        return {"buy_price": dict(self._buy_price)}

    def load_state_dict(self, state) -> None:
        super().load_state_dict(state)
        if state and "buy_price" in state:
            self._buy_price = {str(k): float(v) for k, v in state["buy_price"].items()}


__all__ = ["HotRotationStrategy"]

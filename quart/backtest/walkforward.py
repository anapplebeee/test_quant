"""Walk-Forward Analysis（滚动前推验证）。

为什么它是本项目最缺的一块
--------------------------
README 里的结论链有一个结构性缺口：所有数字都来自**全样本同期优化**
——用 2020-2026 的数据选参数，再用同一段数据报收益。这是典型的
in-sample 乐观偏差来源。README 自己也承认"参数敏感度内"的波动，
但"最优参数"本身是看到了全段数据才选出来的。

WFA 把时间切成若干折，每折：
  1. 在 **train 段**做参数选择（不看未来）
  2. 用选出的参数在紧接着的 **test 段**跑，记录样本外（OOS）净值
  3. 窗口向前滚动，重复

最后把所有 test 段按复利链接成一条**完整的样本外净值曲线**。
这条曲线里的每一个收益数字，都是在"当时不知道未来"的前提下产生的。

防泄漏机制
----------
* **embargo**：train 与 test 之间留空 N 个交易日。
  日频因子（如 20 日动量、低波）在 train 末端的持仓会延续到 test 初期，
  不留 embago 会让相邻段信息"蹭"过去。
* **因子重算**：每个 fold 用 `MarketData.slice_by_pos()` 重切子面板，
  策略的 `prepare()` 在子面板上重新计算滚动窗口，因此 train 段
  不可能用到 test 段的数据。

过拟合诊断
----------
`decay` = mean(OOS metric) / mean(IS metric)：
  * 接近或大于 1.0：参数稳健，样本外未衰减
  * 0.5 左右：典型过拟合，样本内优势一半是拟合噪声
  * 接近 0 或为负：选出的参数在样本外完全无效

`param_stability`：各 fold 选中参数的一致率。
  每折都选出同一个 top_k 才是真稳健；参数来回跳说明指标在挑噪声。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Callable, Iterable, Sequence

import pandas as pd

from quart.backtest.engine import BacktestEngine
from quart.backtest.metrics import summarize
from quart.data.market import MarketData

#: 默认参数选择指标
DEFAULT_METRIC = "sharpe"

#: 允许作为选择指标的键（防止手滑写错后静默全 0 比较）
SELECTABLE_METRICS = ("sharpe", "cagr", "calmar", "total_return", "bench_excess_cagr")


@dataclass(frozen=True)
class Split:
    """一次 train/test 划分（按**位置**索引，避免日期对齐陷阱）。"""

    fold: int
    train_lo: int
    train_hi: int
    test_lo: int
    test_hi: int


@dataclass
class FoldResult:
    fold: int
    train_range: tuple[str, str]
    test_range: tuple[str, str]
    best_params: dict
    is_metrics: dict
    oos_metrics: dict
    n_candidates: int


@dataclass
class WFAResult:
    folds: list[FoldResult] = field(default_factory=list)
    oos_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    oos_summary: dict = field(default_factory=dict)
    param_grid: dict = field(default_factory=dict)
    selection_metric: str = DEFAULT_METRIC

    # ---------------- 诊断 ----------------

    @property
    def decay(self) -> float | None:
        """OOS/IS 衰减比。≈1 稳健，<0.5 疑似过拟合。

        只统计样本外有交易的折：空仓折的指标恒为 0，
        会把"没交易"污染成"衰减到 0"。
        """
        active = [f for f in self.folds if (f.oos_metrics.get("n_trades") or 0) > 0]
        if not active:
            return None
        is_vals = [f.is_metrics.get(self.selection_metric) for f in active]
        oos_vals = [f.oos_metrics.get(self.selection_metric) for f in active]
        pairs = [(i, o) for i, o in zip(is_vals, oos_vals)
                 if i is not None and o is not None]
        if not pairs:
            return None
        mean_is = sum(i for i, _ in pairs) / len(pairs)
        if abs(mean_is) < 1e-9:
            return None
        return (sum(o for _, o in pairs) / len(pairs)) / mean_is

    @property
    def _decay_all_folds(self) -> float | None:
        """未过滤空仓折的衰减比（仅用于对比展示）。"""
        is_vals = [f.is_metrics.get(self.selection_metric) for f in self.folds]
        oos_vals = [f.oos_metrics.get(self.selection_metric) for f in self.folds]
        pairs = [(i, o) for i, o in zip(is_vals, oos_vals)
                 if i is not None and o is not None]
        if not pairs:
            return None
        mean_is = sum(i for i, _ in pairs) / len(pairs)
        if abs(mean_is) < 1e-9:
            return None
        return (sum(o for _, o in pairs) / len(pairs)) / mean_is

    @property
    def n_folds_with_trades(self) -> int:
        """样本外真正产生了交易的折数。

        窗口过短/流动性门槛过高时策略可能全程空仓，此时 OOS 指标恒为 0。
        不区分这一点会把"没交易"误读成"严重过拟合"。
        """
        return sum(1 for f in self.folds if (f.oos_metrics.get("n_trades") or 0) > 0)

    @property
    def param_stability(self) -> dict[str, float]:
        """每个参数在 folds 间的一致率（选中次数 / fold 数）。

        只统计 param_grid 里出现过的参数——固定参数恒为 1.0，无信息量。
        """
        if not self.folds:
            return {}
        out: dict[str, float] = {}
        for key in self.param_grid:
            values = [f.best_params.get(key) for f in self.folds]
            if not values:
                continue
            top = max(set(values), key=values.count)
            out[key] = values.count(top) / len(values)
        return out

    def to_frame(self) -> pd.DataFrame:
        """逐折明细表（供 sweep 式展示/落盘）。"""
        rows = []
        for f in self.folds:
            rows.append({
                "fold": f.fold,
                "train": f"{f.train_range[0]}~{f.train_range[1]}",
                "test": f"{f.test_range[0]}~{f.test_range[1]}",
                **{f"best_{k}": v for k, v in f.best_params.items()},
                "is_sharpe": f.is_metrics.get("sharpe"),
                "is_cagr": f.is_metrics.get("cagr"),
                "oos_sharpe": f.oos_metrics.get("sharpe"),
                "oos_cagr": f.oos_metrics.get("cagr"),
                "oos_mdd": f.oos_metrics.get("max_drawdown"),
                "n_trades": f.oos_metrics.get("n_trades"),
            })
        return pd.DataFrame(rows)


def make_splits(
    n_days: int,
    train_days: int,
    test_days: int,
    step_days: int | None = None,
    embargo_days: int = 0,
    anchored: bool = False,
    min_train_days: int | None = None,
) -> list[Split]:
    """生成 train/test 折。

    Parameters
    ----------
    n_days:
        总交易日数。
    train_days / test_days:
        训练/测试窗口长度（交易日）。
    step_days:
        每次向前滚动多少天。默认 = test_days（首尾相接、无重叠无空隙）。
        小于 test_days 会产生重叠的 OOS 段（更平滑但样本不独立）。
    embargo_days:
        train 结束与 test 开始之间的隔离天数（防信息泄漏）。
    anchored:
        True = 锚定起点，train 段不断变长（积累更多历史，适合早期数据稀缺）；
        False = 滚动窗口，train 长度恒定（反映最近的市场状态）。
    min_train_days:
        锚定模式下 train 段的最小长度，不足则跳过该折。
    """
    if train_days <= 0 or test_days <= 0:
        raise ValueError("train_days 与 test_days 必须为正")
    if embargo_days < 0:
        raise ValueError("embargo_days 不能为负")
    step = test_days if step_days is None else step_days
    if step <= 0:
        raise ValueError("step_days 必须为正")

    min_train = train_days if min_train_days is None else min_train_days
    splits: list[Split] = []
    fold = 0
    cursor = 0 if anchored else train_days
    # 非锚定：窗口整体右移；锚定：起点固定 0，终点右移
    while True:
        if anchored:
            train_lo, train_hi = 0, cursor
        else:
            train_lo, train_hi = cursor - train_days, cursor
        if train_hi - train_lo < min_train:
            if not anchored:
                break
            cursor += step
            if cursor >= n_days:
                break
            continue

        test_lo = train_hi + embargo_days
        test_hi = test_lo + test_days
        if test_hi > n_days:
            break
        splits.append(Split(fold, train_lo, train_hi, test_lo, test_hi))
        fold += 1
        if anchored:
            cursor += step
        else:
            cursor += step
        if not anchored and cursor > n_days:
            break
    return splits


def _grid(param_grid: dict[str, Sequence[Any]]) -> list[dict]:
    """参数网格笛卡尔积。空网格返回 [{}]（纯前推、不调参）。"""
    if not param_grid:
        return [{}]
    keys = list(param_grid)
    return [dict(zip(keys, combo)) for combo in product(*(param_grid[k] for k in keys))]


def _metric_value(summary: dict, metric: str) -> float:
    v = summary.get(metric)
    return float("-inf") if v is None else float(v)


def run_walk_forward(
    md: MarketData,
    benchmark: pd.Series | None,
    strategy_name: str,
    param_grid: dict[str, Sequence[Any]] | None = None,
    base_params: dict | None = None,
    train_days: int = 504,
    test_days: int = 126,
    step_days: int | None = None,
    embargo_days: int = 5,
    anchored: bool = False,
    selection_metric: str = DEFAULT_METRIC,
    initial_cash: float = 1_000_000.0,
    fees=None,
    risk_pipeline: Callable | None = None,
    build_strategy_fn: Callable | None = None,
    min_trades: int = 0,
    progress: Callable[[str], None] | None = None,
) -> WFAResult:
    """执行 walk-forward 验证。

    Parameters
    ----------
    param_grid:
        待搜索参数 {key: [v1, v2, ...]}。留空表示不做参数选择，
        只做"固定参数的样本外滚动"（检验稳健性而非调参能力）。
    min_trades:
        候选参数在 train 段的最少成交笔数。低于此值视为"没在交易"
        （例如流动性门槛把组合清空），不参与最优评选。
    progress:
        进度回调（每折调用一次）。

    Returns
    -------
    WFAResult：含逐折明细、拼接后的样本外净值、衰减比与参数稳定性。
    """
    if selection_metric not in SELECTABLE_METRICS:
        raise ValueError(
            f"selection_metric 必须是 {SELECTABLE_METRICS} 之一，收到 {selection_metric!r}"
        )
    if build_strategy_fn is None:
        from quart.strategy import build_strategy as build_strategy_fn

    param_grid = param_grid or {}
    base_params = dict(base_params or {})
    splits = make_splits(
        len(md), train_days, test_days,
        step_days=step_days, embargo_days=embargo_days, anchored=anchored,
    )
    if not splits:
        raise ValueError(
            f"样本量不足：{len(md)} 个交易日无法切出 "
            f"train={train_days}/test={test_days}/embargo={embargo_days} 的折"
        )

    result = WFAResult(param_grid=dict(param_grid), selection_metric=selection_metric)
    segments: list[pd.Series] = []
    candidates = _grid(param_grid)

    for sp in splits:
        train_md = md.slice_by_pos(sp.train_lo, sp.train_hi)
        test_md = md.slice_by_pos(sp.test_lo, sp.test_hi)

        # ---- 样本内选参 ----
        best_score, best_params, best_summary = float("-inf"), {}, {}
        for combo in candidates:
            params = {**base_params, **combo}
            strat = build_strategy_fn(strategy_name, **params)
            engine = BacktestEngine(train_md, strat, fees=fees,
                                    initial_cash=initial_cash, risk_pipeline=risk_pipeline)
            res = engine.run_result()
            n_trades = 0 if res.trades.empty else len(res.trades)
            if n_trades < min_trades:
                continue
            summary = summarize(res.equity)
            score = _metric_value(summary, selection_metric)
            if score > best_score:
                best_score, best_params, best_summary = score, combo, summary

        if not best_params:
            # 所有候选都因 min_trades 被淘汰：退回第一组，避免整折丢失
            best_params = candidates[0] if candidates else {}
            strat = build_strategy_fn(strategy_name, **{**base_params, **best_params})
            engine = BacktestEngine(train_md, strat, fees=fees,
                                    initial_cash=initial_cash, risk_pipeline=risk_pipeline)
            best_summary = summarize(engine.run_result().equity)

        # ---- 样本外验证 ----
        strat = build_strategy_fn(strategy_name, **{**base_params, **best_params})
        engine = BacktestEngine(test_md, strat, fees=fees,
                                initial_cash=initial_cash, risk_pipeline=risk_pipeline)
        oos = engine.run_result()
        bench_slice = (
            benchmark.iloc[sp.test_lo:sp.test_hi] if benchmark is not None else None
        )
        oos_summary = summarize(oos.equity, benchmark=bench_slice)
        oos_summary["n_trades"] = 0 if oos.trades.empty else len(oos.trades)

        fold = FoldResult(
            fold=sp.fold,
            train_range=(
                str(train_md.dates[0].date()), str(train_md.dates[-1].date()),
            ),
            test_range=(
                str(test_md.dates[0].date()), str(test_md.dates[-1].date()),
            ),
            best_params=dict(best_params),
            is_metrics=dict(best_summary),
            oos_metrics=oos_summary,
            n_candidates=len(candidates),
        )
        result.folds.append(fold)

        # 各折净值归一化后按复利链接成一条连续曲线
        eq = oos.equity.dropna()
        if len(eq) >= 2 and eq.iloc[0] > 0:
            segments.append(eq / eq.iloc[0])

        if progress:
            progress(
                f"fold {sp.fold}: train {fold.train_range[0]}~{fold.train_range[1]} "
                f"-> test {fold.test_range[0]}~{fold.test_range[1]} | "
                f"{selection_metric} IS={best_summary.get(selection_metric, 0):.2f} "
                f"OOS={oos_summary.get(selection_metric, 0):.2f} | {best_params}"
            )

    result.oos_equity = _link_segments(segments)
    if len(result.oos_equity) >= 2:
        bench_all = (
            benchmark.iloc[: len(result.oos_equity)] if benchmark is not None else None
        )
        result.oos_summary = summarize(result.oos_equity, benchmark=bench_all)
    return result


def _link_segments(segments: Iterable[pd.Series]) -> pd.Series:
    """把各折的归一化净值段按复利链接成一条曲线。

    段之间可能有重叠（step_days < test_days）或空隙。重叠处保留后一段
    （更新的参数决策），空隙保持为曲线的跳变——真实交易中窗口之间
    本来就可能空仓。
    """
    if not segments:
        return pd.Series(dtype=float, name="equity")
    level = 1.0
    parts: list[pd.Series] = []
    for seg in segments:
        parts.append(seg * level)
        level = float(seg.iloc[-1]) * level
    combined = pd.concat(parts)
    if combined.index.has_duplicates:
        combined = combined[~combined.index.duplicated(keep="last")]
    combined = combined.sort_index()
    combined.name = "equity"
    return combined


__all__ = [
    "SELECTABLE_METRICS",
    "FoldResult",
    "Split",
    "WFAResult",
    "make_splits",
    "run_walk_forward",
]

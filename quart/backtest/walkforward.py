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
* **PIT 上下文**：每个 fold 的策略只在 `[test_lo-history, test_hi)`
  上 prepare，滚动窗口能在测试首日使用测试日前历史，但不会看到测试结束日之后的数据。
* **连续账户**：默认将各 fold 的唯一测试日期调度到同一个引擎，现金、持仓、
  待执行调仓和策略状态跨 fold 延续；`account_mode="independent"` 保留每折独立诊断口径。

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

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import product
from typing import Any

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
    warmup_days: int = 0
    account_mode: str = "continuous"


@dataclass
class WFAResult:
    folds: list[FoldResult] = field(default_factory=list)
    oos_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    oos_summary: dict = field(default_factory=dict)
    param_grid: dict = field(default_factory=dict)
    selection_metric: str = DEFAULT_METRIC
    account_mode: str = "continuous"

    # ---------------- 诊断 ----------------

    @property
    def decay(self) -> float | None:
        """OOS/IS 衰减比。≈1 稳健，<0.5 疑似过拟合。

        只统计样本外有交易的折：空仓折的指标恒为 0，
        会把"没交易"污染成"衰减到 0"。样本内指标均值非正时，
        比值没有“衰减”含义，返回 ``None``，避免负/负比值被误判为稳健。
        """
        active = [f for f in self.folds if (f.oos_metrics.get("n_trades") or 0) > 0]
        if not active:
            return None
        is_vals = [f.is_metrics.get(self.selection_metric) for f in active]
        oos_vals = [f.oos_metrics.get(self.selection_metric) for f in active]
        pairs = [(i, o) for i, o in zip(is_vals, oos_vals, strict=True)
                 if i is not None and o is not None]
        if not pairs:
            return None
        mean_is = sum(i for i, _ in pairs) / len(pairs)
        if mean_is <= 1e-9:
            return None
        return (sum(o for _, o in pairs) / len(pairs)) / mean_is

    @property
    def _decay_all_folds(self) -> float | None:
        """未过滤空仓折的衰减比（仅用于对比展示）。"""
        is_vals = [f.is_metrics.get(self.selection_metric) for f in self.folds]
        oos_vals = [f.oos_metrics.get(self.selection_metric) for f in self.folds]
        pairs = [(i, o) for i, o in zip(is_vals, oos_vals, strict=True)
                 if i is not None and o is not None]
        if not pairs:
            return None
        mean_is = sum(i for i, _ in pairs) / len(pairs)
        if mean_is <= 1e-9:
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
                "warmup_days": f.warmup_days,
            })
        return pd.DataFrame(rows)


def _required_history_days(strategy: Any) -> int:
    """读取策略声明的历史需求，兼容属性和无参方法两种写法。"""
    value = getattr(strategy, "required_history_days", 0)
    if callable(value):
        value = value()
    try:
        return max(0, int(value))
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"{type(strategy).__name__}.required_history_days 必须是非负整数"
        ) from exc


class _ScheduledStrategy:
    """将已在各自历史上下文中准备好的策略拼成一个连续 OOS 策略。

    ``owner`` 为全局交易日位置到 fold 的唯一映射，重叠 test 窗口后写入的
    fold 覆盖先写入的 fold，因此同一日期只会调用一次策略、只会产生一次收益。
    """

    name = "walk_forward_continuous"
    params: dict = {}

    def __init__(self, execution_start: int, owner: dict[int, int], prepared: list[dict]):
        self.execution_start = int(execution_start)
        self.owner = owner
        self.prepared = prepared
        self._last_owner: int | None = None

    def prepare(self, md: MarketData) -> None:
        # 子策略已经用各自的 context_md prepare；这里仅满足 BacktestEngine 接口。
        return None

    def target_weights(self, i: int) -> dict[str, float]:
        global_i = self.execution_start + int(i)
        fold_idx = self.owner.get(global_i)
        if fold_idx is None:
            return {}
        item = self.prepared[fold_idx]
        if self._last_owner != fold_idx:
            if self._last_owner is not None:
                previous = self.prepared[self._last_owner]
                state = previous["strategy"].serialize_state()
                state = _translate_state_indices(
                    state,
                    int(previous["context_lo"]),
                    int(item["context_lo"]),
                )
                item["strategy"].restore_state(state)
            self._last_owner = fold_idx
        local_i = global_i - int(item["context_lo"])
        return item["strategy"].target_weights(local_i)

    def serialize_state(self) -> dict:
        return {
            str(i): strategy.serialize_state()
            for i, strategy in enumerate(item["strategy"] for item in self.prepared)
        }


def _translate_state_indices(state: dict, from_context_lo: int, to_context_lo: int) -> dict:
    """把内置策略的本地调仓索引转换到新上下文的本地坐标。"""
    out = dict(state)
    if "next_rebalance" in out:
        out["next_rebalance"] = int(out["next_rebalance"]) + from_context_lo - to_context_lo
    return out


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
    return [dict(zip(keys, combo, strict=True)) for combo in product(*(param_grid[k] for k in keys))]


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
    account_mode: str = "continuous",
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
    account_mode:
        ``"continuous"``（默认）在所有唯一 OOS 日期上运行一个连续账户；
        ``"independent"`` 每折从初始现金独立运行，适合诊断单折表现。
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
    if account_mode not in ("continuous", "independent"):
        raise ValueError("account_mode 必须是 'continuous' 或 'independent'")
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

    result = WFAResult(
        param_grid=dict(param_grid),
        selection_metric=selection_metric,
        account_mode=account_mode,
    )
    candidates = _grid(param_grid)
    records: list[dict[str, Any]] = []

    for sp in splits:
        train_md = md.slice_by_pos(sp.train_lo, sp.train_hi)

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

        # 每折测试开始前加载策略声明的历史上下文。上下文只用于计算因子，
        # 执行引擎仍从 test_lo 开始，因此 warmup 收益不会污染 OOS。
        selected = build_strategy_fn(strategy_name, **{**base_params, **best_params})
        warmup_days = _required_history_days(selected)
        context_lo = max(0, sp.test_lo - warmup_days)
        records.append({
            "split": sp,
            "train_md": train_md,
            "test_md": md.slice_by_pos(sp.test_lo, sp.test_hi),
            "best_params": dict(best_params),
            "is_metrics": dict(best_summary),
            "warmup_days": warmup_days,
            "context_lo": context_lo,
            "n_candidates": len(candidates),
            "account_mode": account_mode,
        })

    if account_mode == "independent":
        segments: list[pd.Series] = []
        for rec in records:
            sp = rec["split"]
            test_md = rec["test_md"]
            context_md = md.slice_by_pos(rec["context_lo"], sp.test_hi)
            strat = build_strategy_fn(
                strategy_name, **{**base_params, **rec["best_params"]}
            )
            engine = BacktestEngine(
                test_md, strat, fees=fees, initial_cash=initial_cash,
                risk_pipeline=risk_pipeline, signal_md=context_md,
                signal_offset=sp.test_lo - rec["context_lo"],
            )
            oos = engine.run_result()
            bench_slice = benchmark.iloc[sp.test_lo:sp.test_hi] if benchmark is not None else None
            oos_summary = summarize(oos.equity, benchmark=bench_slice)
            oos_summary["n_trades"] = 0 if oos.trades.empty else len(oos.trades)
            fold = _make_fold_result(rec, oos_summary)
            result.folds.append(fold)
            eq = oos.equity.dropna()
            if len(eq) >= 2 and eq.iloc[0] > 0:
                segments.append(eq / eq.iloc[0])
            if progress:
                _report_fold(progress, fold, selection_metric)
        result.oos_equity = _link_segments(segments)
    else:
        # 连续账户：每个策略只在自己的 PIT 上下文中 prepare，
        # 然后按唯一 OOS 日期调度到同一个 BacktestEngine，现金/持仓不重置。
        owner: dict[int, int] = {}
        prepared: list[dict[str, Any]] = []
        for j, rec in enumerate(records):
            sp = rec["split"]
            context_md = md.slice_by_pos(rec["context_lo"], sp.test_hi)
            strat = build_strategy_fn(
                strategy_name, **{**base_params, **rec["best_params"]}
            )
            strat.prepare(context_md)
            prepared.append({"strategy": strat, "context_lo": rec["context_lo"]})
            # 后出现的 fold 覆盖重叠日期，保证每个 OOS 日期只有一个归属。
            for global_i in range(sp.test_lo, sp.test_hi):
                owner[global_i] = j

        if owner:
            oos_lo, oos_hi = min(owner), max(owner) + 1
            exec_md = md.slice_by_pos(oos_lo, oos_hi)
            scheduled = _ScheduledStrategy(oos_lo, owner, prepared)
            engine = BacktestEngine(
                exec_md, scheduled, fees=fees,
                initial_cash=initial_cash, risk_pipeline=risk_pipeline,
            )
            continuous = engine.run_result()
            oos_dates = md.dates[sorted(owner)]
            result.oos_equity = continuous.equity.reindex(oos_dates).dropna()
        else:
            continuous = None
            result.oos_equity = pd.Series(dtype=float, name="equity")

        for j, rec in enumerate(records):
            sp = rec["split"]
            owned_idx = sorted(i for i, owner_j in owner.items() if owner_j == j)
            dates = md.dates[owned_idx]
            fold_eq = result.oos_equity.reindex(dates).dropna()
            bench_slice = benchmark.iloc[owned_idx] if benchmark is not None else None
            oos_summary = summarize(fold_eq, benchmark=bench_slice)
            if continuous is None or continuous.trades.empty:
                n_trades = 0
            else:
                n_trades = int(continuous.trades["date"].isin(set(dates)).sum())
            oos_summary["n_trades"] = n_trades
            fold = _make_fold_result(rec, oos_summary)
            result.folds.append(fold)
            if progress:
                _report_fold(progress, fold, selection_metric)

    if len(result.oos_equity) >= 2:
        bench_all = benchmark.reindex(result.oos_equity.index) if benchmark is not None else None
        result.oos_summary = summarize(result.oos_equity, benchmark=bench_all)
    return result


def _make_fold_result(rec: dict[str, Any], oos_summary: dict) -> FoldResult:
    sp = rec["split"]
    train_md = rec["train_md"]
    test_md = rec["test_md"]
    return FoldResult(
        fold=sp.fold,
        train_range=(str(train_md.dates[0].date()), str(train_md.dates[-1].date())),
        test_range=(str(test_md.dates[0].date()), str(test_md.dates[-1].date())),
        best_params=dict(rec["best_params"]),
        is_metrics=dict(rec["is_metrics"]),
        oos_metrics=oos_summary,
        n_candidates=int(rec.get("n_candidates", 0)),
        warmup_days=int(rec["warmup_days"]),
        account_mode=rec.get("account_mode", "continuous"),
    )


def _report_fold(progress: Callable[[str], None], fold: FoldResult, metric: str) -> None:
    progress(
        f"fold {fold.fold}: train {fold.train_range[0]}~{fold.train_range[1]} "
        f"-> test {fold.test_range[0]}~{fold.test_range[1]} | "
        f"{metric} IS={fold.is_metrics.get(metric, 0):.2f} "
        f"OOS={fold.oos_metrics.get(metric, 0):.2f} | {fold.best_params}"
    )


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

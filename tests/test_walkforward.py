"""Walk-Forward 测试。

核心不变量：**样本外收益不能用到未来信息**。
如果 WFA 实现有泄漏（比如切错位置、embargo 失效），
一个"在合成数据上必然亏损"的策略会伪装出正收益——这是最难发现的一类 bug。
"""
from __future__ import annotations

from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from quart.backtest.walkforward import (
    SELECTABLE_METRICS,
    WFAResult,
    _link_segments,
    make_splits,
    run_walk_forward,
)
from quart.data.market import MarketData
from quart.execution.fees import Fees
from quart.strategy.base import BaseStrategy


# ---------------------------------------------------------------- 切分


def test_splits_are_contiguous_and_ordered():
    sp = make_splits(n_days=1000, train_days=300, test_days=100)
    assert len(sp) == 7  # (1000-300)/100
    for a, b in pairwise(sp):
        assert b.train_lo > a.train_lo, "窗口未向前滚动"
        assert a.test_hi == b.test_lo, "test 段应首尾相接"
    assert sp[0].train_lo == 0
    assert sp[-1].test_hi == 1000


def test_embargo_gap_between_train_and_test():
    sp = make_splits(n_days=1000, train_days=300, test_days=100, embargo_days=10)
    for s in sp:
        assert s.test_lo - s.train_hi == 10, "embargo 未生效，存在信息泄漏"


def test_anchored_mode_extends_train_window():
    sp = make_splits(n_days=1000, train_days=300, test_days=100, anchored=True)
    for s in sp:
        assert s.train_lo == 0, "锚定模式 train 起点应固定"
    assert sp[-1].train_hi > sp[0].train_hi, "锚定模式 train 应逐折变长"


def test_step_shorter_than_test_creates_overlap():
    sp = make_splits(n_days=1000, train_days=300, test_days=100, step_days=50)
    assert sp[1].test_lo < sp[0].test_hi, "step < test 应产生重叠"


def test_insufficient_sample_raises():
    with pytest.raises(ValueError, match="样本量不足"):
        run_walk_forward(
            _md(n=50), None, "dummy", train_days=504, test_days=126
        )


def test_invalid_metric_rejected():
    with pytest.raises(ValueError, match="selection_metric"):
        run_walk_forward(_md(n=400), None, "dummy", selection_metric="win_rate")
    assert "win_rate" not in SELECTABLE_METRICS


def test_bad_window_sizes_rejected():
    with pytest.raises(ValueError):
        make_splits(1000, train_days=0, test_days=10)
    with pytest.raises(ValueError):
        make_splits(1000, train_days=10, test_days=10, embargo_days=-1)
    with pytest.raises(ValueError, match="warmup_days"):
        run_walk_forward(
            _md(n=100),
            None,
            "dummy",
            train_days=50,
            test_days=20,
            warmup_days=-1,
        )


# ---------------------------------------------------------------- 段链接


def test_link_segments_compounds_levels():
    s1 = pd.Series([1.0, 1.1], index=pd.date_range("2024-01-01", periods=2))
    s2 = pd.Series([1.0, 0.5], index=pd.date_range("2024-01-03", periods=2))
    out = _link_segments([s1, s2])
    # 第二段起点应承接第一段终点 1.1
    assert out.iloc[0] == pytest.approx(1.0)
    assert out.iloc[1] == pytest.approx(1.1)
    assert out.iloc[2] == pytest.approx(1.1)
    assert out.iloc[-1] == pytest.approx(0.55)


def test_link_segments_dedupes_overlap_keeping_latest():
    idx = pd.date_range("2024-01-01", periods=3)
    s1 = pd.Series([1.0, 1.0, 1.0], index=idx)
    s2 = pd.Series([1.0, 2.0], index=idx[1:])
    out = _link_segments([s1, s2])
    assert not out.index.has_duplicates
    assert out.iloc[-1] == pytest.approx(2.0), "重叠处应保留后一段"


def test_link_empty():
    assert _link_segments([]).empty


# ---------------------------------------------------------------- 泄漏防线


class _Declining(BaseStrategy):
    """每调仓日全仓持有跌幅最大的那只——在合成数据上必然亏损。

    用来验证 WFA 不会把亏损策略"洗"成正收益。
    """

    name = "declining"

    def prepare(self, md):
        super().prepare(md)
        self.period = int(self.params.get("period", 5))
        self._next = self.period

    def target_weights(self, i):
        md = self._require_md()
        if i < self.period or i < self._next:
            return {}
        self._next = i + self.period
        mom = md.closes.pct_change(self.period).iloc[i].dropna()
        if mom.empty:
            return {}
        # 专挑最差的一只
        return {mom.idxmin(): 1.0}


class _HistorySignal(BaseStrategy):
    """只有观察到足够历史后才在每折首个测试日发出信号。"""

    name = "history_signal"
    required_history_days = 5

    def prepare(self, md):
        super().prepare(md)
        self.seen = 0

    def target_weights(self, i):
        self.seen = max(self.seen, i)
        if i < self.required_history_days:
            return {}
        # 单次信号足以验证测试首日的上下文确实被加载；引擎在下一交易日执行。
        if i == self.required_history_days:
            return {"S00": 1.0}
        return {}


class _NeedsHistory(BaseStrategy):
    name = "needs_history"
    required_history_days = 80

    def prepare(self, md):
        super().prepare(md)

    def target_weights(self, i):
        # 声明 required_history_days 后，WFA 会为测试段前置该窗口的历史上下文，
        # 策略在测试首日就能看到足够历史并出信号
        if i < self.required_history_days:
            return {}
        return {self._md.symbols[0]: 1.0}


def _md(n: int = 400, n_syms: int = 8, seed: int = 3) -> MarketData:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2022-01-03", periods=n, freq="B")
    rets = pd.DataFrame(
        rng.normal(0.0004, 0.02, size=(n, n_syms)),
        index=dates, columns=[f"S{i:02d}" for i in range(n_syms)],
    )
    closes = (1 + rets).cumprod() * 10
    opens = closes.shift(1).fillna(closes.iloc[0])
    bars = pd.DataFrame({
        "date": np.repeat(dates, n_syms),
        "symbol": np.tile(closes.columns.values, n),
        "open": opens.to_numpy().ravel(),
        "high": np.maximum(opens, closes).to_numpy().ravel() * 1.01,
        "low": np.minimum(opens, closes).to_numpy().ravel() * 0.99,
        "close": closes.to_numpy().ravel(),
        "volume": 1e7,
        "amount": 1e8,
    })
    return MarketData.from_bars(bars)


def test_no_lookahead_worst_pick_stays_bad_out_of_sample():
    """泄漏检测：故意选最差标的，OOS 必须仍是负的。

    若实现有位置切错/embargo 失效，未来信息会渗进来，
    这个"必亏"策略就会伪装出正收益。
    """
    md = _md(n=500)
    result = run_walk_forward(
        md, None, "declining",
        param_grid={"period": [5, 10]},
        train_days=200, test_days=50, embargo_days=5,
        initial_cash=1_000_000.0,
        fees=Fees.zero(),
        build_strategy_fn=lambda name, **kw: _Declining(**kw),
    )
    assert len(result.folds) >= 3
    # 等权基准（全样本买入持有）应显著优于"专挑最差"
    bench = md.closes.mean(axis=1)
    bench_ret = float(bench.iloc[-1] / bench.iloc[0] - 1)
    oos_ret = float(result.oos_equity.iloc[-1] / result.oos_equity.iloc[0] - 1)
    assert oos_ret < bench_ret, (
        f"疑似未来函数泄漏：挑最差标的的 OOS({oos_ret:.2%}) "
        f"未低于等权基准({bench_ret:.2%})"
    )


def test_oos_equity_is_continuous_and_monotonic_in_time():
    md = _md(n=400)
    result = run_walk_forward(
        md, None, "declining",
        train_days=200, test_days=50, embargo_days=5,
        fees=Fees.zero(),
        build_strategy_fn=lambda name, **kw: _Declining(**kw),
    )
    eq = result.oos_equity
    assert eq.index.is_monotonic_increasing
    assert not eq.index.has_duplicates
    assert (eq > 0).all(), "净值曲线不应出现非正值"


def test_oos_uses_pre_test_history_as_warmup():
    # 每折独立账户：test 段前会加载 required_history_days 的历史上下文，
    # 策略在测试首日（local_i == required_history_days）就能出信号。
    result = run_walk_forward(
        _md(n=400),
        None,
        "needs_history",
        train_days=120,
        test_days=60,
        embargo_days=5,
        account_mode="independent",
        fees=Fees.zero(),
        build_strategy_fn=lambda name, **kwargs: _NeedsHistory(**kwargs),
    )

    assert result.n_folds_with_trades == len(result.folds), (
        f"测试段未使用前置历史：{result.n_folds_with_trades}/{len(result.folds)} 折有交易"
    )


def test_folds_cover_disjoint_test_windows():
    md = _md(n=400)
    result = run_walk_forward(
        md, None, "declining",
        train_days=200, test_days=50, embargo_days=0,
        fees=Fees.zero(),
        build_strategy_fn=lambda name, **kw: _Declining(**kw),
    )
    ranges = [f.test_range for f in result.folds]
    for a, b in pairwise(ranges):
        assert a[1] < b[0] or a[1] == b[0], "test 窗口不应乱序"


def test_wfa_loads_history_before_oos_without_counting_warmup_returns():
    md = _md(n=160, n_syms=3)
    result = run_walk_forward(
        md, None, "history_signal", train_days=80, test_days=20,
        embargo_days=0, fees=Fees.zero(),
        build_strategy_fn=lambda name, **kw: _HistorySignal(**kw),
        account_mode="independent",
    )
    assert result.folds
    assert all(f.warmup_days == 5 for f in result.folds)
    # 每折的测试区间仍从 test_lo 开始，而不是从 warmup context 开始。
    assert result.folds[0].test_range[0] == str(md.dates[80].date())
    assert result.folds[0].oos_metrics["n_trades"] == 1


def test_continuous_wfa_dedupes_overlapping_oos_dates_and_keeps_one_account():
    md = _md(n=180, n_syms=3)
    result = run_walk_forward(
        md, None, "history_signal", train_days=80, test_days=30,
        step_days=10, embargo_days=0, fees=Fees.zero(),
        build_strategy_fn=lambda name, **kw: _HistorySignal(**kw),
    )
    assert result.account_mode == "continuous"
    assert not result.oos_equity.index.has_duplicates
    expected = len({i for sp in make_splits(180, 80, 30, step_days=10)
                     for i in range(sp.test_lo, sp.test_hi)})
    assert len(result.oos_equity) == expected


# ---------------------------------------------------------------- 诊断


def test_decay_and_stability_on_synthetic_result():
    r = WFAResult(selection_metric="sharpe", param_grid={"top_k": [10, 20]})
    r.folds = [
        type("F", (), {})() for _ in range(4)
    ]
    # 用真实结构构造
    from quart.backtest.walkforward import FoldResult

    # 每折都带 n_trades > 0：无成交的折会被衰减比排除（见下一条测试）
    r.folds = [
        FoldResult(0, ("a", "b"), ("c", "d"), {"top_k": 10},
                   {"sharpe": 1.0}, {"sharpe": 0.8, "n_trades": 10}, 2),
        FoldResult(1, ("a", "b"), ("c", "d"), {"top_k": 10},
                   {"sharpe": 1.0}, {"sharpe": 0.6, "n_trades": 8}, 2),
        FoldResult(2, ("a", "b"), ("c", "d"), {"top_k": 20},
                   {"sharpe": 1.0}, {"sharpe": 1.0, "n_trades": 12}, 2),
        FoldResult(3, ("a", "b"), ("c", "d"), {"top_k": 10},
                   {"sharpe": 1.0}, {"sharpe": 0.6, "n_trades": 9}, 2),
    ]
    assert r.decay == pytest.approx(0.75)      # (0.8+0.6+1.0+0.6)/4
    assert r.param_stability["top_k"] == pytest.approx(0.75)  # 10 出现 3/4


def test_decay_none_when_no_folds():
    assert WFAResult().decay is None


def test_decay_none_when_in_sample_metric_is_nonpositive():
    """IS 指标均值非正时，负/负比值不应伪装成稳健。"""
    from quart.backtest.walkforward import FoldResult

    r = WFAResult(selection_metric="sharpe")
    r.folds = [
        FoldResult(0, ("a", "b"), ("c", "d"), {}, {"sharpe": -0.5},
                   {"sharpe": -1.0, "n_trades": 10}, 2),
    ]
    assert r.decay is None


def test_decay_ignores_folds_without_trades():
    """空仓折的 OOS 恒为 0，若计入会把'没交易'误读成'衰减到 0'。"""
    from quart.backtest.walkforward import FoldResult

    r = WFAResult(selection_metric="sharpe")
    r.folds = [
        # 有交易：IS 1.0 → OOS 0.8
        FoldResult(0, ("a", "b"), ("c", "d"), {}, {"sharpe": 1.0},
                   {"sharpe": 0.8, "n_trades": 10}, 2),
        # 无交易：OOS 恒 0，必须被排除
        FoldResult(1, ("a", "b"), ("c", "d"), {}, {"sharpe": 1.0},
                   {"sharpe": 0.0, "n_trades": 0}, 2),
    ]
    assert r.decay == pytest.approx(0.8), "空仓折不应参与衰减比计算"
    assert r.n_folds_with_trades == 1


def test_n_folds_with_trades_zero_when_all_empty():
    from quart.backtest.walkforward import FoldResult

    r = WFAResult(selection_metric="sharpe")
    r.folds = [
        FoldResult(0, ("a", "b"), ("c", "d"), {}, {"sharpe": 1.0},
                   {"sharpe": 0.0, "n_trades": 0}, 2),
    ]
    assert r.n_folds_with_trades == 0
    assert r.decay is None, "全空仓时不应给出误导性的衰减比"


def test_grid_selection_picks_best_in_sample():
    """参数网格应选出样本内最优，而非第一组或最后一组。"""
    md = _md(n=500)
    result = run_walk_forward(
        md, None, "declining",
        param_grid={"period": [3, 10, 30]},
        train_days=200, test_days=50, embargo_days=5,
        fees=Fees.zero(),
        build_strategy_fn=lambda name, **kw: _Declining(**kw),
    )
    chosen = {f.best_params["period"] for f in result.folds}
    assert len(chosen) >= 1
    assert all(p in (3, 10, 30) for p in chosen)


def test_min_trades_filters_inactive_candidates():
    """min_trades 应淘汰'没在交易'的参数（如流动性门槛把组合清空）。"""
    md = _md(n=500)
    result = run_walk_forward(
        md, None, "declining",
        param_grid={"period": [5]},
        train_days=200, test_days=50, embargo_days=5,
        min_trades=10_000,  # 不可能达到
        fees=Fees.zero(),
        build_strategy_fn=lambda name, **kw: _Declining(**kw),
    )
    # 全部候选被淘汰后应回退到第一组，而不是丢掉整折
    assert len(result.folds) >= 3
    assert result.folds[0].best_params == {"period": 5}


def test_oos_summary_populated():
    md = _md(n=450)
    result = run_walk_forward(
        md, None, "declining",
        train_days=200, test_days=50, embargo_days=5,
        fees=Fees.zero(),
        build_strategy_fn=lambda name, **kw: _Declining(**kw),
    )
    assert "cagr" in result.oos_summary
    assert "sharpe" in result.oos_summary
    assert len(result.oos_equity) >= 2
    # 与直接对拼接曲线算指标一致
    assert result.oos_summary["total_return"] == pytest.approx(
        float(result.oos_equity.iloc[-1] / result.oos_equity.iloc[0] - 1)
    )

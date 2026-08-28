from __future__ import annotations

import numpy as np
import pandas as pd

from quart.backtest.engine import MarketData
from quart.strategy.ml_rank import MLRankStrategy


def make_md(n_days=120, symbols=("A", "B", "C", "D")) -> MarketData:
    dates = pd.date_range("2024-01-01", periods=n_days)
    rng = np.random.default_rng(7)
    rets = pd.DataFrame(rng.normal(0.001, 0.02, size=(n_days, len(symbols))), index=dates, columns=list(symbols))
    closes = (1 + rets).cumprod() * 10
    opens = closes.shift(1).fillna(closes.iloc[0])
    bars = pd.DataFrame({
        "date": np.repeat(dates, len(symbols)),
        "symbol": np.tile(closes.columns.values, n_days),
        "open": opens.to_numpy().ravel(),
        "high": (np.maximum(opens, closes) * 1.01).to_numpy().ravel(),
        "low": (np.minimum(opens, closes) * 0.99).to_numpy().ravel(),
        "close": closes.to_numpy().ravel(),
        "volume": 1e6,
        "amount": 1e7,
    })
    bench = pd.DataFrame({
        "date": dates, "symbol": "IDX000300", "open": 3000.0, "high": 3010.0,
        "low": 2990.0, "close": 3000.0, "volume": 1e8, "amount": 3e11,
    })
    return MarketData.from_bars(bars, benchmark=bench)


def write_scores(path, rows):
    pd.DataFrame(rows, columns=["datetime", "instrument", "score"]).to_csv(path, index=False)


def test_ml_rank_picks_top_scores(tmp_path):
    scores_file = tmp_path / "preds.csv"
    dates = pd.date_range("2024-03-01", periods=30)
    write_scores(scores_file, [(d, s, 4 - i) for d in dates for i, s in enumerate(["A", "B", "C", "D"])])

    md = make_md()
    strat = MLRankStrategy(
        scores_path=str(scores_file), top_k=2, rebalance_days=5,
        max_weight_pct=0.6, use_regime_filter=False,
    )
    strat.prepare(md)

    i = md.dates.get_loc(pd.Timestamp("2024-03-04"))
    w = strat.target_weights(i)
    assert set(w.keys()) == {"A", "B"}
    assert all(v == 0.5 for v in w.values())


def test_ml_rank_stale_scores_expire(tmp_path):
    scores_file = tmp_path / "preds.csv"
    write_scores(scores_file, [
        (pd.Timestamp("2024-03-01"), "A", 9.0),
        (pd.Timestamp("2024-03-01"), "B", 8.0),
    ])

    md = make_md(n_days=120)
    strat = MLRankStrategy(
        scores_path=str(scores_file), top_k=2, rebalance_days=1,
        max_weight_pct=1.0, use_regime_filter=False, stale_days=35,
    )
    strat.prepare(md)

    early_i = md.dates.get_loc(pd.Timestamp("2024-03-04"))
    assert set(strat.target_weights(early_i).keys()) == {"A", "B"}

    late_date = pd.Timestamp("2024-06-15")
    if late_date in md.dates:
        late_i = md.dates.get_loc(late_date)
        assert strat.target_weights(late_i) == {}


def test_ml_rank_regime_filter_goes_cash(tmp_path):
    scores_file = tmp_path / "preds.csv"
    dates = pd.date_range("2024-03-01", periods=30)
    write_scores(scores_file, [(d, s, 1.0) for d in dates for s in ["A", "B"]])

    md = make_md()
    n = len(md.dates)
    md.benchmark_close = pd.Series(np.linspace(3000.0, 2600.0, n), index=md.dates)

    strat = MLRankStrategy(
        scores_path=str(scores_file), top_k=2, rebalance_days=5,
        use_regime_filter=True, regime_filter_days=20, regime_band=0.0,
    )
    strat.prepare(md)
    i = md.dates.get_loc(pd.Timestamp("2024-04-01"))
    from quart.backtest.engine import FLAT
    assert strat.target_weights(i) == {FLAT: 1.0}


def test_regime_hysteresis_band_requires_deeper_break():
    """带缓冲带时：小幅跌破 MA（<2%）不触发 FLAT；深度跌破（>2%）触发。"""
    import pandas as pd

    from quart.strategy.filters import regime_flat_series

    dates = pd.date_range("2024-01-01", periods=60)
    ma = pd.Series(100.0, index=dates)
    # close = 99（仅低于 MA 1%，在缓冲带内）→ 不应翻空
    close_shallow = pd.Series(99.0, index=dates)
    flat_shallow = regime_flat_series(close_shallow, ma, band=0.02)
    assert not flat_shallow.iloc[-1]
    # close = 97（低于 MA 3%，超出缓冲带）→ 应翻空
    close_deep = pd.Series(97.0, index=dates)
    flat_deep = regime_flat_series(close_deep, ma, band=0.02)
    assert flat_deep.iloc[-1]
    # 翻空后 close 回到 99（MA 上方 1%，仍在缓冲带内）→ 保持空仓（hysteresis）
    close_recover = pd.Series(99.0, index=dates)
    close_recover.iloc[:30] = 97.0  # 先深度跌破
    flat_recover = regime_flat_series(close_recover, ma, band=0.02)
    assert flat_recover.iloc[-1]
    # close 回到 103（MA 上方 3%，明确超出缓冲带）→ 恢复持仓
    close_full = pd.Series(103.0, index=dates)
    close_full.iloc[:30] = 97.0
    flat_full = regime_flat_series(close_full, ma, band=0.02)
    assert not flat_full.iloc[-1]


def test_missing_scores_file_raises(tmp_path):
    import pytest

    strat = MLRankStrategy(scores_path=str(tmp_path / "none.csv"))
    with pytest.raises(FileNotFoundError):
        strat.prepare(make_md())

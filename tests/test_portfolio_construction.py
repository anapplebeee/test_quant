"""多因子合成 + 组合构造升级测试（lowvol 系策略增强）。

覆盖：
- pit_panels：披露时滞防前视、ep/bp 定价、宽表面板形态；
- lowvol vg 合成：vg_weight>0 复合分被质量分牵引、混合公式正确；
- _risk_weights：inv_vol / zscore 权重归一化与 max_weight 截断；
- equal 模式保持历史行为。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import quart.config as quart_config
import quart.strategy.lowvol_composite as lc_mod
from quart.strategy.lowvol_composite import LowVolCompositeStrategy


# ---------------- fixture ----------------

def _fin_fixture() -> pd.DataFrame:
    # 两个报告期：Q4-2019（+120d 后 2020-04-29 起可用）、Q1-2020（+120d 后 2020-07-29 起可用）
    return pd.DataFrame([
        {"symbol": "A", "date": "2019-12-31", "eps": 0.5, "bps": 10.0, "roe": 5.0, "profit_yoy": 10.0},
        {"symbol": "A", "date": "2020-03-31", "eps": 0.8, "bps": 10.5, "roe": 8.0, "profit_yoy": 20.0},
        {"symbol": "B", "date": "2019-12-31", "eps": 0.2, "bps": 5.0, "roe": 2.0, "profit_yoy": -5.0},
    ])


def _closes_fixture() -> pd.DataFrame:
    dates = pd.date_range("2020-01-01", periods=300, freq="D")
    return pd.DataFrame({"A": 10.0, "B": 20.0}, index=dates)


def _md_from_closes(closes: pd.DataFrame):
    from quart.backtest.engine import MarketData

    dates = closes.index
    opens = closes.shift(1).fillna(closes.iloc[0])
    highs = np.maximum(opens, closes) * 1.005
    lows = np.minimum(opens, closes) * 0.995
    frames = []
    for s in closes.columns:
        frames.append(pd.DataFrame({
            "date": dates, "symbol": s, "open": opens[s], "high": highs[s],
            "low": lows[s], "close": closes[s], "volume": 1e6, "amount": 1e8,
        }))
    return MarketData.from_bars(pd.concat(frames, ignore_index=True))


def _lowvol_md(n_syms=10, n_days=80, seed=7):
    dates = pd.date_range("2024-01-01", periods=n_days)
    rng = np.random.default_rng(seed)
    cols = {}
    for n in range(n_syms):
        vol = 0.008 if n == 0 else 0.05  # S0 低波，其余高波
        rets = rng.normal(0.0002, vol, size=n_days)
        cols[f"S{n}"] = (1 + rets).cumprod() * 10
    return _md_from_closes(pd.DataFrame(cols, index=dates))


# ---------------- pit_panels ----------------

def test_pit_panels_no_lookahead():
    from quart.research.value_growth import pit_panels

    closes = _closes_fixture()
    panels = pit_panels(_fin_fixture(), closes, factors=("roe_improve", "profit_yoy", "ep", "bp"))
    assert set(panels) == {"roe_improve", "profit_yoy", "ep", "bp"}

    ep = panels["ep"]
    # 2020-01-15：Q4-2019 报告期 +120d（2020-04-29）之前 → 不可用
    assert pd.isna(ep.loc["2020-01-15", "A"])
    # 2020-05-15：Q4 报告可用，ep = eps/close*100 = 0.5/10*100 = 5.0
    assert np.isclose(ep.loc["2020-05-15", "A"], 5.0)
    # 2020-09-15：Q1 报告可用，ep = 0.8/10*100 = 8.0
    assert np.isclose(ep.loc["2020-09-15", "A"], 8.0)
    # roe_improve：第二个报告期才出现 = 8.0 - 5.0 = 3.0
    assert np.isclose(panels["roe_improve"].loc["2020-09-15", "A"], 3.0)
    # 无后续报告的 B 停留在 Q4：bp = 5/20*100 = 25
    assert np.isclose(panels["bp"].loc["2020-09-15", "B"], 25.0)


def test_pit_panels_empty_inputs():
    from quart.research.value_growth import pit_panels

    assert pit_panels(pd.DataFrame(), _closes_fixture()) == {}
    assert pit_panels(_fin_fixture(), pd.DataFrame()) == {}


# ---------------- lowvol vg 合成 ----------------

def test_vg_blend_changes_composite(monkeypatch, tmp_path):
    """vg_weight>0：复合分 = (1-w)*低波 + w*价值成长z；缺失财务 = 中性 0。"""
    fin_dir = tmp_path / "data" / "factors"
    fin_dir.mkdir(parents=True)
    _fin_fixture().to_parquet(fin_dir / "financials.parquet")
    monkeypatch.setattr(quart_config, "PROJECT_ROOT", tmp_path)

    # 2020-09 起 Q1 报告已可用
    dates = pd.date_range("2020-09-01", periods=30)
    closes = pd.DataFrame({"A": np.linspace(10, 11, 30), "B": np.linspace(20, 21, 30)}, index=dates)
    md = _md_from_closes(closes)

    base = LowVolCompositeStrategy(top_k=1, rebalance_days=1, use_regime_filter=False, min_avg_amount=None)
    base.prepare(md)

    blend = LowVolCompositeStrategy(top_k=1, rebalance_days=1, use_regime_filter=False,
                                    min_avg_amount=None, vg_weight=0.5)
    blend.prepare(md)
    assert blend.vg_score is not None

    w = 0.5
    expected = ((1 - w) * base.composite
                + w * blend.vg_score.reindex_like(base.composite).fillna(0.0)).astype("float32")
    pd.testing.assert_frame_equal(blend.composite, expected)
    # 合成确实改变了截面
    assert not np.allclose(
        blend.composite.dropna().to_numpy(), base.composite.dropna().to_numpy()
    )


def test_vg_disabled_by_default(monkeypatch, tmp_path):
    """vg_weight=0（默认）不读财务文件、composite 与历史一致。"""
    fin_dir = tmp_path / "data" / "factors"
    fin_dir.mkdir(parents=True)
    _fin_fixture().to_parquet(fin_dir / "financials.parquet")
    monkeypatch.setattr(quart_config, "PROJECT_ROOT", tmp_path)

    md = _lowvol_md()
    base = LowVolCompositeStrategy(top_k=2, rebalance_days=1, use_regime_filter=False, min_avg_amount=None)
    base.prepare(md)
    assert base.vg_score is None


def test_vg_missing_file_degrades_gracefully(tmp_path):
    """financials.parquet 不存在 → vg_score None，纯低波不变。"""
    md = _lowvol_md()
    strat = LowVolCompositeStrategy(top_k=2, rebalance_days=1, use_regime_filter=False,
                                    min_avg_amount=None, vg_weight=0.5)
    strat.prepare(md)
    assert strat.vg_score is None


# ---------------- 组合构造 ----------------

def _strat_with_vol(vol_map: dict[str, float], max_w: float = 1.0) -> LowVolCompositeStrategy:
    strat = LowVolCompositeStrategy(top_k=len(vol_map), max_weight_pct=max_w)
    strat.weight_mode = "inv_vol"
    strat.max_weight = max_w
    dates = pd.date_range("2024-01-01", periods=2)
    strat.vol20 = pd.DataFrame([vol_map] * 2, index=dates)
    return strat


def test_inv_vol_weights_inverse_to_vol():
    strat = _strat_with_vol({"A": 0.01, "B": 0.02, "C": 0.04})
    scores = pd.Series({"A": 1.0, "B": 1.0, "C": 1.0})
    w = strat._risk_weights(["A", "B", "C"], scores, 1)
    assert np.isclose(w.sum(), 1.0)
    # 波动率倒数比 1/0.01 : 1/0.02 : 1/0.04 = 4 : 2 : 1
    assert np.isclose(w["A"], 4 / 7) and np.isclose(w["B"], 2 / 7) and np.isclose(w["C"], 1 / 7)


def test_inv_vol_nan_vol_falls_back_to_mean():
    strat = _strat_with_vol({"A": 0.01, "B": 0.02})  # max_w=1.0，不触发截断
    strat.vol20.loc[strat.vol20.index[1], "C"] = np.nan
    scores = pd.Series({"A": 1.0, "B": 1.0, "C": 1.0})
    w = strat._risk_weights(["A", "B", "C"], scores, 1)
    assert np.isclose(w.sum(), 1.0)
    # NaN 波动率 → inv 序列均值回填 = (1/0.01 + 1/0.02)/2 = 75；归一化后 = 75/225
    assert np.isclose(w["C"], 75 / 225)


def test_zscore_weights_preserve_strength_ordering():
    strat = LowVolCompositeStrategy(top_k=3)
    strat.weight_mode = "zscore"
    strat.max_weight = 1.0
    scores = pd.Series({"A": 2.0, "B": 1.0, "C": 0.0})
    w = strat._risk_weights(["A", "B", "C"], scores, 0)
    assert np.isclose(w.sum(), 1.0)
    assert w["A"] > w["B"] > w["C"] > 0
    # 平移不变：整体加常数后权重一致
    w2 = strat._risk_weights(["A", "B", "C"], scores + 10.0, 0)
    pd.testing.assert_series_equal(w, w2)


def test_max_weight_cap_iterative():
    strat = _strat_with_vol({"A": 0.001, "B": 0.1, "C": 0.1}, max_w=0.4)
    scores = pd.Series({"A": 1.0, "B": 1.0, "C": 1.0})
    w = strat._risk_weights(["A", "B", "C"], scores, 1)
    assert (w <= 0.4 + 1e-9).all()
    assert np.isclose(w.sum(), 1.0)


def test_equal_mode_unchanged_history():
    """equal 模式（默认）保持历史行为：min(1/k, cap)，不归一。"""
    strat = LowVolCompositeStrategy(top_k=5, max_weight_pct=0.25, rebalance_days=1,
                                    use_regime_filter=False, min_avg_amount=None)
    md = _lowvol_md()
    strat.prepare(md)
    i = len(md.dates) - 2
    w = strat.target_weights(i)
    assert len(w) == 5
    assert all(np.isclose(v, 0.2) for v in w.values())


def test_target_weights_inv_vol_integration():
    """target_weights 端到端：inv_vol 模式权重和 ≤ 1，低波票权重最高。"""
    strat = LowVolCompositeStrategy(top_k=3, max_weight_pct=1.0, rebalance_days=1,
                                    use_regime_filter=False, min_avg_amount=None,
                                    weight_mode="inv_vol")
    md = _lowvol_md()
    strat.prepare(md)
    i = len(md.dates) - 2
    w = strat.target_weights(i)
    assert len(w) == 3
    assert sum(w.values()) <= 1.0 + 1e-6
    top = max(w, key=w.get)
    assert top == "S0"  # 低波票在 inv_vol 加权下应权重最高


def test_unknown_weight_mode_falls_back_equal():
    strat = LowVolCompositeStrategy(top_k=2, weight_mode="bogus")
    assert strat.weight_mode == "equal"

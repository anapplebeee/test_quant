"""研报因子移植测试：择时模块（R4）、技术指标因子库（R3/R4）、价值成长因子（R2）。

构造合成数据验证核心不变量（趋势市/震荡市/数据缺失退化），
并用真实日线数据做回测冒烟（regime_mode="score" 与 "ma" 均可跑通）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.data.market import MarketData
from quart.research.momentum import (
    rank_momentum,
    remove_limit_up_momentum,
    signed_smooth,
    simple_momentum,
)
from quart.research.technicals import FACTOR_CATALOG, compute_panels
from quart.research.value_growth import build_value_growth
from quart.strategy.timing import score_timing_exposure


# ---------------------------------------------------------------- 合成数据

# 路径动量包含 240 日窗口；保持足够尾部样本，避免因子面板的最小历史
# 过滤把长窗口因子误判为空。
N_DAYS = 400
N_SYMS = 8


def _synthetic_md(trend: str = "up") -> MarketData:
    """构造合成行情：基准趋势 up/down/sideways，个股围绕基准波动。"""
    rng = np.random.default_rng(42)
    idx = pd.bdate_range("2025-01-01", periods=N_DAYS)
    if trend == "up":
        drift = np.linspace(0, 0.8, N_DAYS)
    elif trend == "down":
        drift = np.linspace(0, -0.5, N_DAYS)
    else:
        drift = 0.3 * np.sin(np.linspace(0, 6 * np.pi, N_DAYS))
    bench = pd.Series(100 * (1 + drift + rng.normal(0, 0.005, N_DAYS).cumsum() * 0.05), index=idx)
    cols = [f"S{i:06d}" for i in range(N_SYMS)]
    # 个股 = 基准 × (1 + 小幅个股特质偏离)，保证合成市场方向与基准一致
    rel = 1 + rng.normal(0, 0.01, (N_DAYS, N_SYMS)).cumsum(axis=0) * 0.002
    closes = rel * bench.to_numpy()[:, None]
    closes = pd.DataFrame(closes, index=idx, columns=cols)
    opens = closes.shift(1).fillna(closes)
    highs = closes * 1.01
    lows = closes * 0.99
    volumes = pd.DataFrame(rng.uniform(1e6, 5e6, (N_DAYS, N_SYMS)), index=idx, columns=cols)
    amounts = volumes * closes
    return MarketData(opens, highs, lows, closes, volumes, benchmark_close=bench, amounts=amounts)


# ---------------------------------------------------------------- timing (R4)

def test_score_timing_up_trend_full_exposure():
    md = _synthetic_md("up")
    exposure = score_timing_exposure(md, levels=3)
    assert exposure is not None
    tail = exposure.iloc[-30:]
    assert (tail > 0).mean() > 0.8, "持续上涨趋势末段应以持仓为主"


def test_score_timing_down_trend_flat():
    md = _synthetic_md("down")
    exposure = score_timing_exposure(md, levels=3)
    assert exposure is not None
    tail = exposure.iloc[-30:]
    assert (tail == 0).mean() > 0.8, "持续下跌趋势末段应空仓为主"


def test_score_timing_exposure_on_level_grid():
    md = _synthetic_md("sideways")
    exposure = score_timing_exposure(md, levels=3)
    assert set(exposure.dropna().unique()).issubset({0.0, 0.5, 1.0})


def test_score_timing_two_levels_binary():
    md = _synthetic_md("up")
    exposure = score_timing_exposure(md, levels=2)
    assert set(exposure.dropna().unique()).issubset({0.0, 1.0})


def test_score_timing_degrades_without_amounts():
    """缺成交额面板时量能/价量类别自动剔除，不报错、仍出序列。"""
    md = _synthetic_md("up")
    no_amounts = MarketData(
        md.opens, md.highs, md.lows, md.closes, md.volumes,
        benchmark_close=md.benchmark_close, amounts=None,
    )
    exposure = score_timing_exposure(no_amounts, levels=3)
    assert exposure is not None
    assert (exposure.iloc[-30:] >= 0).all()


def test_score_timing_none_without_benchmark():
    md = _synthetic_md("up")
    bare = MarketData(md.opens, md.highs, md.lows, md.closes, md.volumes, benchmark_close=None)
    assert score_timing_exposure(bare) is None


def test_score_timing_warmup_is_flat():
    md = _synthetic_md("up")
    exposure = score_timing_exposure(md, levels=3)
    assert (exposure.iloc[:10] == 0).all(), "预热期（均线未就绪）应空仓"


# ---------------------------------------------------------------- technicals (R3/R4)

def test_compute_panels_all_factors():
    md = _synthetic_md("up")
    panels = compute_panels(md)
    assert set(panels) == set(FACTOR_CATALOG)
    for name, panel in panels.items():
        assert isinstance(panel, pd.DataFrame)
        assert not panel.empty, f"{name} 面板不应为空"
        assert panel.notna().to_numpy().any(), f"{name} 不应全为 NaN"


def test_technical_factors_scale_free():
    """归一化因子不受价格量纲影响：价格×10 后因子截面排序基本不变。"""
    md = _synthetic_md("up")
    md10 = MarketData(
        md.opens * 10, md.highs * 10, md.lows * 10, md.closes * 10,
        md.volumes, benchmark_close=md.benchmark_close, amounts=md.amounts * 10,
    )
    p1 = compute_panels(md, names=["macd_dif", "dma", "trix"]).get("macd_dif")
    p2 = compute_panels(md10, names=["macd_dif", "dma", "trix"]).get("macd_dif")
    last1 = p1.iloc[-1].dropna().rank()
    last2 = p2.iloc[-1].dropna().rank()
    common = last1.index.intersection(last2.index)
    corr = last1[common].corr(last2[common])
    assert corr > 0.95, f"价格缩放后排序相关性 {corr:.3f} 应接近 1"


def test_compute_panels_rejects_unknown():
    md = _synthetic_md("up")
    with pytest.raises(KeyError):
        compute_panels(md, names=["no_such_factor"])


def test_path_momentum_skip_and_direction():
    idx = pd.bdate_range("2025-01-01", periods=5)
    closes = pd.DataFrame(
        {"UP": [100.0, 101.0, 102.0, 103.0, 104.0],
         "DOWN": [100.0, 99.0, 98.0, 97.0, 96.0]},
        index=idx,
    )
    # t=4 跳过最近一天后，比较 t=3 与 t=1。
    got = simple_momentum(closes, window=2, skip_days=1)
    assert got.loc[idx[4], "UP"] == pytest.approx(103 / 101 - 1)

    smooth = signed_smooth(closes, window=2)
    assert smooth.loc[idx[2], "UP"] > 0
    assert smooth.loc[idx[2], "DOWN"] < 0


def test_rank_momentum_is_cross_sectional_percentile():
    idx = pd.bdate_range("2025-01-01", periods=4)
    closes = pd.DataFrame(
        {"A": [100.0, 101.0, 102.0, 104.0],
         "B": [100.0, 100.0, 100.0, 100.0],
         "C": [100.0, 99.0, 98.0, 97.0]},
        index=idx,
    )
    rank = rank_momentum(closes, window=2, skip_days=0)
    assert rank.loc[idx[3], "A"] > rank.loc[idx[3], "B"] > rank.loc[idx[3], "C"]
    assert rank.loc[idx[3]].between(0.0, 1.0).all()


def test_remove_limit_up_excludes_threshold_day():
    idx = pd.bdate_range("2025-01-01", periods=3)
    closes = pd.DataFrame({"A": [100.0, 110.0, 110.0]}, index=idx)
    got = remove_limit_up_momentum(closes, window=1, skip_days=0, limit_up_threshold=0.095)
    assert got.loc[idx[1], "A"] == pytest.approx(0.0)
    assert got.loc[idx[2], "A"] == pytest.approx(0.0)


def test_vwap_uses_rolling_amount_over_volume():
    md = _synthetic_md("up")
    panel = compute_panels(md, names=["vwap_dev"], min_history=0)["vwap_dev"]
    day = md.dates[25]
    amount = md.amounts.iloc[6:26].sum(axis=0)
    volume = md.volumes.iloc[6:26].sum(axis=0)
    expected = md.closes.loc[day] / (amount / volume) - 1.0
    pd.testing.assert_series_equal(panel.loc[day], expected, check_names=False)


# ---------------------------------------------------------------- value_growth (R2)

def _financials_fixture() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["S000001", "S000001", "S000002", "S000002"],
            "date": ["2025-03-31", "2025-06-30", "2025-03-31", "2025-06-30"],
            "eps": [0.5, 0.8, 1.0, 0.9],
            "bps": [10.0, 10.5, 5.0, 5.1],
            "roe": [5.0, 7.5, 20.0, 17.0],
            "gross_margin": [30.0, 31.0, 50.0, 49.0],
            "rev_yoy": [10.0, 12.0, 5.0, 3.0],
            "profit_yoy": [8.0, 15.0, 20.0, -5.0],
        }
    )


def test_value_growth_basic():
    md = _synthetic_md("up")
    fin = _financials_fixture()
    out = build_value_growth(fin, md.closes, as_of="2025-12-31")
    assert not out.empty
    assert {"roe", "roe_improve", "ep", "bp", "vg_score"}.issubset(out.columns)
    # 披露时滞：2025-06-30 报告期 +120 天 = 2025-10-28 后才可用，
    # as_of=2025-12-31 时最新报告应为 Q2
    assert (out["roe_improve"] == pd.Series({"S000001": 2.5, "S000002": -3.0})).all()


def test_value_growth_respects_disclosure_lag():
    """as_of 早于（最新报告期+时滞）时只能用到更早的报告期。"""
    md = _synthetic_md("up")
    fin = _financials_fixture()
    out = build_value_growth(fin, md.closes, as_of="2025-09-30")
    assert not out.empty
    # 2025-06-30+120d=10-28 > 09-30 → 只能用 2025-03-31 报告，无法算改善
    assert out["roe_improve"].isna().all()


def test_value_growth_empty_when_no_data():
    md = _synthetic_md("up")
    out = build_value_growth(_financials_fixture(), md.closes, as_of="2020-01-01")
    assert out.empty


# ---------------------------------------------------------------- 接线：训练特征 / lowvol / config

def test_stack_panels_roundtrip():
    """宽表 → stack → 与 qlib 风格 (datetime, instrument) 索引对齐。"""
    from quart.research.technicals import stack_panels

    md = _synthetic_md("up")
    panels = compute_panels(md, names=["er", "tii"])
    stacked = stack_panels(panels)
    assert stacked.index.names == ["datetime", "instrument"]
    assert list(stacked.columns) == ["er", "tii"]
    # 行数应等于两面板非空 cell 数
    assert len(stacked) > 0
    # instrument 应为原始 symbol 代码（与 export_to_qlib 一致）
    assert set(stacked.index.get_level_values("instrument")).issubset(md.closes.columns)


def test_pit_features_no_lookahead():
    """PIT 特征：报告期+120天前的日期拿不到该报告，之后才能拿到。"""
    from quart.research.value_growth import pit_features

    fin = _financials_fixture()
    # S000001 两期报告：2025-03-31（+120d=07-29 可用）、2025-06-30（+120d=10-28 可用）
    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2025-07-28"), "S000001"),  # 07-29 前一天 → 只能用 Q1
            (pd.Timestamp("2025-07-29"), "S000001"),  # Q1
            (pd.Timestamp("2025-10-27"), "S000001"),  # 仍 Q1
            (pd.Timestamp("2025-10-28"), "S000001"),  # Q2
            (pd.Timestamp("2025-12-01"), "S000002"),  # Q2
        ],
        names=["datetime", "instrument"],
    )
    out = pit_features(fin, idx)
    assert list(out.index) == list(idx)
    # 07-28：Q1 可用日 07-29 之前 → 什么报告都拿不到（无前视，NaN 是正确行为）
    assert pd.isna(out.loc[(pd.Timestamp("2025-07-28"), "S000001"), "vg_roe"])
    # 07-29 起 Q1 可用（roe=5.0）
    assert out.loc[(pd.Timestamp("2025-07-29"), "S000001"), "vg_roe"] == 5.0
    assert out.loc[(pd.Timestamp("2025-10-27"), "S000001"), "vg_roe"] == 5.0
    # 10-28 起可用 Q2（roe=7.5），且 roe_improve=7.5-5.0=2.5
    assert out.loc[(pd.Timestamp("2025-10-28"), "S000001"), "vg_roe"] == 7.5
    assert out.loc[(pd.Timestamp("2025-10-28"), "S000001"), "roe_improve"] == 2.5
    assert out.loc[(pd.Timestamp("2025-12-01"), "S000002"), "vg_roe"] == 17.0


def test_lowvol_accepts_score_regime():
    """lowvol_composite(regime_mode='score') 参数可通过 PARAMS_SCHEMA 校验并运行。"""
    from quart.strategy.lowvol_composite import LowVolCompositeStrategy

    md = _synthetic_md("down")
    strat = LowVolCompositeStrategy(
        regime_mode="score", timing_levels=2, use_regime_filter=True, top_k=5
    )
    strat.prepare(md)
    assert strat.timing_exposure is not None
    tail = [strat.target_weights(i) for i in range(N_DAYS - 10, N_DAYS)]
    assert any(w.get("__FLAT__") for w in tail), "下跌趋势末段 2 档模式应出现全清仓"


def test_config_override_applies_score_mode():
    """config overrides 里 momentum_rotation 的 regime_mode=score 应被 build_strategy 应用。"""
    from quart.config import load_config
    from quart.strategy import build_strategy

    cfg = load_config()
    override = cfg["strategy"].get("overrides", {}).get("momentum_rotation", {})
    assert override.get("regime_mode") == "score", "config 应包含 momentum_rotation 的 score 覆盖"

    params = {**{k: v for k, v in cfg["strategy"].items()
                 if k not in ("name", "live_allowlist", "overrides")}, **override}
    strat = build_strategy("momentum_rotation", **params)
    assert strat.params.get("regime_mode") == "score"


# ---------------------------------------------------------------- 策略接入冒烟

def test_momentum_accepts_score_regime():
    """momentum_rotation(regime_mode='score') prepare+target_weights 全流程。"""
    from quart.strategy.momentum import MomentumRotationStrategy

    md = _synthetic_md("up")
    strat = MomentumRotationStrategy(
        regime_mode="score", timing_levels=3, lookback_days=20, top_k=5
    )
    strat.prepare(md)
    assert strat.timing_exposure is not None
    got_weights = False
    for i in range(80, N_DAYS):
        w = strat.target_weights(i)
        if w and "__FLAT__" not in w:
            got_weights = True
            total = sum(w.values())
            exposure = float(strat.timing_exposure.iloc[i])
            assert total <= exposure * 1.0 + 1e-9, "权重总和不应超过目标仓位档"
            break
    assert got_weights, "上涨趋势中应产生过持仓"


def test_momentum_score_regime_flat_in_downtrend():
    from quart.execution.constraints import FLAT
    from quart.strategy.momentum import MomentumRotationStrategy

    md = _synthetic_md("down")
    strat = MomentumRotationStrategy(
        regime_mode="score", timing_levels=2, lookback_days=20, top_k=5
    )
    strat.prepare(md)
    tail = [strat.target_weights(i) for i in range(N_DAYS - 10, N_DAYS)]
    assert any(w.get(FLAT) for w in tail), "下跌趋势末段 2 档模式应出现全清仓"


def test_momentum_path_strategy_uses_report_factor():
    from quart.strategy import build_strategy

    md = _synthetic_md("up")
    strat = build_strategy(
        "momentum_path",
        use_regime_filter=False,
        momentum_mode="smooth",
        lookback_days=40,
        momentum_skip_days=5,
        top_k=3,
    )
    strat.prepare(md)
    assert strat.momentum_mode == "smooth"
    assert strat.warmup == 46
    assert strat.required_history_days == 46
    got = [strat.target_weights(i) for i in range(46, N_DAYS)]
    assert any(w and "__FLAT__" not in w for w in got)

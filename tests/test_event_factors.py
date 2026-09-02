from __future__ import annotations

import re

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.research.event_factors import (
    DIRECTOR_SALE_PERSON_REGEX,
    director_sale_support_panels,
    dragon_tiger_panels,
    event_sentiment_panels,
    limit_event_panels,
    neutralize_against,
    price_limit_panel,
)


def _market(dates: pd.DatetimeIndex, symbols: list[str]) -> MarketData:
    close = pd.DataFrame(10.0, index=dates, columns=symbols)
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=symbols)
    amount = volume * close * 100.0
    return MarketData(open_, close * 1.01, close * 0.99, close, volume, amounts=amount)


def test_price_limit_panel_uses_historical_chinext_rule():
    dates = pd.DatetimeIndex(["2020-08-21", "2020-08-24"])
    panel = price_limit_panel(dates, ["300001", "600000"])

    assert panel.loc[dates[0], "300001"] == np.float32(0.10)
    assert panel.loc[dates[1], "300001"] == np.float32(0.20)
    assert panel.loc[dates[1], "600000"] == np.float32(0.10)


def test_limit_event_panels_only_use_current_and_past_data():
    dates = pd.bdate_range("2024-01-02", periods=50)
    market = _market(dates, ["600000", "600001"])
    market.close_val.loc[dates[15], "600000"] = 11.0
    market.closes.loc[dates[15], "600000"] = 11.0
    original = limit_event_panels(market)

    changed = _market(dates, ["600000", "600001"])
    changed.close_val.loc[dates[15], "600000"] = 11.0
    changed.closes.loc[dates[15], "600000"] = 11.0
    changed.close_val.loc[dates[31]:, :] *= 3.0
    changed.closes.loc[dates[31]:, :] *= 3.0
    mutated = limit_event_panels(changed)

    assert original["limit_hit_count20_neg"].loc[dates[25], "600000"] == -1.0
    for name in original:
        pd.testing.assert_series_equal(
            original[name].loc[dates[30]], mutated[name].loc[dates[30]], check_names=False
        )


def test_neutralize_against_removes_cross_sectional_linear_exposure():
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2025-01-02", periods=4)
    symbols = [f"{600000 + i:06d}" for i in range(30)]
    control = pd.DataFrame(rng.normal(size=(4, 30)), index=dates, columns=symbols)
    noise = pd.DataFrame(rng.normal(scale=0.1, size=(4, 30)), index=dates, columns=symbols)
    residual = neutralize_against(3.0 * control + noise, control)

    for date in dates:
        assert abs(residual.loc[date].corr(control.loc[date])) < 1e-7


def test_event_availability_respects_close_and_date_only_records():
    dates = pd.bdate_range("2025-01-02", periods=4)
    symbols = ["600000", "600001", "600002"]
    events = pd.DataFrame(
        {
            "symbol": symbols,
            "published_at": [
                "2025-01-02 14:00:00",
                "2025-01-02 16:00:00",
                "2025-01-02",
            ],
            "sentiment": [1.0, -1.0, 0.5],
        }
    )
    panel = event_sentiment_panels(events, dates, symbols)["event_sentiment_decay"]

    assert panel.loc[dates[0], "600000"] == 1.0
    assert panel.loc[dates[0], "600001"] == 0.0
    assert panel.loc[dates[0], "600002"] == 0.0
    assert panel.loc[dates[1], "600001"] < 0.0
    assert panel.loc[dates[1], "600002"] > 0.0


def test_explicit_available_at_takes_precedence():
    dates = pd.bdate_range("2025-01-02", periods=4)
    events = pd.DataFrame(
        {
            "symbol": ["600000"],
            "published_at": ["2025-01-02 10:00:00"],
            "available_at": ["2025-01-03 09:00:00"],
            "sentiment": [1.0],
        }
    )
    panel = event_sentiment_panels(events, dates, ["600000"])["event_sentiment_decay"]

    assert panel.loc[dates[0], "600000"] == 0.0
    assert panel.loc[dates[1], "600000"] == 1.0


def test_available_at_cannot_precede_publication():
    dates = pd.bdate_range("2025-01-02", periods=4)
    events = pd.DataFrame(
        {
            "symbol": ["600000"],
            "published_at": ["2025-01-02 16:00:00"],
            "available_at": ["2025-01-02 10:00:00"],
            "sentiment": [1.0],
        }
    )
    panel = event_sentiment_panels(events, dates, ["600000"])["event_sentiment_decay"]

    assert panel.loc[dates[0], "600000"] == 0.0
    assert panel.loc[dates[1], "600000"] == 1.0


def test_dragon_tiger_factor_is_normalized_by_disclosed_turnover():
    dates = pd.bdate_range("2025-01-02", periods=4)
    events = pd.DataFrame(
        {
            "symbol": ["600000"],
            "published_at": ["2025-01-02 16:00:00"],
            "net_buy_amount": [20.0],
            "institution_net_buy_amount": [5.0],
            "turnover_amount": [100.0],
        }
    )
    panels = dragon_tiger_panels(events, dates, ["600000"])

    assert panels["dragon_tiger_net_buy_decay"].loc[dates[1], "600000"] == np.float32(0.2)
    assert panels["dragon_tiger_institution_decay"].loc[dates[1], "600000"] == np.float32(0.05)


def _sale_market(n_days: int) -> tuple[pd.DatetimeIndex, pd.DataFrame]:
    """构造 600000 持续 3% 拉升、600001/600002 平走的收益面板。"""
    dates = pd.bdate_range("2025-01-02", periods=n_days)
    close = pd.DataFrame(10.0, index=dates, columns=["600000", "600001", "600002"])
    close["600000"] = 10.0 * (1.03 ** np.arange(n_days))  # 持续 3% 拉升
    close["600001"] = 10.0  # 平走
    close["600002"] = 10.0
    returns = close.pct_change(fill_method=None)
    return dates, returns


def test_director_sale_active_mask_is_nan_outside_window():
    """无减持事件的股票-日期必须为 NaN，而不是 0（选择性披露）。"""
    dates, returns = _sale_market(8)
    events = pd.DataFrame({
        "symbol": ["600000"],
        "published_at": ["2025-01-02"],
        "title": ["关于董事减持股份预披露的公告"],
    })
    panels = director_sale_support_panels(events, dates, ["600000", "600001", "600002"],
                                          returns=returns, support_window=3)
    active = panels["director_sale_active"]
    # 600001/600002 无事件 → 全 NaN
    assert np.isnan(active.loc[dates[3], "600001"])
    assert np.isnan(active.loc[dates[3], "600002"])
    # 600000 无事件的早期日也是 NaN（尚未披露）
    assert np.isnan(active.loc[dates[0], "600000"])
    # 事件披露次日可用（无时分秒 → 下一交易日），窗口内为 1.0
    assert active.loc[dates[1], "600000"] == np.float32(1.0)


def test_director_sale_support_no_future_leakage():
    """拉升度量逐日累计：T 日信号不得包含 T 之后的量价。

    横截面均值基于传入的全部 symbols（审计管道传全市场）；600000 拉升
    3%/日、600001/600002 平走 → 每日 rel(600000)=3%-1%=2%。
    """
    dates, returns = _sale_market(8)
    events = pd.DataFrame({
        "symbol": ["600000"],
        "published_at": ["2025-01-02"],
        "title": ["关于董事减持股份预披露的公告"],
    })
    symbols = ["600000", "600001", "600002"]
    panels = director_sale_support_panels(events, dates, symbols, returns=returns,
                                          support_window=5)
    support = panels["director_sale_support_neg"]
    # 窗口从 1-03（index=1）起：逐日累计相对收益
    # 1-03: rel=2% → 累计 2%；1-06: 累计 4%；1-07: 累计 6%
    # 关键断言：某天的值 = 截至该天的累计，而非窗口末尾的总额
    v_day2 = support.loc[dates[1], "600000"]  # 累计 2%
    v_day3 = support.loc[dates[2], "600000"]  # 累计 4%
    assert np.isclose(v_day2, 0.02, atol=1e-3)
    assert np.isclose(v_day3, 0.04, atol=1e-3)


def test_director_sale_support_direction_positive_when_pumped():
    """窗口内被拉抬（相对强度为正）→ 因子值为正（负向信号：该回避）。"""
    dates, returns = _sale_market(8)
    events = pd.DataFrame({
        "symbol": ["600000"],  # 持续拉升
        "published_at": ["2025-01-02"],
        "title": ["关于董事减持股份预披露的公告"],
    })
    symbols = ["600000", "600001", "600002"]
    panels = director_sale_support_panels(events, dates, symbols, returns=returns,
                                          support_window=5)
    support = panels["director_sale_support_neg"]
    # 拉升股在窗口内的相对强度为正 → 因子正值（越高越该回避）
    assert support.loc[dates[2], "600000"] > 0.03
    # 因子本身不 active 之外的日期为 NaN
    assert np.isnan(support.loc[dates[6], "600000"])


def test_director_sale_filters_non_director_reduction():
    """非内部人减持（如财务投资者）不应触发拉升因子。"""
    dates, returns = _sale_market(8)
    events = pd.DataFrame({
        "symbol": ["600000", "600001"],
        "published_at": ["2025-01-02", "2025-01-02"],
        "title": [
            "关于股东减持股份预披露的公告",  # 无董事/高管/实控人关键词
            "关于董事减持股份预披露的公告",
        ],
    })
    panels = director_sale_support_panels(events, dates, ["600000", "600001"],
                                          returns=returns, support_window=3)
    active = panels["director_sale_active"]
    # 600000 非内部人 → 不进入事件窗口
    assert np.isnan(active.loc[dates[1], "600000"])
    # 600001 含"董事" → 进入
    assert active.loc[dates[1], "600001"] == np.float32(1.0)


def test_director_sale_regex_covered_personas():
    """内部人正则覆盖董事/高管/监事/实控人，但**排除**持股5%以上（财务投资者）。"""
    assert re.search(DIRECTOR_SALE_PERSON_REGEX, "董事减持公告")
    assert re.search(DIRECTOR_SALE_PERSON_REGEX, "高级管理人员减持")
    assert re.search(DIRECTOR_SALE_PERSON_REGEX, "实际控制人减持")
    assert re.search(DIRECTOR_SALE_PERSON_REGEX, "监事减持")
    # "持股5%以上股东"是财务/战略投资者，不是内部人，必须排除
    assert not re.search(DIRECTOR_SALE_PERSON_REGEX, "持股5%以上股东减持")
    assert not re.search(DIRECTOR_SALE_PERSON_REGEX, "股东减持公告")

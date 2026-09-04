"""small_value 策略契约测试：月度轮动 Top10 等权 + 避雷过滤 + PIT 财务过滤 + 日历空仓 + 状态序列化。

辅助数据（fundamental/financials/上市日）通过 monkeypatch `_load_aux` 注入合成帧，
不依赖真实 data/factors 文件。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.backtest.engine import MarketData
from quart.execution.constraints import FLAT
from quart.strategy import REGISTRY, build_strategy
from quart.strategy.small_value import SmallValueStrategy

SYMS = [f"60{i:04d}" for i in range(50)] + ["830001"]  # 50 只主板 + 1 只北交所
DATES = pd.bdate_range("2023-07-03", "2024-05-31")


def _bars() -> MarketData:
    dates = DATES
    close = pd.DataFrame(10.0, index=dates, columns=SYMS)  # 价 10 元：3~25 区间内
    rows = []
    for symbol in SYMS:
        rows.extend(
            {
                "date": date, "symbol": symbol,
                "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
                "volume": 1_000_000.0, "amount": 100_000_000.0,  # 1 亿 > 5000 万门槛
            }
            for date in dates
        )
    return MarketData.from_bars(pd.DataFrame(rows))


def _aux() -> dict[str, pd.DataFrame]:
    n = len(DATES)
    fmcap = pd.DataFrame(30e8, index=DATES, columns=SYMS)  # 30 亿：15~60 亿区间内
    # 制造避雷/过滤差异
    fmcap["600001"] = 100e8   # 超市值上限 → 剔除
    fmcap["600002"] = 10e8    # 低于市值下限 → 剔除
    fmcap["600003"] = 30e8
    fmcap.loc[DATES[-80:], "600003"] = 8e8   # 近 60 日市值最低 < 12 亿 → 剔除
    turn = pd.DataFrame(1.0, index=DATES, columns=SYMS)
    st = pd.DataFrame(0.0, index=DATES, columns=SYMS)
    st["600004"] = 1.0        # 当前 ST → 剔除
    fin = pd.DataFrame(
        {
            "symbol": SYMS,
            "announcement_date": pd.Timestamp("2023-01-01"),
            "revenue": 5e8,
            "net_profit": 1e8,
            "roe": 5.0,
        }
    )
    # 财务过滤：600005 营收不达标、600006 亏损 → 剔除
    fin.loc[fin["symbol"] == "600005", "revenue"] = 1e8
    fin.loc[fin["symbol"] == "600006", "net_profit"] = -1e8
    # 打分差异化：600010~600019 的 ROE=30（其余 5）→ use_score 下应入选
    fin.loc[fin["symbol"].isin([f"60001{k}" for k in range(10)]), "roe"] = 30.0
    listed = pd.Series(pd.Timestamp("2020-01-01"), index=SYMS)  # 上市 > 2 年
    listed["830001"] = pd.Timestamp("2020-01-01")  # 北交所由前缀剔除
    return {"fmcap": fmcap, "turn": turn, "st": st, "fin": fin, "listed_days": listed}


@pytest.fixture()
def strategy(monkeypatch) -> SmallValueStrategy:
    monkeypatch.setattr(SmallValueStrategy, "_load_aux", lambda self, dates: _aux())
    s = build_strategy("small_value")
    s.prepare(_bars())
    s._warm = 0
    return s


def test_registered_and_build():
    assert "small_value" in REGISTRY
    s = build_strategy("small_value")
    assert isinstance(s, SmallValueStrategy)
    assert s.params["n"] == 10
    assert s.params["use_score"] is True
    assert s.params["mcap_min"] == pytest.approx(1.5e9)  # settings.overrides 生效（15 亿）


def test_monthly_rebalance_top10_equal_weight(strategy):
    """非空仓月首个交易日：Top10 等权 0.1；北交所与问题票不入选。"""
    found = None
    for i in range(65, len(DATES)):
        if int(strategy.day_no.iloc[i]) != 1:
            continue
        w = strategy.target_weights(i)
        if w and FLAT not in w:
            found = (i, w)
            break
    assert found is not None
    i, w = found
    assert len(w) == 10
    assert all(abs(v - 0.1) < 1e-6 for v in w.values())
    assert all(v < 0.999 for k, v in w.items() if k != FLAT)
    picks = set(w)
    assert "830001" not in picks  # 北交所前缀剔除
    assert "600001" not in picks  # 市值超上限
    assert "600002" not in picks  # 市值低于下限
    assert "600004" not in picks  # ST
    assert "600005" not in picks  # 营收不达标
    assert "600006" not in picks  # 亏损


def test_score_prefers_high_roe(strategy):
    """use_score=True：ROE 30 的 10 只应全部入选（打分生效）。"""
    picks: set[str] | None = None
    for i in range(65, len(DATES)):
        if int(strategy.day_no.iloc[i]) != 1:
            continue
        w = strategy.target_weights(i)
        if w and FLAT not in w:
            picks = set(w)
            break
    assert picks is not None
    high_roe = {f"60001{k}" for k in range(10)}
    assert high_roe <= picks, f"ROE 最高的 10 只应全部入选: missing={high_roe - picks}"


def test_calendar_flat_january_and_late_april(strategy):
    """1 月整月与 4 月 20 日后空仓（唯一实证正贡献风控项）。"""
    # 12 月调仓日先建仓
    dec_first = next(i for i in range(65, len(DATES))
                     if DATES[i].year == 2023 and DATES[i].month == 12
                     and int(strategy.day_no.iloc[i]) == 1)
    w_dec = strategy.target_weights(dec_first)
    assert w_dec and FLAT not in w_dec and len(w_dec) == 10
    jan_i = [i for i, d in enumerate(DATES) if d.year == 2024 and d.month == 1 and i > 65]
    assert jan_i
    assert strategy.target_weights(jan_i[0]) == {FLAT: 1.0}
    # 空仓期持续：持仓已清 → 返回 {}（保持空仓）
    assert strategy.target_weights(jan_i[-1]) == {}
    # 4 月调仓日正常建仓，4/20 后非调仓日清仓
    apr_first = next(i for i, d in enumerate(DATES)
                     if d.year == 2024 and d.month == 4)
    w_apr = strategy.target_weights(apr_first)
    assert w_apr and FLAT not in w_apr
    late_apr = next(i for i, d in enumerate(DATES)
                    if d.year == 2024 and d.month == 4 and d.day >= 21)
    assert strategy.target_weights(late_apr) == {FLAT: 1.0}
    # 5 月首个调仓日恢复建仓
    may_first = next(i for i, d in enumerate(DATES) if d.year == 2024 and d.month == 5)
    w_may = strategy.target_weights(may_first)
    assert w_may and FLAT not in w_may and len(w_may) == 10


def test_non_rebalance_day_keeps_positions(strategy):
    """非调仓日（非空仓期）返回 {} 引擎保持持仓。"""
    strategy.target_weights(65)
    non_rebal = [i for i in range(66, len(DATES))
                 if int(strategy.day_no.iloc[i]) != 1
                 and not strategy._calendar_flat(DATES[i])]
    assert non_rebal
    assert strategy.target_weights(non_rebal[0]) == {}


def test_state_roundtrip(monkeypatch):
    monkeypatch.setattr(SmallValueStrategy, "_load_aux", lambda self, dates: _aux())
    s = build_strategy("small_value")
    s.prepare(_bars())
    s._warm = 0
    s.target_weights(65)
    state = s.state_dict()
    assert "held" in state
    s2 = build_strategy("small_value")
    s2.prepare(_bars())
    s2.load_state_dict(state)
    assert s2._held == s._held


def test_unknown_param_filtered():
    """未知参数被 build_strategy 过滤（_filter_to_schema），不影响构建。"""
    s = build_strategy("small_value", nonexistent_param=1)
    assert "nonexistent_param" not in s.params
    assert s.params["n"] == 10

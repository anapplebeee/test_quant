"""RULE-001 验收：按日期版本化规则引擎（历史日期规则测试）。"""
from __future__ import annotations

import pytest

from quart.market_rules.rule_book import (
    BSE_OPEN,
    STAMP_TAX_CUT,
    STAR_MARKET_OPEN,
    RuleBook,
    RuleSet,
    default_rule_book,
    load_rule_book_version,
    stamp_tax_as_of,
)


@pytest.fixture(scope="module")
def book() -> RuleBook:
    return default_rule_book()


# ---------------- 历史日期解析 ----------------


def test_chinext_limit_changed_at_registration_reform(book):
    """创业板涨跌幅 2020-08-24 改革当天从 10% 变为 20%（含头不含尾）。"""
    before = book.lookup("2020-08-21", exchange="SZSE", board="CHINEXT")
    on_day = book.lookup("2020-08-24", exchange="SZSE", board="CHINEXT")
    assert before is not None and before.price_limit_pct == 0.10
    assert on_day is not None and on_day.price_limit_pct == 0.20
    assert on_day.ipo_no_limit_days == 5
    assert before.ipo_no_limit_days == 0


def test_main_board_stays_10pct_but_ipo_phase_changed(book):
    """主板涨跌幅始终 10%，但注册制改革后新股前 5 日无涨跌幅。"""
    old = book.lookup("2015-06-01", exchange="SSE", board="MAIN")
    new = book.lookup("2024-01-02", exchange="SSE", board="MAIN")
    assert old.price_limit_pct == new.price_limit_pct == 0.10
    assert old.ipo_no_limit_days == 0
    assert old.ipo_first_day_limits == (0.44, 0.36)
    assert new.ipo_no_limit_days == 5
    assert new.ipo_first_day_limits is None


def test_star_market_absent_before_open(book):
    """科创板 2019-07-22 开市前没有规则，查询必须返回 None。"""
    assert book.lookup("2019-07-19", exchange="SSE", board="STAR") is None
    rule = book.lookup(STAR_MARKET_OPEN, exchange="SSE", board="STAR")
    assert rule is not None and rule.price_limit_pct == 0.20
    assert rule.ipo_no_limit_days == 5


def test_bse_absent_before_open(book):
    assert book.lookup("2021-11-12", exchange="BSE", board="BSE") is None
    rule = book.lookup(BSE_OPEN, exchange="BSE", board="BSE")
    assert rule is not None and rule.price_limit_pct == 0.30


def test_st_status_main_board_5pct(book):
    rule = book.lookup("2024-01-02", exchange="SSE", board="MAIN", status="st")
    assert rule is not None and rule.price_limit_pct == 0.05


def test_chinext_st_follows_reform(book):
    before = book.lookup("2020-01-06", exchange="SZSE", board="CHINEXT", status="st")
    after = book.lookup("2021-01-04", exchange="SZSE", board="CHINEXT", status="st")
    assert before.price_limit_pct == 0.05
    assert after.price_limit_pct == 0.20


def test_delisting_status_resolves(book):
    rule = book.lookup("2024-05-06", exchange="SZSE", board="MAIN", status="delisting")
    assert rule is not None and rule.price_limit_pct == 0.10


def test_unknown_key_returns_none(book):
    assert book.lookup("2024-01-02", exchange="HKEX", board="MAIN") is None


# ---------------- 代码解析 ----------------


def test_resolve_symbol_prefix_mapping(book):
    assert book.resolve_symbol("600519", "2024-01-02").board == "MAIN"
    assert book.resolve_symbol("300750", "2021-01-04").price_limit_pct == 0.20
    assert book.resolve_symbol("300750", "2019-01-02").price_limit_pct == 0.10
    assert book.resolve_symbol("688981", STAR_MARKET_OPEN).price_limit_pct == 0.20
    assert book.resolve_symbol("830799", BSE_OPEN).price_limit_pct == 0.30


def test_resolve_symbol_before_board_open(book):
    assert book.resolve_symbol("688981", "2018-01-02") is None


# ---------------- 涨跌幅计算与新股阶段 ----------------


def test_price_limits_normal(book):
    rule = book.lookup("2024-01-02", exchange="SSE", board="MAIN")
    up, down = book.price_limits(rule, 10.0)
    assert up == pytest.approx(11.0, abs=1e-6)
    assert down == pytest.approx(9.0, abs=1e-6)


def test_price_limits_ipo_no_limit_window(book):
    """注册制新股前 5 个交易日无涨跌幅 → None。"""
    rule = book.lookup("2024-01-02", exchange="SSE", board="MAIN")
    for age in range(5):
        assert book.price_limits(rule, 10.0, trading_age=age) is None
    assert book.price_limits(rule, 10.0, trading_age=5) is not None


def test_price_limits_legacy_first_day(book):
    """旧主板规则：首日 +44%/-36%，次日起 ±10%。"""
    rule = book.lookup("2015-06-01", exchange="SSE", board="MAIN")
    up, down = book.price_limits(rule, 10.0, trading_age=0)
    assert up == pytest.approx(14.4, abs=1e-6)
    assert down == pytest.approx(6.4, abs=1e-6)
    up, down = book.price_limits(rule, 10.0, trading_age=1)
    assert up == pytest.approx(11.0, abs=1e-6)
    assert down == pytest.approx(9.0, abs=1e-6)


def test_price_limits_unknown_age_falls_back_to_limit(book):
    """trading_age 未知时按非新股处理（调用方责任）。"""
    rule = book.lookup("2024-01-02", exchange="SSE", board="MAIN")
    assert book.price_limits(rule, 10.0, trading_age=None) is not None


def test_price_limits_invalid_prev_close(book):
    rule = book.lookup("2024-01-02", exchange="SSE", board="MAIN")
    assert book.price_limits(rule, float("nan")) is None
    assert book.price_limits(rule, -1.0) is None


# ---------------- 费用历史 ----------------


def test_stamp_tax_history():
    assert stamp_tax_as_of("2023-08-25") == 0.001
    assert stamp_tax_as_of(STAMP_TAX_CUT) == 0.0005
    assert stamp_tax_as_of("2026-01-05") == 0.0005


# ---------------- 不变量与完整性 ----------------


def test_default_book_validates(book):
    assert book.validate() == []


def test_overlap_detected():
    dup_a = RuleSet("SSE", "MAIN", "stock", "listed", None, None, 0.10)
    dup_b = RuleSet("SSE", "MAIN", "stock", "listed", None, None, 0.20)
    problems = RuleBook([dup_a, dup_b]).validate()
    assert any("overlap" in p for p in problems)


def test_lookup_ambiguity_raises():
    """区间重叠是数据错误：查询必须失败而不是任选其一。"""
    dup_a = RuleSet("SSE", "MAIN", "stock", "listed", None, None, 0.10)
    dup_b = RuleSet("SSE", "MAIN", "stock", "listed", None, None, 0.20)
    with pytest.raises(ValueError, match="overlapping"):
        RuleBook([dup_a, dup_b]).lookup("2024-01-02", exchange="SSE", board="MAIN")


def test_version_changes_with_rules(book):
    v1 = book.version()
    extra = RuleSet("SSE", "MAIN", "stock", "listed", None, None, 0.11)
    v2 = RuleBook([*book.rules, extra]).version()
    assert v1 != v2
    assert default_rule_book().version() == v1  # 确定性


def test_save_load_roundtrip(tmp_path):
    book = default_rule_book()
    path = book.save(tmp_path / "rule_book.json")
    loaded = RuleBook.load(path)
    assert loaded.version() == book.version()
    assert loaded.lookup("2020-08-24", exchange="SZSE", board="CHINEXT").price_limit_pct == 0.20


def test_load_rule_book_version_falls_back_to_default(tmp_path):
    assert load_rule_book_version(tmp_path / "missing.json") == default_rule_book().version()
    path = default_rule_book().save(tmp_path / "rule_book.json")
    assert load_rule_book_version(path) == default_rule_book().version()

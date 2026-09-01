"""RESEARCH-002 数据基建测试：财报 PIT / 公告事件 / 证券状态 PIT。

只测纯逻辑（不触网）：快照规范化、修订合并、事件分类与情绪、
主数据区间查询与校验。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.data.announcements import (
    build_event_frame,
    classify_event,
    merge_forecast_events,
    rule_sentiment,
)
from quart.data.financials_pit import (
    coverage_report,
    merge_flash_announcements,
    merge_revisions,
    normalize_yjbb,
)
from quart.data.security_master import SecurityMaster, is_st_at, listing_age_days


# ---------------- 财报 PIT ----------------

def _yjbb_raw(symbol="000001", eps=1.0, announce="2023-04-25"):
    return pd.DataFrame([{
        "股票代码": symbol, "每股收益": eps, "每股净资产": 10.0, "净资产收益率": 5.0,
        "销售毛利率": 20.0, "营业总收入-同比增长": 10.0, "净利润-同比增长": 15.0,
        "营业总收入-营业总收入": 1e9, "净利润-净利润": 1e8,
        "最新公告日期": announce,
    }])


def test_normalize_yjbb_maps_columns_and_guards_announce():
    snap = normalize_yjbb(_yjbb_raw(), "20230331", pd.Timestamp("2026-09-01"))
    assert snap.loc[0, "symbol"] == "000001"
    assert np.isclose(snap.loc[0, "eps"], 1.0)
    assert snap.loc[0, "date"] == pd.Timestamp("2023-03-31")
    assert snap.loc[0, "announcement_date"] == pd.Timestamp("2023-04-25")
    assert snap.loc[0, "available_at"] == pd.Timestamp("2026-09-01")


def test_normalize_yjbb_rejects_announce_before_period():
    """公告时间早于报告期 = 脏数据 → 置空交给可用时点链兜底。"""
    snap = normalize_yjbb(_yjbb_raw(announce="2022-12-01"), "20230331", pd.Timestamp.now())
    assert pd.isna(snap.loc[0, "announcement_date"])


def test_merge_revisions_new_unchanged_changed():
    fetched = pd.Timestamp("2026-09-01")
    snap1 = normalize_yjbb(_yjbb_raw(), "20230331", fetched)
    main1, rev1 = merge_revisions(pd.DataFrame(), pd.DataFrame(), snap1)
    assert len(main1) == 1 and main1.loc[0, "revision"] == 1 and rev1.empty

    # 完全一致 → revision 不变
    snap2 = normalize_yjbb(_yjbb_raw(), "20230331", pd.Timestamp("2026-09-02"))
    main2, rev2 = merge_revisions(main1, rev1, snap2)
    assert main2.loc[0, "revision"] == 1 and rev2.empty

    # 数值变化 → revision+1，旧版写入修订日志
    snap3 = normalize_yjbb(_yjbb_raw(eps=2.0), "20230331", pd.Timestamp("2026-09-03"))
    main3, rev3 = merge_revisions(main2, rev2, snap3)
    assert main3.loc[0, "revision"] == 2 and main3.loc[0, "eps"] == 2.0
    assert len(rev3) == 1 and rev3.loc[0, "eps"] == 1.0 and rev3.loc[0, "revision"] == 1


def test_merge_revisions_keeps_other_periods():
    p1 = normalize_yjbb(_yjbb_raw(), "20230331", pd.Timestamp.now())
    p2 = normalize_yjbb(_yjbb_raw(), "20230630", pd.Timestamp.now())
    m1, _ = merge_revisions(pd.DataFrame(), pd.DataFrame(), p1)
    m2, _ = merge_revisions(m1, pd.DataFrame(), p2)
    assert len(m2) == 2


def test_merge_flash_announcements_earlier_date_wins():
    report = normalize_yjbb(_yjbb_raw(announce="2023-04-25"), "20230331", pd.Timestamp.now())
    flash = pd.DataFrame([{
        "symbol": "000001", "date": pd.Timestamp("2023-03-31"),
        "flash_announce_date": pd.Timestamp("2023-03-15"),
    }])
    merged = merge_flash_announcements(report, flash)
    assert merged.loc[0, "announcement_date"] == pd.Timestamp("2023-03-15")
    assert merged.loc[0, "announce_source"] == "yjkb_flash"

    # 快报晚于正式报告 → 不采用
    flash_late = flash.assign(flash_announce_date=pd.Timestamp("2023-05-01"))
    merged2 = merge_flash_announcements(report, flash_late)
    assert merged2.loc[0, "announcement_date"] == pd.Timestamp("2023-04-25")
    assert merged2.loc[0, "announce_source"] == "yjbb_report"


def test_coverage_report_pct():
    fin = pd.DataFrame([
        {"symbol": "000001", "date": "2023-03-31", "announcement_date": "2023-04-25"},
        {"symbol": "000002", "date": "2023-03-31", "announcement_date": pd.NaT},
        {"symbol": "000001", "date": "2023-06-30", "announcement_date": "2023-08-25"},
    ])
    rep = coverage_report(fin, ["000001", "000002", "000003", "000004"])
    assert rep.loc[0, "symbols"] == 2 and np.isclose(rep.loc[0, "coverage_pct"], 0.5)
    assert rep.loc[0, "with_announce_date"] == 1


# ---------------- 公告事件 ----------------

def test_classify_event_types():
    assert classify_event("关于回购股份的公告") == "buyback"
    assert classify_event("收到证监会立案告知书") == "penalty"
    assert classify_event("2025年半年度业绩预告") == "earnings_forecast"
    assert classify_event("2025年年度业绩快报") == "earnings_flash"
    assert classify_event("利润分配及派息公告") == "dividend"
    assert classify_event("股东减持计划公告") == "share_reduction"
    assert classify_event("重大诉讼进展") == "lawsuit"
    assert classify_event("日常经营公告") == "other"


def test_rule_sentiment_directions():
    assert rule_sentiment("预计2025年净利润同比增长50%")[0] == 1
    assert rule_sentiment("2025年年度业绩预告：预亏")[0] == -1
    assert rule_sentiment("收到深交所监管函")[0] == -1
    assert rule_sentiment("控股股东增持计划")[0] == 1
    assert rule_sentiment("关于召开股东大会的通知")[0] == 0  # other → 中性
    # 置信度在 [0,1]
    s, c = rule_sentiment("收到深交所监管函")
    assert 0 <= c <= 1


def test_build_event_frame_contract():
    raw = pd.DataFrame([
        {"代码": "1", "公告标题": "回购股份方案", "公告日期": "2025-08-29"},
        {"代码": "2", "公告标题": "日常关联交易", "公告日期": "2025-08-29"},  # other → 过滤
        {"代码": "3", "公告标题": "收到监管函", "公告日期": "bad-date"},  # 日期非法 → 过滤
    ])
    out = build_event_frame(raw, pd.Timestamp("2026-09-01"))
    # 事件合同必需列（§3.4）：symbol/published_at/sentiment 必需，
    # confidence/relevance/available_at 可选增强
    for col in ("symbol", "published_at", "sentiment", "confidence", "relevance", "available_at"):
        assert col in out.columns
    assert out.loc[0, "symbol"] == "000001"
    assert out.loc[0, "event_type"] == "buyback"
    assert out.loc[0, "sentiment"] == 1.0
    assert len(out) == 1  # other 与非法日期被过滤


def test_merge_forecast_events_direction():
    raw = pd.DataFrame([
        {"股票代码": "300001", "预告类型": "预增", "业绩变动": "预计增长80%", "公告日期": "2025-07-10"},
        {"股票代码": "300002", "预告类型": "首亏", "业绩变动": None, "公告日期": "2025-07-11"},
    ])
    out = merge_forecast_events(raw)
    assert out.loc[0, "rule_sentiment"] == 1 and out.loc[1, "rule_sentiment"] == -1
    assert (out["event_type"] == "earnings_forecast").all()
    assert (out["confidence"] == 0.9).all()


# ---------------- 证券状态 PIT ----------------

def _master():
    rows = [
        {"symbol": "000001", "exchange": "SZSE", "board": "MAIN", "security_type": "stock",
         "listed_at": "1991-04-03", "delisted_at": pd.NaT, "status": "listed",
         "status_effective_from": "1991-04-03", "status_effective_to": pd.NaT,
         "lot_size": 100, "tick_size": 0.01, "price_limit_rule": 0.10, "settlement_rule": "T+1"},
        {"symbol": "000001", "exchange": "SZSE", "board": "MAIN", "security_type": "stock",
         "listed_at": "1991-04-03", "delisted_at": pd.NaT, "status": "st",
         "status_effective_from": "2023-05-01", "status_effective_to": "2024-06-01",
         "lot_size": 100, "tick_size": 0.01, "price_limit_rule": 0.05, "settlement_rule": "T+1"},
        {"symbol": "600999", "exchange": "SSE", "board": "MAIN", "security_type": "stock",
         "listed_at": "2010-01-01", "delisted_at": "2020-01-01", "status": "listed",
         "status_effective_from": "2010-01-01", "status_effective_to": pd.NaT,
         "lot_size": 100, "tick_size": 0.01, "price_limit_rule": 0.10, "settlement_rule": "T+1"},
        {"symbol": "600999", "exchange": "SSE", "board": "MAIN", "security_type": "stock",
         "listed_at": "2010-01-01", "delisted_at": "2020-01-01", "status": "delisted",
         "status_effective_from": "2020-01-01", "status_effective_to": pd.NaT,
         "lot_size": 100, "tick_size": 0.01, "price_limit_rule": 0.10, "settlement_rule": "T+1"},
    ]
    return SecurityMaster(pd.DataFrame(rows))


def test_is_st_at_window_semantics():
    m = _master()
    # 含头不含尾：起点日生效，终点日失效
    assert is_st_at(m, "000001", "2023-05-01")
    assert is_st_at(m, "000001", "2024-05-31")
    assert not is_st_at(m, "000001", "2024-06-01")
    assert not is_st_at(m, "000001", "2023-04-30")


def test_as_of_excludes_future_listing_and_delisted():
    m = _master()
    # 1991-04-03 当天 000001 已上市
    assert "000001" in set(m.as_of("1991-04-03")["symbol"])
    # 退市后不可见
    assert "600999" in set(m.as_of("2015-01-01")["symbol"])
    assert "600999" not in set(m.as_of("2021-01-01")["symbol"])


def test_listing_age_days_pit():
    m = _master()
    assert listing_age_days(m, "000001", "2023-05-02") == (pd.Timestamp("2023-05-02")
                                                           - pd.Timestamp("1991-04-03")).days
    assert listing_age_days(m, "000001", "1991-04-02") is None  # 未上市
    assert listing_age_days(m, "999999", "2024-01-01") is None  # 未知


def test_validate_detects_overlapping_intervals():
    rows = [
        {"symbol": "000001", "exchange": "SZSE", "board": "MAIN", "security_type": "stock",
         "listed_at": "1991-04-03", "delisted_at": pd.NaT, "status": "st",
         "status_effective_from": "2023-05-01", "status_effective_to": pd.NaT,
         "lot_size": 100, "tick_size": 0.01, "price_limit_rule": 0.05, "settlement_rule": "T+1"},
        {"symbol": "000001", "exchange": "SZSE", "board": "MAIN", "security_type": "stock",
         "listed_at": "1991-04-03", "delisted_at": pd.NaT, "status": "st",
         "status_effective_from": "2023-06-01", "status_effective_to": pd.NaT,
         "lot_size": 100, "tick_size": 0.01, "price_limit_rule": 0.05, "settlement_rule": "T+1"},
    ]
    problems = SecurityMaster(pd.DataFrame(rows)).validate()
    assert any("overlapping" in p for p in problems)


def test_version_changes_with_content():
    m1, m2 = _master(), _master()
    assert m1.version() == m2.version()
    rows = m2.table.copy()
    rows.loc[rows["status"] == "st", "status_effective_to"] = pd.Timestamp("2025-01-01")
    assert SecurityMaster(rows).version() != m1.version()


def test_is_suspended_at(tmp_path, monkeypatch):
    from quart.data import security_master as sm_mod

    sus = pd.DataFrame([
        {"snap_date": "2024-07-01", "symbol": "000003",
         "suspend_from": pd.Timestamp("2024-07-01"), "resume_at": pd.Timestamp("2024-07-10"),
         "reason": "重大事项"},
        {"snap_date": "2024-08-01", "symbol": "000004",
         "suspend_from": pd.NaT, "resume_at": pd.NaT, "reason": "待披露"},
    ])
    monkeypatch.setattr(sm_mod, "suspension_intervals", lambda path=None: sus)
    assert sm_mod.is_suspended_at("000003", "2024-07-05", sus)
    assert not sm_mod.is_suspended_at("000003", "2024-07-10", sus)  # 含头不含尾
    assert sm_mod.is_suspended_at("000004", "2024-09-01", sus)  # 未复牌一直生效
    assert not sm_mod.is_suspended_at("000001", "2024-07-05", sus)

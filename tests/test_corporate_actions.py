"""DATA-002：公司行为 PIT 账本与快照绑定。"""

from __future__ import annotations

import pandas as pd
import pytest

from quart.data import snapshot as snap
from quart.data.corporate_actions import (
    CorporateActionLedger,
    load_corporate_action_version,
    normalize_corporate_actions,
)


def _raw_actions() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "股票代码": "1",
                "行为类型": "现金分红",
                "公告日期": "2024-01-05",
                "除权除息日": "2024-01-12",
                "每股派息": "0.20",
                "公告编号": "2024-001",
            },
            {
                "股票代码": "600000",
                "行为类型": "送股",
                "公告日期": "2024-02-01",
                "除权日": "2024-02-15",
                "送股比例": "0.10",
                "公告编号": "2024-002",
            },
        ]
    )


def test_normalize_and_query_actions_point_in_time():
    ledger = CorporateActionLedger(normalize_corporate_actions(_raw_actions(), source="cninfo"))
    dividend = ledger.table.iloc[0]
    assert dividend["symbol"] == "000001"
    assert dividend["action_type"] == "cash_dividend"
    # 日期级公告没有精确时刻时，最早于次日可用于决策。
    assert dividend["available_at"] == pd.Timestamp("2024-01-06")
    assert ledger.as_of("2024-01-05").empty
    assert len(ledger.as_of("2024-01-06", "000001")) == 1
    assert len(ledger.effective_on("2024-01-12", "000001")) == 1
    assert ledger.effective_on("2024-01-12", "600000").empty


def test_ledger_rejects_impossible_availability():
    raw = _raw_actions().iloc[[0]].copy()
    raw["可用日期"] = "2024-01-04"
    with pytest.raises(ValueError, match="available_at earlier"):
        CorporateActionLedger(normalize_corporate_actions(raw, source="cninfo"))


def test_ledger_keeps_revisions_without_future_leakage():
    original = _raw_actions().iloc[[0]].copy()
    original["可用日期"] = "2024-01-06"
    original["修订日期"] = "2024-01-06"
    revised = original.copy()
    revised["每股派息"] = "0.30"
    revised["可用日期"] = "2024-01-20"
    revised["修订日期"] = "2024-01-20"
    ledger = CorporateActionLedger(
        pd.concat(
            [
                normalize_corporate_actions(original, source="cninfo", ingested_at="2024-01-06"),
                normalize_corporate_actions(revised, source="cninfo", ingested_at="2024-01-20"),
            ],
            ignore_index=True,
        )
    )
    assert len(ledger.table) == 2  # 同一 action_id 的两份修订均可审计
    assert ledger.as_of("2024-01-12").loc[0, "cash_per_share"] == 0.20
    assert ledger.as_of("2024-01-20").loc[0, "cash_per_share"] == 0.30


def test_ledger_version_persists_and_enters_snapshot_metadata(tmp_path):
    path = tmp_path / "meta" / "corporate_actions.parquet"
    ledger = CorporateActionLedger(normalize_corporate_actions(_raw_actions(), source="cninfo"))
    ledger.save(path)

    assert load_corporate_action_version(path) == ledger.version()
    meta = snap.collect_pit_metadata(tmp_path)
    assert meta["corporate_action_version"] == ledger.version()
    assert meta["corporate_actions"]["rows"] == 2
    assert meta["corporate_actions"]["symbols"] == 2

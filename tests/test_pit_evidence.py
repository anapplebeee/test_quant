"""DATA-002B：formal 运行的 PIT 证据门禁。"""

from __future__ import annotations

import pandas as pd
import pytest

from quart.data.corporate_actions import CorporateActionLedger, normalize_corporate_actions
from quart.data.pit_evidence import PITEvidenceError, check_pit_evidence, require_pit_evidence
from quart.data.security_master import SecurityMaster


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["000001", "000001", "600000", "600000"],
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-02", "2024-01-03"]),
        }
    )


def _prepare_pit_root(root) -> None:
    meta = root / "meta"
    universe = root / "universe"
    meta.mkdir()
    universe.mkdir()
    master = SecurityMaster(
        pd.DataFrame(
            [
                {"symbol": "000001", "listed_at": "1991-04-03", "status": "listed"},
                {"symbol": "600000", "listed_at": "1999-11-10", "status": "listed"},
            ]
        )
    )
    master.save(meta / "security_master.parquet")
    CorporateActionLedger(
        normalize_corporate_actions(
            pd.DataFrame(
                [
                    {
                        "symbol": "000001",
                        "action_type": "cash_dividend",
                        "announcement_date": "2023-06-01",
                        "available_at": "2023-06-02",
                        "ex_date": "2023-06-10",
                        "cash_per_share": 0.1,
                    }
                ]
            ),
            source="fixture",
            ingested_at="2023-06-02",
        )
    ).save(meta / "corporate_actions.parquet")
    pd.DataFrame(
        {
            "symbol": ["000001", "600000"],
            "in_date": [pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-01")],
            "out_date": [pd.NaT, pd.NaT],
        }
    ).to_parquet(universe / "000300_constituents_history.parquet", index=False)


def test_pit_evidence_passes_with_all_three_data_proofs(tmp_path):
    _prepare_pit_root(tmp_path)
    result = check_pit_evidence(_bars(), index_code="000300", root=tmp_path)
    assert result.passed, result.to_dict()
    assert result.metadata["security_master_version"]
    assert result.metadata["corporate_action_version"]


def test_pit_evidence_fails_when_company_actions_are_missing(tmp_path):
    _prepare_pit_root(tmp_path)
    (tmp_path / "meta" / "corporate_actions.parquet").unlink()
    result = check_pit_evidence(_bars(), index_code="000300", root=tmp_path)
    assert not result.passed
    assert any(issue.rule_id == "PIT-030" for issue in result.issues)
    with pytest.raises(PITEvidenceError, match="PIT-030"):
        require_pit_evidence(_bars(), index_code="000300", root=tmp_path)


def test_pit_evidence_detects_bar_outside_master_listing_interval(tmp_path):
    _prepare_pit_root(tmp_path)
    bars = _bars()
    bars.loc[0, "symbol"] = "300001"  # 主数据没有该证券，不能拿当前/近似状态回填
    result = check_pit_evidence(bars, index_code="000300", root=tmp_path)
    issue = next(issue for issue in result.issues if issue.rule_id == "PIT-022")
    assert issue.affected_rows == 1
    assert issue.samples == ("300001@2024-01-02",)

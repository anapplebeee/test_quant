"""券商成交 CSV 列映射与归一化导入测试。"""
from __future__ import annotations

import csv

import pytest

from quart.manual_trading.broker_profiles import (
    convert_broker_csv,
    convert_broker_xlsx,
    normalize_date,
    normalize_side,
    normalize_symbol,
)
from quart.manual_trading.io import import_broker_csv
from quart.manual_trading.repository import TradingRepository


def test_normalize_side_variants():
    assert normalize_side("买入") == "BUY"
    assert normalize_side("证券卖出") == "SELL"
    assert normalize_side("b") == "BUY"
    assert normalize_side("未知方向") == ""


def test_normalize_date_formats():
    assert normalize_date("2026-08-31") == "2026-08-31"
    assert normalize_date("2026/8/31") == "2026-08-31"
    assert normalize_date("20260831") == "2026-08-31"
    assert normalize_date("20260831093025") == "2026-08-31"
    with pytest.raises(ValueError, match="无法解析日期"):
        normalize_date("昨天")


def test_normalize_symbol_suffixes():
    assert normalize_symbol("600000.SH") == "600000"
    assert normalize_symbol("1.sz") == "000001"
    assert normalize_symbol("600519") == "600519"


def test_convert_broker_csv_ths_style(tmp_path):
    source = tmp_path / "broker.csv"
    source.write_text(
        "成交日期,证券代码,操作,成交数量,成交价格,成交编号\n"
        "20260831,600519,买入,100,1500.00,90001\n"
        "20260831,000001.SZ,证券卖出,200,12.50,90002\n",
        encoding="utf-8-sig",
    )
    target = convert_broker_csv(source, tmp_path / "normalized.csv")
    with target.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 2
    assert rows[0]["trade_date"] == "2026-08-31"
    assert rows[0]["symbol"] == "600519"
    assert rows[0]["side"] == "BUY"
    assert rows[1]["symbol"] == "000001"
    assert rows[1]["side"] == "SELL"


def test_convert_broker_csv_reports_unknown_columns(tmp_path):
    source = tmp_path / "broker.csv"
    source.write_text(
        "日期,代码,方向,数量,价格\n"
        "20260831,600519,买入,100,1500.00\n",
        encoding="utf-8",
    )
    # 列名全部是别名可识别的（"日期/代码/方向/数量/价格"）
    target = convert_broker_csv(source, tmp_path / "normalized.csv")
    assert target.exists()

    bad = tmp_path / "bad.csv"
    bad.write_text("/foo,/bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必需列"):
        convert_broker_csv(bad, tmp_path / "out.csv")


def test_import_broker_csv_records_fills(tmp_path):
    repository = TradingRepository(tmp_path / "trading.db")
    repository.initialize_schema()
    repository.initialize_account(cash=1_000_000, positions={}, as_of="2026-08-28")
    state = repository.account_state(as_of="2026-08-31")
    assert state is not None

    source = tmp_path / "broker.csv"
    source.write_text(
        "成交日期,证券代码,操作,成交数量,成交价格,成交编号\n"
        "20260831,600519,买入,100,1500.00,90001\n"
        "20260831,600000,买入,500,10.00,90002\n",
        encoding="utf-8-sig",
    )
    fill_ids = import_broker_csv(repository, state.account_id, source)
    assert len(fill_ids) == 2

    after = repository.account_state(as_of="2026-09-01")
    assert after is not None
    # 两笔买入：600519 100股 + 600000 500股，现金被扣减
    assert after.cash_total < 1_000_000
    assert after.total_positions == {"600519": 100, "600000": 500}

    # 重复导入同 broker_fill_id 会被拦截
    with pytest.raises(ValueError, match="成交编号重复"):
        import_broker_csv(repository, state.account_id, source)


def test_convert_broker_xlsx(tmp_path):
    """XLSX 券商成交表 → 通用模板 CSV（列名归一化 + 数字单元格兼容）。"""
    import openpyxl

    source = tmp_path / "broker.xlsx"
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = "成交明细"
    ws.append(["成交日期", "证券代码", "操作", "成交数量", "成交价格", "成交编号"])
    ws.append(["2026-08-31", "600519.SH", "买入", 100, 1500.00, "x1"])
    ws.append(["2026/8/31", "000001", "证券卖出", 200.0, 12.5, "x2"])
    workbook.save(source)

    target = convert_broker_xlsx(source, tmp_path / "normalized.csv")
    import csv

    with target.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 2
    assert rows[0]["symbol"] == "600519"
    assert rows[0]["side"] == "BUY"
    assert rows[0]["quantity"] == "100"
    assert rows[1]["trade_date"] == "2026-08-31"
    assert rows[1]["side"] == "SELL"
    assert rows[1]["quantity"] == "200"


def test_import_broker_fill_file_auto_detect(tmp_path):
    """import_broker_fill_file 按扩展名自动分流（xlsx 直接入账）。"""
    import openpyxl

    from quart.manual_trading.io import import_broker_fill_file

    repository = TradingRepository(tmp_path / "trading.db")
    repository.initialize_schema()
    repository.initialize_account(cash=1_000_000, positions={}, as_of="2026-08-28")
    state = repository.account_state(as_of="2026-08-31")
    assert state is not None

    source = tmp_path / "broker.xlsx"
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.append(["成交日期", "证券代码", "操作", "成交数量", "成交价格", "成交编号"])
    ws.append(["2026-08-31", "600519", "买入", 100, 1500.00, "xlsx-1"])
    workbook.save(source)

    fill_ids = import_broker_fill_file(repository, state.account_id, source)
    assert len(fill_ids) == 1
    assert repository.account_state(as_of="2026-09-01").total_positions == {"600519": 100}

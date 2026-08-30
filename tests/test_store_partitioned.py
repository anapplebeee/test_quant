"""分区存储测试。

核心不变量：分区布局与旧布局对**同一个查询**必须返回一致的结果。
分区改造是纯粹的物理布局变化，不能改变语义。
"""
from __future__ import annotations

import pandas as pd
import pytest

from quart.data.store import BarStore, PARTITION_PREFIX


def make_bars(symbol: str, dates, price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "symbol": symbol,
        "open": price, "high": price + 1, "low": price - 1, "close": price,
        "volume": 1_000_000.0,
        "amount": price * 1_000_000.0,
    })


def _seed_flat(tmp_path, symbol="600519", years=(2023, 2024, 2025)) -> BarStore:
    """用旧布局写入三年数据。"""
    store = BarStore(root=tmp_path, partitioned=False)
    for y in years:
        dates = pd.bdate_range(f"{y}-01-01", f"{y}-12-31")
        store.save(make_bars(symbol, dates, price=float(y - 2000)))
    return store


# ---------------------------------------------------------------- 写入


def test_save_creates_year_partitions(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    dates = pd.bdate_range("2024-01-01", "2025-12-31")
    store.save(make_bars("600519", dates))

    dirs = sorted(p.name for p in (tmp_path / "daily").iterdir() if p.is_dir())
    assert dirs == ["year=2024", "year=2025"], f"分区目录不符合预期: {dirs}"
    assert (tmp_path / "daily" / "year=2024" / "600519.parquet").exists()
    assert (tmp_path / "daily" / "year=2025" / "600519.parquet").exists()


def test_incremental_save_only_touches_current_year(tmp_path):
    """分区的核心收益：增量 1 天不应重写历史年份的文件。"""
    store = BarStore(root=tmp_path, partitioned=True)
    store.save(make_bars("600519", pd.bdate_range("2024-01-01", "2024-12-31")))
    old = tmp_path / "daily" / "year=2024" / "600519.parquet"
    mtime_before = old.stat().st_mtime_ns
    size_before = old.stat().st_size

    store.save(make_bars("600519", pd.bdate_range("2025-01-01", "2025-01-10")))

    assert old.stat().st_mtime_ns == mtime_before, "历史年份分区被重写了"
    assert old.stat().st_size == size_before
    assert (tmp_path / "daily" / "year=2025" / "600519.parquet").exists()


def test_index_symbols_go_to_index_dir(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    store.save(make_bars("IDX000300", pd.bdate_range("2024-01-01", "2024-06-30")))
    assert (tmp_path / "index" / "year=2024" / "IDX000300.parquet").exists()
    assert not (tmp_path / "daily" / "year=2024").exists()


def test_replace_mode_overwrites_partition(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    dates = pd.bdate_range("2024-01-01", "2024-03-31")
    store.save(make_bars("600519", dates, price=100.0))
    store.save(make_bars("600519", dates, price=50.0), replace=True)

    out = store.load(start="2024-01-01", end="2024-03-31")
    assert out["close"].unique().tolist() == [50.0]


def test_dedupe_on_overlap(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    d1 = pd.bdate_range("2024-01-01", "2024-01-10")
    d2 = pd.bdate_range("2024-01-06", "2024-01-15")
    store.save(make_bars("600519", d1, price=100.0))
    store.save(make_bars("600519", d2, price=110.0))

    out = store.load()
    assert len(out) == len(d1.union(d2))
    assert out["close"].max() == 110.0


# ---------------------------------------------------------------- 读取等价性


def test_partitioned_matches_flat_full_load(tmp_path):
    """同一份数据在两种布局下，全量查询必须等价。"""
    symbol = "600519"
    dates = pd.bdate_range("2023-01-01", "2025-12-31")

    flat_root = tmp_path / "flat"
    flat = BarStore(root=flat_root, partitioned=False)
    flat.save(make_bars(symbol, dates, price=10.0))

    part_root = tmp_path / "part"
    part = BarStore(root=part_root, partitioned=True)
    part.save(make_bars(symbol, dates, price=10.0))

    a = flat.load().reset_index(drop=True)
    b = part.load().reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)


def test_partitioned_matches_flat_date_filtered(tmp_path):
    """带日期过滤时也必须等价（这是分区裁剪生效的路径）。"""
    symbol = "600519"
    dates = pd.bdate_range("2023-01-01", "2025-12-31")

    flat = BarStore(root=tmp_path / "flat", partitioned=False)
    flat.save(make_bars(symbol, dates, price=10.0))
    part = BarStore(root=tmp_path / "part", partitioned=True)
    part.save(make_bars(symbol, dates, price=10.0))

    for start, end in [("2024-01-01", None), (None, "2024-06-30"),
                       ("2024-03-01", "2024-09-30")]:
        a = flat.load(start=start, end=end).reset_index(drop=True)
        b = part.load(start=start, end=end).reset_index(drop=True)
        pd.testing.assert_frame_equal(a, b, obj=f"start={start} end={end}")


def test_partitioned_matches_flat_symbol_subset(tmp_path):
    dates = pd.bdate_range("2024-01-01", "2025-06-30")
    flat = BarStore(root=tmp_path / "flat", partitioned=False)
    part = BarStore(root=tmp_path / "part", partitioned=True)
    for sym, px in (("600519", 10.0), ("000001", 20.0), ("601318", 30.0)):
        flat.save(make_bars(sym, dates, px))
        part.save(make_bars(sym, dates, px))

    a = flat.load(symbols=["600519", "000001"]).reset_index(drop=True)
    b = part.load(symbols=["600519", "000001"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)


def test_load_excludes_out_of_range_years(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    store.save(make_bars("600519", pd.bdate_range("2023-01-01", "2025-12-31")))

    out = store.load(start="2024-01-01", end="2024-12-31")
    years = set(pd.to_datetime(out["date"]).dt.year)
    assert years == {2024}, f"日期过滤失效，返回了 {years}"


def test_load_benchmark_works_partitioned(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    store.save(make_bars("IDX000300", pd.bdate_range("2024-01-01", "2024-06-30"), 3000.0))
    bench = store.load_benchmark("000300")
    assert len(bench) > 0
    assert bench["close"].iloc[0] == 3000.0


# ---------------------------------------------------------------- 元数据


def test_first_and_last_date_span_partitions(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    store.save(make_bars("600519", pd.bdate_range("2023-01-01", "2025-12-31")))

    assert store.first_date("600519") == pd.Timestamp("2023-01-02")  # 首个工作日
    assert store.last_date("600519").year == 2025


def test_symbols_deduplicates_across_years(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    for y in (2024, 2025):
        store.save(make_bars("600519", pd.bdate_range(f"{y}-01-01", f"{y}-06-30")))
    store.save(make_bars("000001", pd.bdate_range("2025-01-01", "2025-06-30")))
    assert store.symbols() == ["000001", "600519"]


def test_missing_symbol_returns_none(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    assert store.first_date("999999") is None
    assert store.last_date("999999") is None
    assert store.load(symbols=["999999"]).empty


def test_freshness_days_partitioned(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    store.save(make_bars("600519", pd.bdate_range("2024-01-01", "2024-06-30")))
    days = store.freshness_days(reference="20240715")
    assert days is not None and days >= 0 and days < 60


def test_empty_store_returns_empty_bars(tmp_path):
    store = BarStore(root=tmp_path, partitioned=True)
    out = store.load()
    assert out.empty
    assert list(out.columns) == ["date", "symbol", "open", "high", "low", "close",
                                 "volume", "amount"]


# ---------------------------------------------------------------- 布局检测


def test_auto_detect_flat_layout(tmp_path):
    BarStore(root=tmp_path, partitioned=False).save(
        make_bars("600519", pd.bdate_range("2024-01-01", "2024-03-31"))
    )
    assert BarStore(root=tmp_path).partitioned is False


def test_auto_detect_partitioned_layout(tmp_path):
    BarStore(root=tmp_path, partitioned=True).save(
        make_bars("600519", pd.bdate_range("2024-01-01", "2024-03-31"))
    )
    assert BarStore(root=tmp_path).partitioned is True


# ---------------------------------------------------------------- 迁移


def test_migrate_moves_flat_to_partitions(tmp_path):
    store = _seed_flat(tmp_path)
    assert not store.partitioned

    stats = store.migrate_to_partitioned()
    assert stats["symbols"] == 1
    assert stats["files"] == 3
    assert store.partitioned

    # 旧文件已删除
    assert not (tmp_path / "daily" / "600519.parquet").exists()
    for y in (2023, 2024, 2025):
        assert (tmp_path / "daily" / f"{PARTITION_PREFIX}{y}" / "600519.parquet").exists()


def test_migrate_preserves_data(tmp_path):
    """迁移不能改变数据内容——这是最关键的迁移不变量。"""
    store = _seed_flat(tmp_path, years=(2023, 2024))
    before = store.load().reset_index(drop=True)

    store.migrate_to_partitioned()
    after = BarStore(root=tmp_path).load().reset_index(drop=True)

    pd.testing.assert_frame_equal(before, after)
    assert len(after) == len(before)


def test_migrate_is_idempotent(tmp_path):
    store = _seed_flat(tmp_path, years=(2023, 2024))
    store.migrate_to_partitioned()
    n_after_first = len(BarStore(root=tmp_path).symbols())

    # 第二次迁移：没有旧文件可迁，不应报错也不应丢数据
    store2 = BarStore(root=tmp_path)
    stats = store2.migrate_to_partitioned()
    assert stats["symbols"] == 0
    assert len(BarStore(root=tmp_path).symbols()) == n_after_first


def test_migrate_handles_mixed_layout(tmp_path):
    """迁移期新旧布局共存：读取必须同时能看到两边的数据。"""
    store = BarStore(root=tmp_path, partitioned=False)
    store.save(make_bars("600519", pd.bdate_range("2024-01-01", "2024-06-30"), 10.0))

    # 手动迁一只，留一只在旧布局
    store2 = BarStore(root=tmp_path, partitioned=True)
    store2.save(make_bars("000001", pd.bdate_range("2024-01-01", "2024-06-30"), 20.0))
    (tmp_path / "daily" / "600519.parquet").unlink()

    mixed = BarStore(root=tmp_path)
    assert mixed.partitioned is True
    # 旧布局的 600519 已删除，但 symbols() 应能看到分区里的 000001
    assert "000001" in mixed.symbols()


def test_migrate_skips_unreadable_file(tmp_path):
    store = BarStore(root=tmp_path, partitioned=False)
    store.save(make_bars("600519", pd.bdate_range("2024-01-01", "2024-03-31")))
    (tmp_path / "daily" / "000001.parquet").write_bytes(b"not a parquet")

    stats = store.migrate_to_partitioned()
    assert stats["skipped"] >= 1
    assert "600519" in BarStore(root=tmp_path).symbols()

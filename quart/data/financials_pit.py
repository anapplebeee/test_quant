"""财报 PIT 全市场数据层（RESEARCH-002 §8-1）。

数据源：东财业绩报表 ``ak.stock_yjbb_em(date=报告期)`` —— 按报告期返回
全市场已披露该期财报的股票（**含此后退市的样本**），附最新公告日期。

PIT 三元组（RESEARCH-002 数据合同）：

- ``announcement_date``：供应商可见的最新公告日（= 最新修订的公告时间，
  保守不早于首次披露，无前视风险；首次披露时间由修订日志累积逼近）；
- ``available_at``：供应商数据到达本地时间（抓取时刻）；
- ``revision``：同一 (symbol, report_period) 内容变化次数，值变化即递增，
  全部修订历史存 ``financials_revisions.parquet``。

可用时点链沿用 ``value_growth.resolve_usable_at``：
announcement_date → 报告期 + 120 日兜底。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quart.config import data_root

FIN_PIT_DIR = data_root() / "factors"
FIN_MAIN_PATH = FIN_PIT_DIR / "financials.parquet"
FIN_REVISIONS_PATH = FIN_PIT_DIR / "financials_revisions.parquet"
FIN_CACHE_DIR = FIN_PIT_DIR / "pit_period_cache"

#: 全市场业绩报表行 → 项目财报 schema 的映射
YJBB_COLUMN_MAP = {
    "每股收益": "eps",
    "每股净资产": "bps",
    "净资产收益率": "roe",
    "销售毛利率": "gross_margin",
    "营业总收入-同比增长": "rev_yoy",
    "净利润-同比增长": "profit_yoy",
    "营业总收入-营业总收入": "revenue",
    "净利润-净利润": "net_profit",
}
VALUE_COLS = list(YJBB_COLUMN_MAP.values())

#: 业绩快报（披露更早、含实际初步数值 → 可作为首次可用时间）
YJKB_COLUMN_MAP = {
    "每股收益": "eps",
    "每股净资产": "bps",
    "净资产收益率": "roe",
    "营业收入-同比增长": "rev_yoy",
    "净利润-同比增长": "profit_yoy",
}


def normalize_yjkb(raw: pd.DataFrame, report_period: str, fetched_at: pd.Timestamp) -> pd.DataFrame:
    """业绩快报一期规范化（同 yjbb 口径；快报为正式报告前的实际初步数值）。

    注意：业绩**预告**（yjyg）只有预测值，不能作为正式财务数据的可用时点，
    归入公告事件流处理；快报含实际数值，可以。
    """
    period = pd.Timestamp(report_period)
    need = {"股票代码", *YJKB_COLUMN_MAP.keys(), "公告日期"}
    missing = need - set(raw.columns)
    if missing:
        raise ValueError(f"yjkb missing columns: {sorted(missing)}")
    out = pd.DataFrame({"symbol": raw["股票代码"].astype(str).str.zfill(6)})
    for src, dst in YJKB_COLUMN_MAP.items():
        out[dst] = pd.to_numeric(raw[src], errors="coerce")
    out["date"] = period
    announce = pd.to_datetime(raw["公告日期"], errors="coerce", format="mixed")
    out["flash_announce_date"] = announce.where(announce >= period)
    out["available_at"] = fetched_at
    return out.dropna(subset=["symbol"]).reset_index(drop=True)


def merge_flash_announcements(snapshot: pd.DataFrame, flash: pd.DataFrame) -> pd.DataFrame:
    """用快报公告日重建首次公告时间：announcement_date = min(正式报告, 快报)。

    快报比正式报告早数周，且含实际初步数值 —— 取两者较早者作为该报告期
    财务数据首次可用时间；来源记入 announce_source 供审计。
    """
    if flash.empty:
        return snapshot
    out = snapshot.copy()
    flash_idx = flash.set_index(["symbol", "date"])["flash_announce_date"]
    keys = pd.MultiIndex.from_frame(out[["symbol", "date"]])
    flash_val = flash_idx.reindex(keys).to_numpy()
    cur = pd.to_datetime(out["announcement_date"], errors="coerce")
    flash_ts = pd.Series(pd.to_datetime(flash_val, errors="coerce"), index=out.index)
    use_flash = flash_ts.notna() & (cur.isna() | (flash_ts < cur))
    out["announcement_date"] = cur.where(~use_flash, flash_ts)
    out["announce_source"] = use_flash.map({True: "yjkb_flash", False: "yjbb_report"})
    return out


def normalize_yjbb(raw: pd.DataFrame, report_period: str, fetched_at: pd.Timestamp) -> pd.DataFrame:
    """把一期业绩报表原始表规范化为 PIT 财报长表片段。

    Args:
        raw: ak.stock_yjbb_em 返回的原始表。
        report_period: 报告期，如 ``20230331``。
        fetched_at: 供应商数据到达时间。
    """
    period = pd.Timestamp(report_period)
    need = {"股票代码", *YJBB_COLUMN_MAP.keys(), "最新公告日期"}
    missing = need - set(raw.columns)
    if missing:
        raise ValueError(f"yjbb missing columns: {sorted(missing)}")
    out = pd.DataFrame({"symbol": raw["股票代码"].astype(str).str.zfill(6)})
    for src, dst in YJBB_COLUMN_MAP.items():
        out[dst] = pd.to_numeric(raw[src], errors="coerce")
    out["date"] = period
    announce = pd.to_datetime(raw["最新公告日期"], errors="coerce", format="mixed")
    # 公告时间早于报告期是供应商脏数据：置空，交给可用时点链兜底
    out["announcement_date"] = announce.where(announce >= period)
    out["available_at"] = fetched_at
    out["source"] = "yjbb_em"
    return out.dropna(subset=["symbol"]).reset_index(drop=True)


def _values_equal(a, b) -> bool:
    """同一 (symbol, date) 的两版数值在容差内是否一致（a/b 为行 Series）。"""
    for col in VALUE_COLS:
        va, vb = pd.to_numeric(pd.Series([a.get(col)]), errors="coerce").iloc[0], \
            pd.to_numeric(pd.Series([b.get(col)]), errors="coerce").iloc[0]
        va = -9e99 if pd.isna(va) else float(va)
        vb = -9e99 if pd.isna(vb) else float(vb)
        if abs(va - vb) > 1e-6 * (1 + abs(va)):
            return False
    pa = pd.to_datetime(pd.Series([a.get("announcement_date")]), errors="coerce").iloc[0]
    pb = pd.to_datetime(pd.Series([b.get("announcement_date")]), errors="coerce").iloc[0]
    return bool(pa == pb or (pd.isna(pa) and pd.isna(pb)))


def merge_revisions(
    main: pd.DataFrame,
    revisions: pd.DataFrame,
    snapshot: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """把新一期快照合并进主表与修订日志。

    - (symbol, date) 首次出现 → revision = 1；
    - 数值/公告时间与现有最新版一致 → 保持 revision；
    - 有变化 → revision = 现有 + 1，且旧行写入修订日志（带 fetched_at）。
    - 主表中不在本快照里的历史行原样保留（跨期合并）。

    Returns:
        (new_main, new_revisions)
    """
    main = main.copy()
    revisions = revisions.copy()
    if "revision" not in main:
        main["revision"] = 0
    if "announcement_date" not in main:
        main["announcement_date"] = pd.NaT
    if snapshot.empty:
        main["revision"] = pd.to_numeric(main["revision"], errors="coerce").fillna(1).astype(int)
        return main.reset_index(drop=True), revisions.reset_index(drop=True)
    if main.empty:
        snapshot = snapshot.copy()
        snapshot["revision"] = 1
        return snapshot.reset_index(drop=True), revisions.reset_index(drop=True)

    main_i = main.set_index(["symbol", "date"])
    main_i = main_i[~main_i.index.duplicated(keep="last")]
    # 旧主表（如早期 sina 行）可能缺新版数值列，补 NaN 保证对比可用
    for col in VALUE_COLS:
        if col not in main_i.columns:
            main_i[col] = np.nan
    snap_i = snapshot.set_index(["symbol", "date"])
    snap_i = snap_i[~snap_i.index.duplicated(keep="last")]

    common = snap_i.index.intersection(main_i.index)
    rev_values = pd.Series(1, index=snap_i.index, dtype="int64")

    if len(common):
        old_c = main_i.loc[common]
        new_c = snap_i.loc[common]
        changed = np.zeros(len(common), dtype=bool)
        for col in VALUE_COLS:
            a = pd.to_numeric(old_c[col], errors="coerce").fillna(-9e99).to_numpy(dtype=float)
            b = pd.to_numeric(new_c[col], errors="coerce").fillna(-9e99).to_numpy(dtype=float)
            changed |= np.abs(a - b) > 1e-6 * (1 + np.abs(a))
        pa = pd.to_datetime(old_c["announcement_date"], errors="coerce")
        pb = pd.to_datetime(new_c["announcement_date"], errors="coerce")
        pa_na = pa.notna().to_numpy()
        pb_na = pb.notna().to_numpy()
        pa_np = pa.fillna(pd.Timestamp("1900-01-01")).to_numpy()
        pb_np = pb.fillna(pd.Timestamp("1900-01-01")).to_numpy()
        changed |= (pa_na != pb_na) | (pa_np != pb_np)
        old_rev = pd.to_numeric(old_c["revision"], errors="coerce").fillna(0).astype(int).to_numpy()
        rev_values.loc[common] = np.where(changed, old_rev + 1, old_rev)
        if changed.any():
            logged = old_c[changed].copy()
            logged["revision"] = old_rev[changed]  # 被取代的旧版本号
            revisions = pd.concat([revisions, logged.reset_index()], ignore_index=True)

    snap_i = snap_i.copy()
    snap_i["revision"] = rev_values
    kept = main_i.loc[main_i.index.difference(snap_i.index)]
    merged = pd.concat([kept, snap_i], ignore_index=False)
    merged["revision"] = pd.to_numeric(merged["revision"], errors="coerce").fillna(1).astype(int)
    return merged.reset_index(), revisions.reset_index(drop=True)


def coverage_report(financials: pd.DataFrame, all_symbols: list[str]) -> pd.DataFrame:
    """逐报告期统计覆盖率（相对全市场符号清单）。"""
    fin = financials.copy()
    fin["date"] = pd.to_datetime(fin["date"])
    total = max(len(all_symbols), 1)
    rows = []
    for period, g in fin.groupby("date"):
        with_ann = g["announcement_date"].notna().sum() if "announcement_date" in g else 0
        rows.append({
            "report_period": period.date(),
            "symbols": g["symbol"].nunique(),
            "coverage_pct": g["symbol"].nunique() / total,
            "with_announce_date": int(with_ann),
        })
    out = pd.DataFrame(rows).sort_values("report_period").reset_index(drop=True)
    return out

"""龙虎榜选择性披露数据落地（RESEARCH-002 §8-3，P1）。

数据源（东财）：
- ``ak.stock_lhb_detail_em``：龙虎榜明细（净买额/成交额/上榜原因），按日期窗口；
- ``ak.stock_lhb_jgmmtj_em``：机构席位每日统计（机构买入净额/买卖总额）。

机构席位 vs 营业部/游资区分：
    branch_net_buy = 龙虎榜净买额 - 机构净买额（机构以外即营业部/游资席位）

输出合同（RESEARCH-002 §3.4，供 ``dragon_tiger_panels`` 消费）：
    symbol, published_at(上榜日，仅日期→下一交易日可用),
    net_buy_amount, turnover_amount,
    institution_net_buy_amount, branch_net_buy_amount,
    available_at(抓取时刻), reason

榜单是选择性披露样本：未上榜 = 无披露事件，不是净买入为零的完整观测。

用法：
    uv run python scripts/fetch_dragon_tiger.py --since 2023-01-01
    uv run python scripts/fetch_dragon_tiger.py --since 2023-01-01 --window-days 60
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.config import data_root

EVENTS_DIR = Path(data_root()) / "events"
OUT_PATH = EVENTS_DIR / "dragon_tiger.parquet"
WAN = 10_000.0  # 东财金额单位：万元


def _fetch_window(start: str, end: str) -> pd.DataFrame | None:
    import akshare as ak

    try:
        detail = ak.stock_lhb_detail_em(start_date=start, end_date=end)
    except Exception as exc:  # noqa: BLE001
        logger.warning("lhb detail {}~{} failed: {}", start, end, exc)
        return None
    if detail is None or detail.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "symbol": detail["代码"].astype(str).str.zfill(6),
        "published_at": pd.to_datetime(detail["上榜日"], errors="coerce"),
        "net_buy_amount": pd.to_numeric(detail["龙虎榜净买额"], errors="coerce") * WAN,
        "turnover_amount": pd.to_numeric(detail["龙虎榜成交额"], errors="coerce") * WAN,
        "reason": detail.get("上榜原因", ""),
    })
    try:
        inst = ak.stock_lhb_jgmmtj_em(start_date=start, end_date=end)
        if inst is not None and not inst.empty:
            inst = inst.rename(columns={"上榜日期": "上榜日"})
            key = ["symbol", "published_at"]
            inst_map = (inst.assign(
                symbol=inst["代码"].astype(str).str.zfill(6),
                published_at=pd.to_datetime(inst["上榜日"], errors="coerce"),
                institution_net_buy_amount=pd.to_numeric(inst["机构买入净额"], errors="coerce") * WAN,
                institution_buy_amount=pd.to_numeric(inst["机构买入总额"], errors="coerce") * WAN,
                institution_sell_amount=pd.to_numeric(inst["机构卖出总额"], errors="coerce") * WAN,
            )[
                key + ["institution_net_buy_amount", "institution_buy_amount",
                       "institution_sell_amount"]
            ].drop_duplicates(key))
            out = out.merge(inst_map, on=key, how="left")
    except Exception as exc:  # noqa: BLE001
        logger.warning("lhb institution {}~{} failed: {}", start, end, exc)
    for col in ("institution_net_buy_amount", "institution_buy_amount", "institution_sell_amount"):
        if col not in out:
            out[col] = pd.NA
    out["branch_net_buy_amount"] = out["net_buy_amount"] - out["institution_net_buy_amount"].fillna(0)
    # 回填数据：available_at = published_at（仅日期，事件映射推迟到下一交易日可用）；
    # 写抓取时刻会把全部事件推到未来，面板变空。
    out["available_at"] = out["published_at"]
    return out.dropna(subset=["published_at"])


def fetch_since(since: str, end: str | None, window_days: int, sleep_s: float) -> pd.DataFrame:
    """按窗口抓取并累积（窗口小 → 单次请求轻、失败重试代价低）。"""
    import akshare as ak  # noqa: F401 - 确保依赖可见

    start_ts = pd.Timestamp(since)
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.now().normalize()
    frames: list[pd.DataFrame] = []
    cur = start_ts
    while cur <= end_ts:
        wend = min(cur + pd.Timedelta(days=window_days - 1), end_ts)
        w = _fetch_window(cur.strftime("%Y%m%d"), wend.strftime("%Y%m%d"))
        if w is not None and not w.empty:
            frames.append(w)
            logger.info("{} ~ {}: {} events", cur.date(), wend.date(), len(w))
        cur = wend + pd.Timedelta(days=1)
        time.sleep(sleep_s)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(["symbol", "published_at"], keep="last")
    return out.sort_values(["published_at", "symbol"]).reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="龙虎榜数据抓取（选择性披露合同）")
    parser.add_argument("--since", default="2023-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--sleep", type=float, default=2.0)
    args = parser.parse_args()

    out = fetch_since(args.since, args.end, args.window_days, args.sleep)
    if out.empty:
        raise SystemExit("龙虎榜抓取结果为空")
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".tmp")
    out.to_parquet(tmp, index=False)
    tmp.replace(OUT_PATH)
    n_inst = int(out["institution_net_buy_amount"].notna().sum())
    logger.info("saved {} events / {} symbols -> {}（机构席位记录 {}）",
                len(out), out["symbol"].nunique(), OUT_PATH, n_inst)


if __name__ == "__main__":
    main()

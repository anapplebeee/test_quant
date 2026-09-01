"""公司公告结构化事件抓取（RESEARCH-002 §8-4，P1）。

数据源：
- ``ak.stock_notice_report``：东财公告流（标题/类型/日期），逐日抓取、断点续传；
- ``ak.stock_yjyg_em``：业绩预告（结构化方向，见 announcements.merge_forecast_events）。

输出：
- data/events/news.parquet —— 事件合同（symbol, published_at, sentiment,
  confidence, relevance, available_at + event_type/title/source），
  供 ``mine_factors.py --factor-group news`` 与事件情绪模型消费。

用法：
    uv run python scripts/fetch_announcements.py --since 2024-01-01
    uv run python scripts/fetch_announcements.py --skip-notice   # 只并预告
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
from quart.data.announcements import build_event_frame, merge_forecast_events

EVENTS_DIR = Path(data_root()) / "events"
OUT_PATH = EVENTS_DIR / "news.parquet"
CACHE_PATH = EVENTS_DIR / "announcements_raw.parquet"


def _fetch_notice_day(date: str) -> pd.DataFrame | None:
    import akshare as ak

    try:
        df = ak.stock_notice_report(symbol="全部", date=date)
        if df is None or df.empty:
            return None
        df = df[df["代码"].notna()]
        df = df[df["代码"].astype(str).str.match(r"^\d{6}$")]
        return df
    except Exception as exc:  # noqa: BLE001
        logger.warning("notice {} failed: {}", date, exc)
        return None


def fetch_notice_since(since: str, end: str | None, sleep_s: float) -> pd.DataFrame:
    """逐日抓取公告标题流（断点续传，缓存原始行）。"""
    have: set[str] = set()
    if CACHE_PATH.exists():
        have = set(pd.read_parquet(CACHE_PATH)["snap_date"].astype(str))
    dates = pd.bdate_range(since, end or pd.Timestamp.now().date()).strftime("%Y%m%d").tolist()
    todo = [d for d in dates if d not in have]
    frames: list[pd.DataFrame] = []
    for n, d in enumerate(todo, 1):
        df = _fetch_notice_day(d)
        if df is not None:
            df = df.assign(snap_date=d)
            frames.append(df)
        if n % 20 == 0:
            _append(frames)
            frames = []
            logger.info("notice progress {}/{}", n, len(todo))
        time.sleep(sleep_s)
    _append(frames)
    if not CACHE_PATH.exists():
        return pd.DataFrame()
    return pd.read_parquet(CACHE_PATH)


def _append(frames: list[pd.DataFrame]) -> None:
    if not frames:
        return
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    new = pd.concat(frames, ignore_index=True)
    old = pd.read_parquet(CACHE_PATH) if CACHE_PATH.exists() else pd.DataFrame()
    merged = pd.concat([old, new], ignore_index=True)
    merged = merged.drop_duplicates(["snap_date", "代码", "公告标题"], keep="last")
    merged.to_parquet(CACHE_PATH, index=False)


def fetch_forecasts(since_year: int, sleep_s: float) -> pd.DataFrame:
    """全市场业绩预告（按报告期）。"""
    import akshare as ak

    frames = []
    periods = [f"{y}{md}" for y in range(since_year, pd.Timestamp.now().year + 1)
               for md in ("0331", "0630", "0930", "1231")]
    periods = [p for p in periods if pd.Timestamp(p) <= pd.Timestamp.now()]
    for p in periods:
        try:
            df = ak.stock_yjyg_em(date=p)
            if df is not None and not df.empty:
                frames.append(df)
        except Exception as exc:  # noqa: BLE001
            logger.warning("yjyg {} failed: {}", p, exc)
        time.sleep(sleep_s)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main() -> None:
    parser = argparse.ArgumentParser(description="公告事件抓取")
    parser.add_argument("--since", default="2024-01-01", help="公告流起点（逐日，量大）")
    parser.add_argument("--end", default=None)
    parser.add_argument("--forecast-since", type=int, default=2023, help="业绩预告起始年")
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--skip-notice", action="store_true")
    args = parser.parse_args()

    fetched_at = pd.Timestamp.now()
    parts: list[pd.DataFrame] = []
    if not args.skip_notice:
        raw = fetch_notice_since(args.since, args.end, args.sleep)
        if not raw.empty:
            ev = build_event_frame(raw, fetched_at)
            parts.append(ev)
            logger.info("notice events: {} (from {} raw rows)", len(ev), len(raw))
    fc = fetch_forecasts(args.forecast_since, args.sleep)
    if not fc.empty:
        ev = merge_forecast_events(fc, fetched_at)
        parts.append(ev)
        logger.info("forecast events: {}", len(ev))
    if not parts:
        raise SystemExit("无事件产出")
    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values(["published_at", "symbol"]).reset_index(drop=True)
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".tmp")
    out.to_parquet(tmp, index=False)
    tmp.replace(OUT_PATH)
    logger.info("saved {} events / {} symbols -> {}", len(out), out["symbol"].nunique(), OUT_PATH)
    logger.info("event types: {}", out["event_type"].value_counts().to_dict())


if __name__ == "__main__":
    main()

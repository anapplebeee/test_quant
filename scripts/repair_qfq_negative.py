"""修复 qfq 前复权失真导致的负价格/仙股价数据（2026-08-28 数据审计）。

现象：两批股票的 qfq 历史价格被压成负数或近零"仙股价"：
  - 负价格（16 只，首扫）：如 000937 冀中能源 close=-0.32
  - 仙股价/负 low（8 只，二扫）：如 601088 中国神华 2020 年 qfq close≈0.2 元，
    600809 山西汾酒 2019 年 close≈1 元且 low<0；此前 close<=0 保护拦不住
结论：腾讯与东财源 qfq 均返回失真值（复权算法共性），改用后复权 hfq 重拉——
      hfq 从上市日起价格单调累积，不会为负；pct_change 与 qfq 一致，因子层面无差异。
持久化：修复后股票写入 data/hfq_pins.txt（quart/data/hfq_pins.py），
      updater 对钉住股票始终用 hfq 拉取，防止增量更新再次用 qfq 全量刷新导致损坏复发。

用法：python scripts/repair_qfq_negative.py [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.data.hfq_pins import add_hfq_pins
from quart.data.source_akshare import to_tx_symbol
from quart.data.store import BarStore

# 全市场扫描出的 qfq 失真股票：首批负价格（close<0）+ 二批仙股价/负 low（price-ratio>100 或 OHLC 含负值）
NEGATIVE_PRICE_STOCKS = [
    "600188", "000937", "601919", "600295", "600039", "601225", "000408",
    "601666", "600546", "601699", "000933", "002432", "600256", "002756",
    "600096", "300390",
    # 2026-08-28 二扫新增：负 low / 仙股价（600809, 601088, 000408 等复发确认）
    "300573", "600066", "600809", "601088", "601717", "000708", "601126",
]

START = "20190101"


def main() -> None:
    import argparse

    import akshare as ak

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="仅打印清单不落盘")
    args = parser.parse_args()
    if args.dry_run:
        print(f"repair list: {NEGATIVE_PRICE_STOCKS}")
        return

    store = BarStore()
    today = pd.Timestamp.today().strftime("%Y%m%d")
    for sym in NEGATIVE_PRICE_STOCKS:
        try:
            raw = ak.stock_zh_a_hist_tx(
                symbol=to_tx_symbol(sym),
                start_date=START,
                end_date=today,
                adjust="hfq",
            )
        except Exception as exc:
            logger.error("fetch hfq {} failed: {}", sym, str(exc)[:80])
            continue
        if raw is None or raw.empty:
            logger.warning("hfq {} empty, skip", sym)
            continue
        df = raw.rename(
            columns={"date": "date", "open": "open", "close": "close",
                     "high": "high", "low": "low", "volume": "volume",
                     "amount": "amount"}
        ).copy()
        df["symbol"] = sym
        keep = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]
        n = store.save(df[keep], replace=True)
        ok = (df["close"] > 0).all()
        logger.info("{} repaired: rows={} min_close={:.3f} all_positive={}", sym, n, df["close"].min(), ok)

    # 钉住：updater 今后对这些股票固定用 hfq，防止增量更新用 qfq 全量刷新使损坏复发
    add_hfq_pins(NEGATIVE_PRICE_STOCKS)
    logger.info("hfq pins updated: {} symbols", len(NEGATIVE_PRICE_STOCKS))


if __name__ == "__main__":
    main()

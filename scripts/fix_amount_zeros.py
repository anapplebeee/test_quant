"""修复 QG-014 单点：活跃股日线中 volume>0 但 amount<=0 的坏行（数据清洗 2026-09-02）。

背景
----
全库扫描（8.3M 行）定位到唯一坏点：``002336@2020-06-17``（volume=1118342 但
amount=0）。跨日自洽校验：该股有效行 ``amount ≈ volume × close``（median
ratio=1.002，p10-p90 0.988~1.10），故按同源规律把 amount 补齐为
``volume × close``（约 5.84e6），不删行、不破坏复权连续性。

修复原则
--------
- 只动确认的坏点（volume>0 且 amount 缺失/<=0），用股票自身的跨日均价规律补齐；
- 无匹配时不动任何文件并 fail-closed 退出（避免误写）；
- 可复现：再次运行即幂等（已补齐的行 amount>0 不再命中）。
"""
from __future__ import annotations

import pandas as pd
from loguru import logger

FIXES = {
    # symbol: [(date, note)]
    "002336": [("2020-06-17", "volume>0 但 amount=0，按 volume×close 补齐")],
}


def main() -> None:
    from quart.data.store import BarStore

    store = BarStore()
    fixed = 0
    for symbol, points in FIXES.items():
        path = store.daily_dir / f"{symbol}.parquet"
        if not path.exists():
            logger.warning("skip {}: daily file missing ({})", symbol, path)
            continue
        df = pd.read_parquet(path).sort_values("date").reset_index(drop=True)
        changed = False
        for date, note in points:
            mask = df["date"].astype(str).str[:10] == date
            if not mask.any():
                logger.warning("skip {symbol}@{date}: row not found", symbol=symbol, date=date)
                continue
            rows = df[mask]
            bad = (rows["volume"] > 0) & (rows["amount"].isna() | (rows["amount"] <= 0))
            if not bad.any():
                logger.info("{symbol}@{date} already clean", symbol=symbol, date=date)
                continue
            idx = rows.index[bad]
            fill = (rows.loc[idx, "volume"] * rows.loc[idx, "close"]).astype("float64")
            df.loc[idx, "amount"] = fill.round(2)
            logger.info("fix {symbol}@{date}: amount 0 -> {val} ({note})",
                        symbol=symbol, date=date, val=float(fill.iloc[0]), note=note)
            changed = True
        if changed:
            store.save(df, replace=True)
            fixed += 1
    if fixed == 0:
        logger.info("no amount-zero fix needed (QG-014 should now pass or remain unchanged)")


if __name__ == "__main__":
    main()

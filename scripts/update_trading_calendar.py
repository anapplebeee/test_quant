"""从 AkShare 公共接口更新本地 A 股交易日历缓存。"""
from __future__ import annotations

import pandas as pd
from rich.console import Console

from quart.data.calendar import DEFAULT_CALENDAR_PATH

console = Console()


def main() -> None:
    import akshare as ak

    frame = ak.tool_trade_date_hist_sina()
    if frame is None or frame.empty:
        raise SystemExit("交易日历接口返回空数据")
    column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
    output = pd.DataFrame({"trade_date": pd.to_datetime(frame[column]).dt.date.astype(str)})
    output = output.drop_duplicates().sort_values("trade_date")
    DEFAULT_CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(DEFAULT_CALENDAR_PATH, index=False, encoding="utf-8-sig")
    console.print(
        f"[green]交易日历已更新: {DEFAULT_CALENDAR_PATH} | "
        f"{len(output)} 日 | {output.iloc[0, 0]} ~ {output.iloc[-1, 0]}[/green]"
    )


if __name__ == "__main__":
    main()

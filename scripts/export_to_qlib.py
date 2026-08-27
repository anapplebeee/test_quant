from __future__ import annotations

import argparse
import struct
from pathlib import Path

import numpy as np
import pandas as pd
from rich.console import Console
from rich.progress import track

from quart.config import PROJECT_ROOT
from quart.data.store import BarStore

console = Console()

FIELDS = ["open", "high", "low", "close", "volume", "vwap", "factor"]


def export(store: BarStore, out_dir: Path) -> dict:
    bars = store.load(include_index=False)
    if bars.empty:
        raise SystemExit("no bars to export")

    bars["date"] = pd.to_datetime(bars["date"])
    calendar = sorted(bars["date"].unique())
    cal_index = {d: i for i, d in enumerate(calendar)}

    features_dir = out_dir / "features"
    features_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "calendars").mkdir(exist_ok=True)
    (out_dir / "instruments").mkdir(exist_ok=True)

    with open(out_dir / "calendars" / "day.txt", "w", encoding="utf-8") as f:
        for d in calendar:
            f.write(d.strftime("%Y-%m-%d") + "\n")

    lines = []
    symbols = sorted(bars["symbol"].unique())
    for symbol in track(symbols, description="exporting"):
        df = bars[bars["symbol"] == symbol].set_index("date").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        full = df.reindex(calendar)
        start_idx = cal_index[df.index.min()]

        volume = full["volume"].to_numpy(dtype=np.float64)
        amount = full["amount"].to_numpy(dtype=np.float64)
        close = full["close"].to_numpy(dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            vwap = np.where(volume > 0, amount / volume, np.nan)

        arrays = {
            "open": full["open"].to_numpy(dtype=np.float64),
            "high": full["high"].to_numpy(dtype=np.float64),
            "low": full["low"].to_numpy(dtype=np.float64),
            "close": close,
            "volume": volume,
            "vwap": vwap,
            "factor": np.full(len(full), 1.0),
        }

        inst_dir = features_dir / symbol
        inst_dir.mkdir(exist_ok=True)
        for field in FIELDS:
            vals = arrays[field].astype(np.float32)
            payload = struct.pack("<f", float(start_idx)) + vals.tobytes()
            (inst_dir / f"{field}.day.bin").write_bytes(payload)

        valid = df.dropna(subset=["close"])
        lines.append(f"{symbol}\t{valid.index.min().strftime('%Y-%m-%d')}\t{valid.index.max().strftime('%Y-%m-%d')}\n")

    with open(out_dir / "instruments" / "all.txt", "w", encoding="utf-8") as f:
        f.writelines(lines)
    return {"symbols": len(symbols), "calendar_days": len(calendar)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Export BarStore parquet to qlib binary format")
    parser.add_argument("--out", default=None, help="output dir (default: data/qlib)")
    args = parser.parse_args()

    out_dir = Path(args.out) if args.out else PROJECT_ROOT / "data" / "qlib"
    stats = export(BarStore(), out_dir)
    console.print(f"[green]exported[/green] {stats['symbols']} symbols, {stats['calendar_days']} calendar days -> {out_dir}")


if __name__ == "__main__":
    main()

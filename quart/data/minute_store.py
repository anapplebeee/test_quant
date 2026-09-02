"""分钟级行情仓库（research 级，2026-09-02 新增）。

独立于日线 ``BarStore``，存智兔数服分钟K线（5/15/30/60min）。按
``data/minute/{symbol}.parquet`` 单股单文件存储，文件内 ``level`` 列区分分钟粒度
（同一 symbol 不同 level 同文件），每行一根分钟 bar：

    ts / level / open / high / low / close / volume / amount

写入为幂等增量（按 (ts, level) 去重，避免重复抓取覆盖或污染），读取可按
(symbol, level, start, end) 过滤并按 ts 排序。

语义口径
--------
- volume 原始单位为**股**（智兔实测 amount≈volume×price）；本模块不换算，
  统一原样存储并记录单位=股，由消费方按需 ÷100 转手（见 ``volume_in_lots``）。
- 复权：抓取默认 ``adj=n``（不复权）。分钟研究（日内路径/竞价）宜用不复权价，
  避免盘中除权跳变；如需与日线 qfq 对齐应另拉 f 并在消费侧自行对齐。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from loguru import logger

from quart.config import data_root

COLUMNS = ["ts", "level", "open", "high", "low", "close", "volume", "amount"]
LEVELS = {"5", "15", "30", "60"}


class MinuteStore:
    """分钟行情仓库：``data/minute/{symbol}.parquet``。"""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else data_root()
        self.minute_dir = self.root / "minute"
        self.minute_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 写入 ----------------

    def save(
        self,
        symbol: str,
        df: pd.DataFrame,
        level: str | None = None,
        replace: bool = False,
    ) -> int:
        """写入一只股票一个（或多个）粒度的分钟 bar（幂等增量）。

        ``level`` 指定则强制把 df 标记为该粒度（df 可不含 level 列）；df 本身含
        level 列时忽略该参数。按 (ts, level) 去重、ts 升序。返回写入行数。
        """
        if df is None or df.empty:
            return 0
        if level is not None and level not in LEVELS:
            raise ValueError(f"unsupported level {level!r}（支持 {sorted(LEVELS)}）")
        if "level" in df.columns:
            data = df.copy()
        else:
            if level is None:
                raise ValueError("save 需要 df 含 level 列或显式传 level")
            data = df.assign(level=level)
        data = data[COLUMNS].copy()
        data["ts"] = pd.to_datetime(data["ts"], errors="coerce")
        data = data.dropna(subset=["ts"]).drop_duplicates(subset=["ts", "level"], keep="last")

        path = self._path(symbol)
        if path.exists() and not replace:
            existing = pd.read_parquet(path)
            existing = existing[COLUMNS]
            data = pd.concat([existing, data], ignore_index=True)
        data = (
            data.drop_duplicates(subset=["ts", "level"], keep="last")
            .sort_values(["ts", "level"])
            .reset_index(drop=True)
        )
        tmp = path.with_suffix(".parquet.tmp")
        data.to_parquet(tmp, index=False)
        tmp.replace(path)
        return len(data)

    # ---------------- 读取 ----------------

    def load(
        self,
        symbol: str,
        level: str | None = None,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """读取一只股票的分钟 bar，可选按 level 与 ts 区间过滤。找不到文件返回空。"""
        path = self._path(symbol)
        if not path.exists():
            return pd.DataFrame(columns=COLUMNS)
        df = pd.read_parquet(path)
        if df.empty:
            return pd.DataFrame(columns=COLUMNS)
        df = df[COLUMNS].copy()
        if level is not None:
            if level not in LEVELS:
                raise ValueError(f"unsupported level {level!r}")
            df = df[df["level"] == level]
        if start is not None:
            df = df[df["ts"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["ts"] <= pd.Timestamp(end)]
        return df.sort_values("ts").reset_index(drop=True)

    def has(self, symbol: str, level: str) -> bool:
        """该 symbol 是否已有指定粒度的数据（避免重复全量抓取）。"""
        if not self._path(symbol).exists():
            return False
        try:
            return bool(self.load(symbol, level).shape[0])
        except Exception:
            return False

    def _path(self, symbol: str) -> Path:
        return self.minute_dir / f"{str(symbol).zfill(6)}.parquet"


__all__ = ["MinuteStore", "COLUMNS", "LEVELS"]

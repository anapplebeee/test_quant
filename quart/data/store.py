"""行情仓库（Parquet + DuckDB）。

存储布局
--------
新布局（分区，默认写入）：

    data/daily/year=2024/600519.parquet
    data/daily/year=2025/600519.parquet
    data/index/year=2024/IDX000300.parquet

旧布局（单股全史单文件，仍可读取）：

    data/daily/600519.parquet

为什么分区
----------
1. **谓词下推**：DuckDB 能用 `hive_partitioning` 直接跳过无关年份目录。
   回测常只取最近 1-2 年，全市场扫描可省掉 70%+ 的 IO。
2. **增量写入只重写当年**：旧布局每只票增量 1 天也要重写全史
   （read-modify-write 整个文件）。分区后只 touching 当前年份分区。
3. **旧布局 `load()` 把 5000+ 文件路径拼进 SQL 字符串**，
   查询文本大到接近 DuckDB 解析器上限。分区后可以用目录通配符。

兼容性
------
`_paths(symbol)` 同时识别新旧两种布局，读取侧无缝兼容。
用 `scripts/migrate_partition_store.py` 做一次性迁移。
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from loguru import logger

from quart.config import data_root

CN_TZ = ZoneInfo("Asia/Shanghai")

BAR_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]

#: 分区列名与前缀
PARTITION_COLUMN = "year"
PARTITION_PREFIX = "year="


def drop_incomplete_today(df: pd.DataFrame) -> pd.DataFrame:
    """盘中调用时剔除当天未收盘的K线，只保留已完成的历史bar."""
    if df is None or df.empty:
        return df
    now = dt.datetime.now(CN_TZ)
    market_close_minutes = 15 * 60 + 30
    if now.hour * 60 + now.minute >= market_close_minutes:
        return df
    today_midnight = pd.Timestamp(now.date())
    return df[df["date"] < today_midnight]


EMPTY_BARS = pd.DataFrame({c: pd.Series(dtype=t) for c, t in {
    "date": "datetime64[ns]", "symbol": "object", "open": "float64",
    "high": "float64", "low": "float64", "close": "float64",
    "volume": "float64", "amount": "float64",
}.items()})


class BarStore:
    """行情仓库。

    Parameters
    ----------
    partitioned:
        None（默认）= 自动检测：目录里已有 `year=` 子目录则用分区布局，
        否则用旧布局。迁移期间两种布局可以共存。
    """

    def __init__(self, root: str | Path | None = None, partitioned: bool | None = None):
        self.root = Path(root) if root else data_root()
        self.daily_dir = self.root / "daily"
        self.index_dir = self.root / "index"
        self.universe_dir = self.root / "universe"
        for d in (self.daily_dir, self.index_dir, self.universe_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._partitioned = self._detect_layout() if partitioned is None else partitioned

    # ---------------- 布局 ----------------

    @property
    def partitioned(self) -> bool:
        return self._partitioned

    def _detect_layout(self) -> bool:
        """存在任何 `year=` 子目录即认为已迁移到分区布局。"""
        return any(
            p.is_dir() and p.name.startswith(PARTITION_PREFIX)
            for p in list(self.daily_dir.iterdir())[:1000]
        ) if self.daily_dir.exists() else False

    def _base_dir(self, symbol: str) -> Path:
        return self.index_dir if symbol.startswith("IDX") else self.daily_dir

    def _path(self, symbol: str) -> Path:
        """旧布局路径（单股单文件）。"""
        return self._base_dir(symbol) / f"{symbol}.parquet"

    def _paths(self, symbol: str) -> list[Path]:
        """该 symbol 的全部数据文件（新旧布局均支持，按年份升序）。"""
        base = self._base_dir(symbol)
        if not self._partitioned:
            p = base / f"{symbol}.parquet"
            return [p] if p.exists() else []
        return sorted(
            base.glob(f"{PARTITION_PREFIX}*/{symbol}.parquet"),
            key=lambda p: p.parent.name,
        )

    def _partition_path(self, symbol: str, year: int) -> Path:
        return self._base_dir(symbol) / f"{PARTITION_PREFIX}{year}" / f"{symbol}.parquet"

    # ---------------- 写入 ----------------

    def save(self, df: pd.DataFrame, replace: bool = False) -> int:
        if df is None or df.empty:
            return 0
        df = df[BAR_COLUMNS].copy()
        df["date"] = pd.to_datetime(df["date"])
        for col in ("open", "high", "low", "close", "volume", "amount"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # 保护：qfq 前复权在极端股本变动（重组/债转股）下会把历史价格算成负值，
        # 收益率/波动率因子随之失真。负价格行置 NaN 并告警（应改用 hfq 重拉）。
        for sym, g in df.groupby("symbol"):
            neg = g["close"] <= 0
            if neg.any():
                logger.warning(
                    "{} non-positive close rows detected ({}), set to NaN - "
                    "consider hfq repair (scripts/repair_qfq_negative.py)",
                    sym, int(neg.sum()),
                )
                df.loc[g.index[neg], ["open", "high", "low", "close"]] = float("nan")

        if not self._partitioned:
            return self._save_flat(df, replace)

        written = 0
        # 按 (symbol, year) 分组：增量只重写涉及的年份分区，
        # 而不是像旧布局那样重写该 symbol 的全史。
        df = df.assign(**{PARTITION_COLUMN: df["date"].dt.year})
        for (symbol, year), group in df.groupby(["symbol", PARTITION_COLUMN]):
            written += self._write_partition(
                str(symbol), int(year), group.drop(columns=[PARTITION_COLUMN]), replace
            )
        return written

    def _write_partition(self, symbol: str, year: int, group: pd.DataFrame, replace: bool) -> int:
        path = self._partition_path(symbol, year)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not replace:
            existing = pd.read_parquet(path)
            group = pd.concat([existing, group], ignore_index=True)
            group = group.drop_duplicates(subset=["date", "symbol"], keep="last")
        group = group.sort_values("date").reset_index(drop=True)
        tmp = path.with_suffix(".parquet.tmp")
        group.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        return len(group)

    def _save_flat(self, df: pd.DataFrame, replace: bool) -> int:
        """旧布局写入（保留以兼容未迁移的环境）。"""
        written = 0
        for symbol, group in df.groupby("symbol"):
            path = self._path(str(symbol))
            if path.exists() and not replace:
                existing = pd.read_parquet(path)
                group = pd.concat([existing, group], ignore_index=True)
                group = group.drop_duplicates(subset=["date", "symbol"], keep="last")
            group = group.sort_values("date").reset_index(drop=True)
            tmp = path.with_suffix(".parquet.tmp")
            group.to_parquet(tmp, index=False)
            os.replace(tmp, path)
            written += len(group)
        return written

    # ---------------- 读取 ----------------

    def load(
        self,
        symbols: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        include_index: bool = False,
        exclude_symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        if symbols is not None:
            df = self._load_symbols(list(symbols), start, end, include_index)
            if exclude_symbols:
                df = df[~df["symbol"].isin(set(exclude_symbols))]
            return df

        df = self._load_all_paths(start, end, include_index)
        if exclude_symbols:
            df = df[~df["symbol"].isin(set(exclude_symbols))]
        return df

    def _load_all_paths(self, start, end, include_index: bool) -> pd.DataFrame:
        if self._partitioned:
            return self._query_partitioned(start, end, include_index)

        dirs = [self.daily_dir] + ([self.index_dir] if include_index else [])
        files: list[Path] = []
        for d in dirs:
            files.extend(d.glob("*.parquet"))
        if not files:
            return EMPTY_BARS.copy()
        return self._query_files(files, start, end)

    def _query_partitioned(self, start, end, include_index: bool) -> pd.DataFrame:
        """分区查询：用目录通配符 + hive_partitioning 让 DuckDB 裁剪年份。"""
        globs = self._partition_globs(start, end, include_index)
        if not globs:
            return EMPTY_BARS.copy()
        try:
            import duckdb
        except ImportError:
            return self._query_globs_pandas(globs, start, end)

        # glob 可能一个文件都匹配不到（空存储/年份目录为空），
        # DuckDB 的 read_parquet 对空列表直接抛 IOException，需先探测。
        import glob as _glob

        if not any(_glob.glob(g) for g in globs):
            return EMPTY_BARS.copy()

        listing = ", ".join(f"'{g}'" for g in globs)
        conds = ["date IS NOT NULL"]
        if start:
            conds.append(f"date >= DATE '{start}'")
        if end:
            conds.append(f"date <= DATE '{end}'")
        where = "WHERE " + " AND ".join(conds)
        query = (
            f"SELECT * FROM read_parquet([{listing}], hive_partitioning=true) "
            f"{where} ORDER BY date, symbol"
        )
        df = duckdb.sql(query).df()
        # 分区列是 DuckDB 从目录名推导的，落盘时不应出现
        return df.drop(columns=[PARTITION_COLUMN], errors="ignore")

    def _partition_globs(self, start, end, include_index: bool) -> list[str]:
        """按起止年份收窄需要扫描的分区目录。

        这是分区改造的核心收益：只 glob 涉及的年份，
        而不是每天把全市场的全部历史文件都塞进查询。
        """
        dirs = [self.daily_dir] + ([self.index_dir] if include_index else [])
        years = self._years_in_range(start, end)
        globs: list[str] = []
        for d in dirs:
            if years is None:
                globs.append((d / f"{PARTITION_PREFIX}*" / "*.parquet").as_posix())
            else:
                for y in years:
                    p = d / f"{PARTITION_PREFIX}{y}"
                    if p.exists():
                        globs.append((p / "*.parquet").as_posix())
                # 旧布局遗留文件（迁移后仍可能有未迁移的 symbol）
        return globs

    def _years_in_range(self, start, end) -> list[int] | None:
        """返回查询涉及的年份列表；无法确定范围时返回 None（扫全部分区）。"""
        if not start and not end:
            return None
        lo = pd.Timestamp(start).year if start else self._min_partition_year()
        hi = pd.Timestamp(end).year if end else self._max_partition_year()
        if lo is None or hi is None or hi < lo:
            return None
        return list(range(int(lo), int(hi) + 1))

    def _partition_years(self, directory: Path) -> list[int]:
        if not directory.exists():
            return []
        return sorted(
            int(p.name[len(PARTITION_PREFIX):])
            for p in directory.iterdir()
            if p.is_dir() and p.name.startswith(PARTITION_PREFIX)
            and p.name[len(PARTITION_PREFIX):].isdigit()
        )

    def _min_partition_year(self) -> int | None:
        ys = self._partition_years(self.daily_dir)
        return ys[0] if ys else None

    def _max_partition_year(self) -> int | None:
        ys = self._partition_years(self.daily_dir)
        return ys[-1] if ys else None

    def _query_globs_pandas(self, globs: list[str], start, end) -> pd.DataFrame:
        import glob as _glob

        files: list[Path] = []
        for g in globs:
            files.extend(Path(p) for p in _glob.glob(g))
        return self._query_files_pandas(files, start, end)

    def _query_files(self, files: list[Path], start: str | None, end: str | None) -> pd.DataFrame:
        """全量查询。优先 DuckDB（列式下推 + 谓词过滤），不可用时回退 pandas。

        duckdb 延迟导入：它在 `load()` 之外的路径（单文件读写、索引查询）
        完全用不到，顶层 import 会让核心数据层对可选依赖产生硬耦合。
        """
        try:
            import duckdb
        except ImportError:
            logger.debug("duckdb unavailable, falling back to pandas scan")
            return self._query_files_pandas(files, start, end)

        quoted = "[" + ", ".join(f"'{f.as_posix()}'" for f in sorted(files)) + "]"
        conds = ["date IS NOT NULL"]
        if start:
            conds.append(f"date >= '{start}'")
        if end:
            conds.append(f"date <= '{end}'")
        where = "WHERE " + " AND ".join(conds)
        query = f"SELECT * FROM read_parquet({quoted}) {where} ORDER BY date, symbol"
        return duckdb.sql(query).df()

    def _query_files_pandas(self, files: list[Path], start: str | None, end: str | None) -> pd.DataFrame:
        frames = []
        for f in sorted(files):
            try:
                frames.append(pd.read_parquet(f))
            except Exception as exc:
                logger.warning("skip unreadable {}: {}", f, exc)
        if not frames:
            return EMPTY_BARS.copy()
        out = pd.concat(frames, ignore_index=True)
        out = out[out["date"].notna()]
        if start:
            out = out[out["date"] >= pd.Timestamp(start)]
        if end:
            out = out[out["date"] <= pd.Timestamp(end)]
        return out.sort_values(["date", "symbol"]).reset_index(drop=True)

    def _load_symbols(
        self,
        symbols: list[str],
        start: str | None,
        end: str | None,
        include_index: bool,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        missing: list[str] = []
        for sym in symbols:
            if sym.startswith("IDX") and not include_index:
                missing.append(sym)
                continue
            paths = self._paths(sym)
            if not paths:
                missing.append(sym)
                continue
            # 分区布局下按年份过滤，避免读取无关年份
            paths = self._filter_paths_by_year(paths, start, end)
            if not paths:
                missing.append(sym)
                continue
            frames.extend(pd.read_parquet(p) for p in paths)
        if missing:
            logger.warning("symbols not in store: {}", sorted(missing)[:20])
        if not frames:
            return EMPTY_BARS.copy()
        out = pd.concat(frames, ignore_index=True)
        out = out.drop(columns=[PARTITION_COLUMN], errors="ignore")
        if start:
            out = out[out["date"] >= pd.Timestamp(start)]
        if end:
            out = out[out["date"] <= pd.Timestamp(end)]
        return out.sort_values(["date", "symbol"]).reset_index(drop=True)

    def _filter_paths_by_year(self, paths: list[Path], start, end) -> list[Path]:
        if not self._partitioned or (not start and not end):
            return paths
        lo = pd.Timestamp(start).year if start else -9999
        hi = pd.Timestamp(end).year if end else 9999
        kept = []
        for p in paths:
            name = p.parent.name
            if not name.startswith(PARTITION_PREFIX):
                kept.append(p)
                continue
            y = int(name[len(PARTITION_PREFIX):])
            if lo <= y <= hi:
                kept.append(p)
        return kept

    def load_benchmark(self, code: str) -> pd.DataFrame:
        return self._load_symbols([f"IDX{code}"], None, None, include_index=True)

    # ---------------- 元数据 ----------------

    def last_date(self, symbol: str) -> pd.Timestamp | None:
        """最新日期：分区布局下从最大年份往回找（避免全史扫描）。"""
        paths = self._paths(symbol)
        if not paths:
            return None
        for p in reversed(paths):
            try:
                dates = pd.read_parquet(p, columns=["date"])["date"]
            except Exception:
                continue
            if not dates.empty:
                return pd.Timestamp(dates.max())
        return None

    def first_date(self, symbol: str) -> pd.Timestamp | None:
        """最早日期：分区布局下从最小年份开始找。"""
        paths = self._paths(symbol)
        if not paths:
            return None
        for p in paths:
            try:
                dates = pd.read_parquet(p, columns=["date"])["date"]
            except Exception:
                continue
            if not dates.empty:
                return pd.Timestamp(dates.min())
        return None

    def symbols(self) -> list[str]:
        """全部股票代码（新旧布局均支持）。"""
        if self._partitioned:
            found = {
                p.stem
                for p in self.daily_dir.glob(f"{PARTITION_PREFIX}*/*.parquet")
            }
        else:
            found = {p.stem for p in self.daily_dir.glob("*.parquet")}
        # 迁移期两种布局可能共存
        found |= {p.stem for p in self.daily_dir.glob("*.parquet")}
        return sorted(found)

    def coverage_summary(self) -> dict:
        """Fast latest-cross-section coverage metadata for UI and audit gates."""
        symbols = self.symbols()
        result = {
            "symbols": len(symbols),
            "latest_date": None,
            "latest_symbols": 0,
            "latest_coverage": 0.0,
            "freshness_days": self.freshness_days(),
        }
        if not symbols:
            return result

        if self._partitioned:
            years = self._partition_years(self.daily_dir)
            if not years:
                return result
            pattern = (self.daily_dir / f"{PARTITION_PREFIX}{years[-1]}" / "*.parquet").as_posix()
            try:
                import duckdb

                query = (
                    "WITH bars AS ("
                    f"SELECT date, symbol FROM read_parquet('{pattern}') WHERE date IS NOT NULL"
                    "), latest AS (SELECT max(date) AS value FROM bars) "
                    "SELECT latest.value AS latest_date, "
                    "count(DISTINCT bars.symbol) FILTER (WHERE bars.date = latest.value) AS latest_symbols "
                    "FROM bars CROSS JOIN latest GROUP BY latest.value"
                )
                row = duckdb.sql(query).fetchone()
                if row:
                    result["latest_date"] = str(pd.Timestamp(row[0]).date()) if row[0] else None
                    result["latest_symbols"] = int(row[1] or 0)
            except Exception as exc:
                logger.debug("coverage summary duckdb fallback: {}", exc)

        if result["latest_date"] is None:
            latest_by_symbol = [self.last_date(symbol) for symbol in symbols]
            latest_by_symbol = [date for date in latest_by_symbol if date is not None]
            if latest_by_symbol:
                latest = max(latest_by_symbol)
                result["latest_date"] = str(latest.date())
                result["latest_symbols"] = sum(date == latest for date in latest_by_symbol)

        if result["symbols"]:
            result["latest_coverage"] = result["latest_symbols"] / result["symbols"]
        return result

    def freshness_days(self, reference: str | None = None) -> int | None:
        """Newest bar age in calendar days vs CN-now (or given YYYYMMDD)."""
        import datetime as _dt

        if reference:
            today = pd.Timestamp(_dt.datetime.strptime(reference, "%Y%m%d").date())
        else:
            today = pd.Timestamp(dt.datetime.now(CN_TZ).date())

        if self._partitioned:
            # 分区布局：全局最新 bar 必在 daily/index 各自最新年份分区，
            # 用 duckdb 一次扫描取 max(date)，避免逐文件 read_parquet 扫全历史（原 68s 卡点）。
            latest = self._freshness_latest_partitioned()
        else:
            latest = None
            candidates: list[Path] = []
            for d in (self.daily_dir, self.index_dir):
                candidates.extend(d.glob("*.parquet"))
            for p in candidates:
                try:
                    mx = pd.read_parquet(p, columns=["date"])["date"].max()
                except Exception:
                    continue
                if latest is None or mx > latest:
                    latest = mx

        if latest is None:
            return None
        return int((today - pd.Timestamp(latest).normalize()).days)

    def _freshness_latest_partitioned(self) -> "pd.Timestamp | None":
        """分区布局下全局最新 bar 日期：只扫 daily/index 各自最新年份分区。

        数据按 date 追加写入并按年分区，全局最大 date 必落在最新年份分区内，
        因此无需扫描历史年份即可获得准确 freshness。
        """
        import duckdb

        latest: "pd.Timestamp | None" = None
        for d in (self.daily_dir, self.index_dir):
            years = self._partition_years(d)
            if not years:
                continue
            pattern = (d / f"{PARTITION_PREFIX}{years[-1]}" / "*.parquet").as_posix()
            try:
                row = duckdb.sql(
                    f"SELECT max(date) FROM read_parquet('{pattern}') WHERE date IS NOT NULL"
                ).fetchone()
                if row and row[0] is not None:
                    ts = pd.Timestamp(row[0])
                    if latest is None or ts > latest:
                        latest = ts
            except Exception as exc:
                logger.debug("freshness partitioned scan failed for {}: {}", d, exc)
        return latest

    # ---------------- 迁移 ----------------

    def migrate_to_partitioned(self, progress=None) -> dict:
        """把旧布局（单股全史单文件）迁移到分区布局。

        幂等：已迁移的文件会跳过；迁移后旧文件被删除。
        返回 {"symbols": n, "files": n, "skipped": n}。
        """
        stats = {"symbols": 0, "files": 0, "skipped": 0}
        for d in (self.daily_dir, self.index_dir):
            for src in sorted(d.glob("*.parquet")):
                try:
                    df = pd.read_parquet(src)
                except Exception as exc:
                    logger.warning("migrate skip {}: {}", src.name, exc)
                    stats["skipped"] += 1
                    continue
                if df.empty or "date" not in df.columns:
                    stats["skipped"] += 1
                    continue
                symbol = str(df["symbol"].iloc[0]) if "symbol" in df.columns else src.stem
                df = df.assign(**{PARTITION_COLUMN: pd.to_datetime(df["date"]).dt.year})
                # 先全量读入已存在的分区再写回：任一年份读失败就整体放弃该
                # symbol，绝不"用部分数据覆盖 + 删源"——那会不可逆地销毁历史。
                merged_by_year: dict[int, pd.DataFrame] = {}
                failed = False
                for year, group in df.groupby(PARTITION_COLUMN):
                    dst = d / f"{PARTITION_PREFIX}{int(year)}" / f"{symbol}.parquet"
                    if dst.exists():
                        try:
                            existing = pd.read_parquet(dst)
                            group = pd.concat([existing, group], ignore_index=True)
                            group = group.drop_duplicates(subset=["date", "symbol"], keep="last")
                        except Exception as exc:
                            # 读不出旧分区就中止本次迁移，保留源平铺文件待人工处理
                            logger.error("migrate abort {}: read {}: {}", symbol, dst.name, exc)
                            failed = True
                            break
                    merged_by_year[int(year)] = group.drop(columns=[PARTITION_COLUMN]).sort_values("date")
                if failed:
                    stats["skipped"] += 1
                    continue
                for year, group in merged_by_year.items():
                    dst = d / f"{PARTITION_PREFIX}{year}" / f"{symbol}.parquet"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_suffix(".parquet.tmp")
                    group.to_parquet(tmp, index=False)
                    os.replace(tmp, dst)
                    stats["files"] += 1
                src.unlink()
                stats["symbols"] += 1
                if progress:
                    progress(f"migrated {symbol}")

        self._partitioned = self._detect_layout()
        return stats

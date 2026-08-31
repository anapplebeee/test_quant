"""数据质量治理：巨幅跳变分类 + 异常符号阻断（隔离）。

背景（CODEX_PROGRESS 未完成事项 #1，2026-08-31）：
全市场扫描发现约 1251 个单日绝对收益超过 25% 的跳变。其中绝大多数是
**合法行情**（创业板/科创板 ±20% 涨跌停、停牌复牌跳空、新股上市初期限价），
真正的坏数据（复权错误/拆分未处理/源异常）只占少数。
治理原则：**分类后只阻断物理不可能的跳变，不误伤真实行情**。

跳变分类规则（按 A 股交易制度）：
- resume_gap  复牌跳空：前一日 volume==0，涨跌停规则不适用，合法；
- new_stock   新股上市初期（前 NEW_STOCK_SESSIONS 个交易日）无涨跌停限制，合法；
- limit_move  |ret| <= 1.05 × 涨跌停幅度：制度允许的真实行情，合法；
- anomaly     超过 1.05 × 涨跌停幅度：制度上不可能 —— 判定为坏数据，阻断。

涨跌停幅度（按代码前缀，ST ±5% 小于主板 ±10% 不影响分类）：
- 科创板 688xxx / 创业板 300-301xxx：±20%
- 北交所 43xxxx/83xxxx/87xxxx/92xxxx：±30%
- 其余（沪市 60x / 深市 00x 主板）：±10%
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
from loguru import logger

from quart.config import data_root

#: 默认跳变报警阈值（绝对日收益），与 data_quality_scan.py --jumps 一致
DEFAULT_JUMP_THRESHOLD = 0.25
#: |ret| 超过 limit × 该系数才判 anomaly（留 5% 余量容纳行情源四舍五入）
ANOMALY_MARGIN = 1.05
#: 新股上市初期的交易日数（无涨跌停限制）
NEW_STOCK_SESSIONS = 10

BLOCKLIST_PATH = Path(data_root()) / "meta" / "quality_blocklist.csv"
QUARANTINE_DIR = Path(data_root()) / "quarantine"

_JUMP_COLUMNS = ["symbol", "date", "ret", "prev_volume", "limit_pct", "class"]


def price_limit_pct(symbol: str) -> float:
    """按代码前缀返回涨跌停幅度（如 0.10）。未知前缀保守按主板处理。"""
    s = str(symbol).zfill(6)
    if s.startswith(("688", "689", "300", "301", "302")):
        return 0.20
    if s.startswith(("43", "83", "87", "88", "92")):
        return 0.30
    return 0.10


def _classify_single(bars: pd.DataFrame, symbol: str, threshold: float) -> pd.DataFrame:
    """对单只股票的日线分类跳变行。bars 需含 date/close/volume，按日期升序。"""
    if bars.empty:
        return pd.DataFrame(columns=_JUMP_COLUMNS)
    df = bars.sort_values("date").reset_index(drop=True)
    ret = df["close"].pct_change(fill_method=None)
    jump_mask = ret.abs() > threshold
    if not jump_mask.any():
        return pd.DataFrame(columns=_JUMP_COLUMNS)

    limit = price_limit_pct(symbol)
    prev_volume = df["volume"].shift(1)
    # 新股判定：跳变行位置在首个交易日后的 NEW_STOCK_SESSIONS 个交易日内
    session_idx = pd.Series(range(len(df)), index=df.index)
    is_new = (session_idx - session_idx.iloc[0]) <= NEW_STOCK_SESSIONS

    rows = []
    for i in df.index[jump_mask]:
        r = float(ret.iloc[i])
        pv = prev_volume.iloc[i]
        if pd.notna(pv) and float(pv) == 0.0:
            cls = "resume_gap"
        elif is_new.iloc[i]:
            cls = "new_stock"
        elif abs(r) <= ANOMALY_MARGIN * limit:
            cls = "limit_move"
        else:
            cls = "anomaly"
        rows.append(
            {
                "symbol": symbol,
                "date": pd.Timestamp(df.loc[i, "date"]),
                "ret": r,
                "prev_volume": None if pd.isna(pv) else float(pv),
                "limit_pct": limit,
                "class": cls,
            }
        )
    return pd.DataFrame(rows, columns=_JUMP_COLUMNS)


def classify_jumps(
    bars: pd.DataFrame,
    threshold: float = DEFAULT_JUMP_THRESHOLD,
) -> pd.DataFrame:
    """对多只股票的日线长表（symbol,date,close,volume）分类全部跳变。

    Returns:
        DataFrame(columns=_JUMP_COLUMNS)，class ∈ resume_gap/new_stock/
        limit_move/anomaly；无跳变时返回空表。
    """
    if bars.empty:
        return pd.DataFrame(columns=_JUMP_COLUMNS)
    parts = []
    for symbol, g in bars.groupby("symbol"):
        part = _classify_single(g, str(symbol), threshold)
        if not part.empty:
            parts.append(part)
    if not parts:
        return pd.DataFrame(columns=_JUMP_COLUMNS)
    return pd.concat(parts, ignore_index=True)


def build_blocklist(jump_report: pd.DataFrame) -> list[str]:
    """从跳变报告提取应阻断的符号：仅含 anomaly（物理不可能的跳变）。"""
    if jump_report.empty:
        return []
    bad = jump_report[jump_report["class"] == "anomaly"]
    return sorted(bad["symbol"].astype(str).unique())


def save_blocklist(symbols: list[str], path: Path | None = None) -> Path:
    """写阻断清单（覆盖写；空清单时写空文件以显式记录"已治理"状态）。"""
    p = path or BLOCKLIST_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"symbol": sorted(set(symbols))}).to_csv(p, index=False)
    logger.info("blocklist saved: {} symbols -> {}", len(set(symbols)), p)
    return p


def load_blocklist(path: Path | None = None) -> set[str]:
    """读取阻断清单；文件不存在返回空集（未治理状态不阻断任何东西）。"""
    p = path or BLOCKLIST_PATH
    if not p.exists():
        return set()
    try:
        df = pd.read_csv(p)
    except Exception:
        return set()
    if df.empty or "symbol" not in df.columns:
        return set()
    return set(df["symbol"].astype(str))


def quarantine_symbols(store, symbols: list[str], quarantine_dir: Path | None = None) -> list[Path]:
    """把异常符号的数据文件移入隔离目录（阻断而非删除，可人工复核恢复）。

    兼容新旧布局：移动该 symbol 的全部数据文件（分年文件一并处理）。
    """
    qdir = quarantine_dir or QUARANTINE_DIR
    qdir.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for sym in symbols:
        for src in store._paths(sym):  # noqa: SLF001 - 治理模块需要布局细节
            if src.exists():
                dst = qdir / src.name
                # 分年文件名（symbol_YYYY.parquet）已含符号，不会互相覆盖
                shutil.move(str(src), str(dst))
                moved.append(dst)
                logger.warning("quarantined {} -> {}", src, dst)
    return moved

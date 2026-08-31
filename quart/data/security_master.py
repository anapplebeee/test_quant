"""证券主数据与来源映射（DATA-001，对应 TARGET_ARCHITECTURE_V3 §8）。

定位
----
本模块建立证券主数据的**权威 schema 与生效区间模型**，并把本地可用
元数据（股票名称表、上市日期、统计行业）装配成第一版主数据。
历史上缺失的字段（真实上市日、退市日、ST 状态历史、停复牌等）
通过 ``SOURCE_MAPPING`` 显式声明数据来源与覆盖状态，
为 Phase B 接入完整 SecurityMaster 铺路 —— 这正是 DATA-001
"准备证券主数据来源映射"的范围。

生效区间合同（§8）::

    symbol, exchange, board, security_type,
    listed_at, delisted_at,
    status, status_effective_from, status_effective_to,
    lot_size, tick_size, price_limit_rule, settlement_rule

规则查询键::

    trade_date + exchange + board + security_type + status

PIT 查询
--------
``SecurityMaster.as_of(date)`` 只返回在该日期已上市且未退市的证券；
``status_as_of(symbol, date)`` 返回该日期生效的状态（区间含头不含尾）。
回测/正式研究必须用 PIT 查询，禁止直接用"当前状态"过滤历史。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from quart.config import data_root

#: 主数据权威列（顺序即 schema，§8 合同）
MASTER_COLUMNS = [
    "symbol", "exchange", "board", "security_type",
    "listed_at", "delisted_at",
    "status", "status_effective_from", "status_effective_to",
    "lot_size", "tick_size", "price_limit_rule", "settlement_rule",
]

MASTER_PATH = Path(data_root()) / "meta" / "security_master.parquet"

#: 字段级来源映射：哪些字段来自哪里、当前覆盖状态。
#: status=available 表示本地已有可靠数据；pending 表示待接入
#: 对应 akshare 接口后补齐（Phase B）。
SOURCE_MAPPING: dict[str, dict[str, Any]] = {
    "symbol/name": {
        "source": "ak.stock_info_a_code_name() / ak.stock_zh_a_spot_em()",
        "local": "data/stock_names.parquet",
        "status": "available",
    },
    "listed_at": {
        "source": "ak.stock_info_sh_name_code() / ak.stock_info_sz_name_code() / ak.stock_info_bj_name_code()",
        "local": "data/universe/list_dates.parquet（近似：面板首日）",
        "status": "pending",
        "note": "list_dates 为行情面板首日，非真实上市日；新股缺失时回退",
    },
    "delisted_at": {
        "source": "ak.stock_info_sh_delist() / ak.stock_info_sz_delist()",
        "local": None,
        "status": "pending",
    },
    "board/exchange/security_type": {
        "source": "代码前缀推断（与 quality.price_limit_pct 同规则）",
        "local": "derived",
        "status": "available",
        "note": "等待板块/证券类型权威数据后改为数据驱动",
    },
    "status(ST/风险警示/停复牌)": {
        "source": "ak.stock_zh_a_st_em() / 停复牌接口",
        "local": None,
        "status": "pending",
    },
    "industry": {
        "source": "ak.stock_board_industry_name_em() / 申万行业",
        "local": "data/universe/stat_industry.parquet（统计聚类，非官方行业）",
        "status": "pending",
    },
    "lot_size/tick_size/settlement_rule": {
        "source": "交易所规则（T+1、买入整手 100 股、tick 0.01）",
        "local": "derived（静态规则）",
        "status": "available",
        "note": "北交所最小价差 0.01、零股卖出等细则待 RuleBook 阶段细化",
    },
}


def _board_of(symbol: str) -> tuple[str, str, str, float]:
    """由代码前缀推导 (exchange, board, security_type, price_limit_rule)。"""
    s = str(symbol).zfill(6)
    if s.startswith(("60", "68")):
        exchange = "SSE"
    elif s.startswith(("00", "30")):
        exchange = "SZSE"
    elif s.startswith(("43", "83", "87", "88", "92")):
        exchange = "BSE"
    else:
        exchange = "UNKNOWN"
    if s.startswith("68"):
        board, limit = "STAR", 0.20  # 科创板
    elif s.startswith("30"):
        board, limit = "CHINEXT", 0.20  # 创业板
    elif exchange == "BSE":
        board, limit = "BSE", 0.30  # 北交所
    else:
        board, limit = "MAIN", 0.10  # 主板
    security_type = "index" if s.startswith("IDX") else "stock"
    return exchange, board, security_type, limit


class SecurityMaster:
    """按生效区间保存的证券主数据（PIT 可查询）。"""

    def __init__(self, table: pd.DataFrame):
        self.table = self._normalize(table)

    # ---------------- 构建 ----------------

    @staticmethod
    def _normalize(table: pd.DataFrame) -> pd.DataFrame:
        df = table.copy()
        for col in MASTER_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NaT if col.endswith("_at") or col.endswith("_from") or col.endswith("_to") else None
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
        for col in ("listed_at", "delisted_at", "status_effective_from", "status_effective_to"):
            df[col] = pd.to_datetime(df[col])
        return df[MASTER_COLUMNS].reset_index(drop=True)

    @classmethod
    def from_local(cls, root: Path | None = None) -> SecurityMaster:
        """从本地元数据装配第一版主数据（名称 + 近似上市日 + 板块推断）。"""
        root = Path(root) if root else Path(data_root())
        names_path = root / "stock_names.parquet"
        if not names_path.exists():
            raise FileNotFoundError(f"stock_names not found: {names_path}")
        names = pd.read_parquet(names_path)

        listed: pd.Series = pd.Series(dtype="datetime64[ns]")
        list_dates_path = root / "universe" / "list_dates.parquet"
        if list_dates_path.exists():
            ld = pd.read_parquet(list_dates_path)
            listed = pd.Series(
                pd.to_datetime(ld["first_date"]).values,
                index=ld["symbol"].astype(str).str.zfill(6),
            )

        rows = []
        for _, r in names.iterrows():
            symbol = str(r["code"]).zfill(6)
            exchange, board, sec_type, limit = _board_of(symbol)
            rows.append({
                "symbol": symbol,
                "exchange": exchange,
                "board": board,
                "security_type": sec_type,
                "listed_at": listed.get(symbol),
                "delisted_at": pd.NaT,  # 待 SOURCE_MAPPING delisted_at 接入
                "status": "listed",
                "status_effective_from": listed.get(symbol),
                "status_effective_to": pd.NaT,
                "lot_size": 100,
                "tick_size": 0.01,
                "price_limit_rule": limit,
                "settlement_rule": "T+1",
            })
        master = cls(pd.DataFrame(rows))
        logger.info("security master built: {} securities", len(master.table))
        return master

    # ---------------- PIT 查询 ----------------

    def as_of(self, date: str | pd.Timestamp) -> pd.DataFrame:
        """返回该日期已上市且未退市的证券（PIT 股票池基础）。

        PIT 正确性优先（§2.1）：listed_at 未知的证券**不能**确认在该
        日期已上市，因此被排除 —— 宁可空池也不引入未来函数。
        需要扩池时先补齐上市日期数据（见 SOURCE_MAPPING）。
        """
        ts = pd.Timestamp(date)
        df = self.table
        listed = df["listed_at"].notna() & (df["listed_at"] <= ts)
        not_delisted = df["delisted_at"].isna() | (df["delisted_at"] > ts)
        return df[listed & not_delisted].reset_index(drop=True)

    def status_as_of(self, symbol: str, date: str | pd.Timestamp) -> dict[str, Any] | None:
        """返回某证券在该日期生效的状态记录（含头不含尾），无则 None。"""
        ts = pd.Timestamp(date)
        df = self.table[self.table["symbol"] == str(symbol).zfill(6)]
        if df.empty:
            return None
        for _, r in df.iterrows():
            start_ok = pd.isna(r["status_effective_from"]) or r["status_effective_from"] <= ts
            end_ok = pd.isna(r["status_effective_to"]) or ts < r["status_effective_to"]
            if start_ok and end_ok:
                return r.to_dict()
        return None

    # ---------------- 版本（内容哈希） ----------------

    def version(self) -> str:
        """主数据内容哈希（security_master_version，16 hex）。

        任何字段值（含历史状态）变化都会改变版本 —— 与快照 ID 同一原则。
        """
        df = self.table.copy()
        for col in ("listed_at", "delisted_at", "status_effective_from", "status_effective_to"):
            df[col] = df[col].astype("str")
        canonical = json.dumps(
            df.to_dict(orient="records"), ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    # ---------------- 校验 ----------------

    def validate(self) -> list[str]:
        """schema 与区间一致性校验，返回问题列表（空 = 通过）。"""
        problems: list[str] = []
        missing_cols = [c for c in MASTER_COLUMNS if c not in self.table.columns]
        if missing_cols:
            problems.append(f"missing columns: {missing_cols}")
            return problems
        if self.table["symbol"].duplicated().any():
            dup = self.table[self.table["symbol"].duplicated()]["symbol"].unique()
            problems.append(f"duplicated symbols (需拆分为状态区间行): {sorted(dup)[:10]}")
        bad = self.table["listed_at"].notna() & self.table["delisted_at"].notna() & (
            self.table["listed_at"] > self.table["delisted_at"]
        )
        if bad.any():
            problems.append(f"listed_at > delisted_at: {sorted(self.table.loc[bad, 'symbol'])[:10]}")
        for _, r in self.table.iterrows():
            if (
                pd.notna(r["status_effective_to"])
                and pd.notna(r["status_effective_from"])
                and r["status_effective_from"] > r["status_effective_to"]
            ):
                problems.append(f"status interval inverted: {r['symbol']}")
        return problems

    # ---------------- 落盘 ----------------

    def save(self, path: Path | None = None) -> Path:
        p = path or MASTER_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        self.table.to_parquet(p, index=False)
        logger.info("security master saved: {} securities -> {}", len(self.table), p)
        return p

    @classmethod
    def load(cls, path: Path | None = None) -> SecurityMaster:
        p = path or MASTER_PATH
        if not p.exists():
            raise FileNotFoundError(f"security master not found: {p}")
        return cls(pd.read_parquet(p))


def load_master_version(path: Path | None = None) -> str:
    """读取主数据文件并返回版本哈希（供快照 PIT 元数据调用）。"""
    return SecurityMaster.load(path).version()


def source_mapping_summary() -> pd.DataFrame:
    """来源映射表（便于人工评审与后续 Phase B 验收）。"""
    return pd.DataFrame(
        [
            {"fields": fields, **{k: v for k, v in spec.items()}}
            for fields, spec in SOURCE_MAPPING.items()
        ]
    )

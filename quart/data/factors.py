"""外部因子数据层：财务质量因子 + 估值代理因子（季频）。

数据源（akshare）：
- 财务：ak.stock_financial_analysis_indicator —— 新浪，季度报告期，
  ROE/毛利率/营收净利增速 + 每股收益/每股净资产（后者与价格合成 EP/BP 估值因子）

落地：data/factors/financials.parquet
     （symbol,date=报告期,eps,bps,roe,gross_margin,rev_yoy,profit_yoy）
使用注意：date 为报告期而非披露日，因子研究时需做披露时滞处理（报告期+约120天）。

设计：抓取为"全量快照"式——每次覆盖写；与日线更新解耦，按需/定时运行。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from loguru import logger

from quart.config import data_root

FACTOR_DIR = Path(data_root()) / "factors"
FIN_COLS = ["symbol", "date", "eps", "bps", "roe", "gross_margin", "rev_yoy", "profit_yoy"]


def _mkdir() -> None:
    FACTOR_DIR.mkdir(parents=True, exist_ok=True)


def fetch_financials(symbol: str, start_year: str = "2019") -> pd.DataFrame | None:
    """单只股票季度财务质量（ROE/毛利率/增速）+ 估值代理（EPS/BPS）。"""
    import akshare as ak

    try:
        raw = ak.stock_financial_analysis_indicator(symbol=symbol, start_year=start_year)
    except Exception as exc:
        logger.warning("financials {} failed: {}", symbol, str(exc)[:80])
        return None
    if raw is None or raw.empty:
        return None
    date_col = next((c for c in raw.columns if "日期" in c), None)
    if date_col is None:
        return None
    col_map = {
        "eps": next((c for c in raw.columns if "摊薄每股收益" in c), None),
        "bps": next((c for c in raw.columns if "每股净资产_调整前" in c), None),
        "roe": next((c for c in raw.columns if "净资产收益率" in c and "加权" in c), None)
        or next((c for c in raw.columns if "净资产收益率" in c), None),
        "gross_margin": next((c for c in raw.columns if "销售毛利率" in c), None),
        "rev_yoy": next((c for c in raw.columns if "主营业务收入增长率" in c), None),
        "profit_yoy": next((c for c in raw.columns if "净利润增长率" in c), None),
    }
    if not all(col_map.values()):
        logger.warning("financials {} column mismatch: {}", symbol, list(raw.columns)[:8])
        return None
    out = pd.DataFrame(
        {
            "symbol": symbol,
            "date": pd.to_datetime(raw[date_col], errors="coerce"),
            **{k: pd.to_numeric(raw[v], errors="coerce") for k, v in col_map.items()},
        }
    ).dropna(subset=["date"]).sort_values("date")
    return out[FIN_COLS]


def build_factor_store(
    symbols: list[str],
    financials: bool = True,
    max_symbols: int | None = None,
) -> dict[str, int]:
    """批量抓取财务因子并落盘。返回 {financials_rows}。"""
    _mkdir()
    targets = list(symbols[:max_symbols] if max_symbols else symbols)
    f_frames = []
    for n, sym in enumerate(targets, 1):
        f = fetch_financials(sym)
        if f is not None and not f.empty:
            f_frames.append(f)
        if n % 25 == 0:
            logger.info("factors progress {}/{}", n, len(targets))
    res = {}
    if financials and f_frames:
        fdf = pd.concat(f_frames, ignore_index=True).drop_duplicates(["symbol", "date"], keep="last")
        fdf.to_parquet(FACTOR_DIR / "financials.parquet", index=False)
        res["financials_rows"] = len(fdf)
        logger.info("financials saved: {} rows", len(fdf))
    return res


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from quart.data.universe import get_constituents

    symbols = get_constituents("000300")
    print(f"universe: {len(symbols)} symbols")
    res = build_factor_store(symbols)
    print(res)

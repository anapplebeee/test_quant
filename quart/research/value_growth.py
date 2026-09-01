"""价值成长基本面因子（研报 R2：华创《威廉·米勒中心价值成长投资法》思想的因子化）。

米勒策略原始形态（R2，2013-2017 A股回测：年化 22.3%、超额 13.1%）是
"筛选+打分"式选股：资本报酬率/营运报酬率高于行业均值且在提升 + 估值合理
（PB<2×市场均值、PE<1/定存利率、市值<10年FCF折现）。

本实现将其核心思想压缩为可用现有数据（quart/data/factors.py 抓取的季频
财务快照 + 日线收盘价）计算的截面因子：

- 质量改善：roe_now vs roe_prev（相邻报告期）、profit_yoy —— 对应米勒
  "资本报酬率高于此前且在提升"的 Growth 一翼；
- 估值代理：ep = eps/close、bp = bps/close —— 对应 "本益比/股价净值比低"
  的 Value 一翼（用最新收盘价对报告期 EPS/BPS 定价）；
- vg_score：四者截面排名等权合成，值越大越"又好又便宜"。

口径警示（与 R2 原文一致的风险点）：
- financials.date 是**报告期**而非披露日；真实披露时间优先，缺失时回退
  disclosure_lag_days（默认120天）避免前视；
- eps 为报告期累计口径（非 TTM），ep 因子有季节性，仅作排序用不作绝对估值。
"""
from __future__ import annotations

import pandas as pd

PUBLICATION_COLUMNS = ("published_at", "announcement_date", "disclosure_date")


def resolve_usable_at(
    financials: pd.DataFrame,
    disclosure_lag_days: int = 120,
) -> pd.Series:
    """解析财报首次可用时间：真实披露时间优先，保守时滞兜底。

    ``date`` 是报告期而不是披露日。若供应商提供 published_at/
    announcement_date/disclosure_date，则使用真实时间；缺失或早于报告期的
    异常记录回退为报告期 + ``disclosure_lag_days``。
    """
    report_date = pd.to_datetime(financials["date"], errors="coerce")
    fallback = report_date + pd.to_timedelta(int(disclosure_lag_days), unit="D")
    publication = pd.Series(pd.NaT, index=financials.index, dtype="datetime64[ns]")
    for column in PUBLICATION_COLUMNS:
        if column in financials:
            candidate = pd.to_datetime(financials[column], errors="coerce", format="mixed")
            publication = publication.where(publication.notna(), candidate)
    valid = publication.notna() & report_date.notna() & (publication >= report_date)
    return publication.where(valid, fallback)


def _add_quality_candidates(financials: pd.DataFrame) -> pd.DataFrame:
    """在季频长表上计算只依赖历史报告期的质量候选。"""
    fin = financials.sort_values(["symbol", "date"]).copy()
    for column in ("roe", "profit_yoy", "rev_yoy", "gross_margin"):
        if column not in fin:
            fin[column] = pd.NA
        fin[column] = pd.to_numeric(fin[column], errors="coerce")
    grouped = fin.groupby("symbol", group_keys=False)
    fin["roe_stability"] = grouped["roe"].transform(
        lambda values: -values.rolling(8, min_periods=4).std()
    )
    fin["profit_accel"] = grouped["profit_yoy"].diff()
    fin["margin_accel"] = grouped["gross_margin"].diff()
    # 没有分析师一致预期时，用净利增速相对营收增速衡量利润弹性；名称明确
    # 标注 proxy，避免把它误称为标准化意外盈余（SUE）。
    fin["earnings_surprise_proxy"] = fin["profit_yoy"] - fin["rev_yoy"]
    return fin


def _normalize_financials(financials: pd.DataFrame) -> pd.DataFrame:
    """统一供应商常见的整数/字符串证券代码口径。"""
    fin = financials.copy()
    if "symbol" not in fin:
        raise ValueError("financial data missing symbol column")

    def _symbol(value) -> str:
        text = str(value)
        if text.endswith(".0") and text[:-2].isdigit():
            text = text[:-2]
        return text.zfill(6) if text.isdigit() else text

    fin["symbol"] = fin["symbol"].map(_symbol)
    return fin


def build_value_growth(
    financials: pd.DataFrame,
    closes: pd.DataFrame,
    as_of: str | pd.Timestamp,
    disclosure_lag_days: int = 120,
) -> pd.DataFrame:
    """构建 as_of 日的价值成长因子截面。

    Args:
        financials: 财务快照长表（symbol,date,eps,bps,roe,...），
                    见 quart/data/factors.py。
        closes: 收盘价宽表面板（index=date, columns=symbol）。
        as_of: 因子日期。
        disclosure_lag_days: 缺真实披露时间时的保守兜底时滞。

    Returns:
        DataFrame(index=symbol)，列：
        roe / roe_prev / roe_improve / profit_yoy / ep / bp / vg_score
    """
    as_of_ts = pd.Timestamp(as_of)
    fin = _normalize_financials(financials)
    fin["date"] = pd.to_datetime(fin["date"], errors="coerce")
    fin = fin.dropna(subset=["date"])
    fin["usable_at"] = resolve_usable_at(fin, disclosure_lag_days)
    fin = _add_quality_candidates(fin)
    fin = fin[fin["usable_at"] <= as_of_ts]
    if fin.empty:
        return pd.DataFrame()

    rows = []
    for sym, g in fin.groupby("symbol"):
        g = g.sort_values("date")
        latest = g.iloc[-1]
        prev = g.iloc[-2] if len(g) >= 2 else None
        price = closes[sym] if sym in closes.columns else None
        if price is None:
            continue
        # 用 as_of 前最近的有效收盘价定价
        px_series = price.loc[:as_of_ts].dropna()
        if px_series.empty:
            continue
        px = float(px_series.iloc[-1])
        if not px > 0:
            continue
        eps, bps = pd.to_numeric(latest.get("eps"), errors="coerce"), pd.to_numeric(
            latest.get("bps"), errors="coerce"
        )
        rows.append(
            {
                "symbol": sym,
                "roe": pd.to_numeric(latest.get("roe"), errors="coerce"),
                "roe_prev": pd.to_numeric(prev.get("roe"), errors="coerce")
                if prev is not None
                else pd.NA,
                "profit_yoy": pd.to_numeric(latest.get("profit_yoy"), errors="coerce"),
                "roe_stability": pd.to_numeric(latest.get("roe_stability"), errors="coerce"),
                "profit_accel": pd.to_numeric(latest.get("profit_accel"), errors="coerce"),
                "margin_accel": pd.to_numeric(latest.get("margin_accel"), errors="coerce"),
                "earnings_surprise_proxy": pd.to_numeric(
                    latest.get("earnings_surprise_proxy"), errors="coerce"
                ),
                "ep": (eps / px) * 100.0 if pd.notna(eps) else pd.NA,  # % 口径
                "bp": (bps / px) * 100.0 if pd.notna(bps) else pd.NA,
                "report_date": latest["date"],
            }
        )

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).set_index("symbol")
    out["roe_improve"] = out["roe"] - out["roe_prev"]

    # vg_score：四因子截面分位排名等权（值越大越好：ROE改善↑、净利增速↑、便宜↑）
    rank_parts = []
    for col in ("roe_improve", "profit_yoy", "ep", "bp"):
        if col in out and out[col].notna().sum() > 1:
            rank_parts.append(out[col].rank(pct=True))
    out["vg_score"] = pd.concat(rank_parts, axis=1).mean(axis=1) if rank_parts else pd.NA
    return out


def pit_panels(
    financials: pd.DataFrame,
    closes: pd.DataFrame,
    factors: tuple[str, ...] = ("roe", "roe_improve", "profit_yoy", "ep", "bp"),
    disclosure_lag_days: int = 120,
) -> dict[str, pd.DataFrame]:
    """构建 PIT 价值成长因子的宽表面板（date × symbol），供策略层合成使用。

    与 :func:`pit_features` 同一可用时点口径（真实披露优先、lag 兜底），
    但输出为逐日截面面板而非长表 —— 策略在 prepare() 阶段一次构建，
    target_weights() 逐日取行，无逐日 merge 开销。

    Args:
        financials: 财务快照长表（symbol,date,eps,bps,roe,profit_yoy）。
        closes: 收盘价宽表（index=date, columns=symbol），用于 ep/bp 定价。
        factors: 需要的因子列。
        disclosure_lag_days: 缺真实披露时间时的兜底时滞（防前视）。

    Returns:
        {factor_name: DataFrame(index=date, columns=symbol)}，仅含有数据的
        符号列；无财务数据的符号不出现在面板中（调用方自行中性填充）。
    """
    if financials.empty or closes.empty:
        return {}
    fin = _normalize_financials(financials)
    fin["date"] = pd.to_datetime(fin["date"], errors="coerce")
    fin = fin.dropna(subset=["date"]).sort_values(["symbol", "date"])
    for col in ("eps", "bps", "roe", "profit_yoy", "rev_yoy", "gross_margin"):
        if col in fin:
            fin[col] = pd.to_numeric(fin[col], errors="coerce")
    fin["usable_at"] = resolve_usable_at(fin, disclosure_lag_days)
    fin = _add_quality_candidates(fin)
    fin["roe_improve"] = fin.groupby("symbol")["roe"].diff()

    idx = pd.DatetimeIndex(closes.index)
    left = pd.DataFrame({"datetime": idx}).sort_values("datetime")

    frames: dict[str, list[pd.Series]] = {f: [] for f in factors}
    for sym, g in fin.groupby("symbol"):
        m = pd.merge_asof(
            left,
            g.rename(columns={"usable_at": "datetime"})
            [[
                "datetime", "eps", "bps", "roe", "roe_improve", "profit_yoy",
                "roe_stability", "profit_accel", "margin_accel",
                "earnings_surprise_proxy",
            ]]
            .sort_values("datetime"),
            on="datetime",
            direction="backward",
        )
        if sym in closes.columns:
            px = closes[sym].reindex(idx).to_numpy()
            m["ep"] = m["eps"] / px * 100.0  # % 口径，与 build_value_growth 一致
            m["bp"] = m["bps"] / px * 100.0
        m = m.set_index("datetime")
        for f in factors:
            if f in m.columns and m[f].notna().any():
                frames[f].append(m[f].rename(sym).astype("float32"))

    return {f: pd.concat(cols, axis=1) for f, cols in frames.items() if cols}


def pit_features(
    financials: pd.DataFrame,
    feature_index: pd.MultiIndex,
    disclosure_lag_days: int = 120,
) -> pd.DataFrame:
    """构建与训练特征索引对齐的 PIT（point-in-time）价值成长特征长表。

    对 (datetime, instrument) 索引中的每个 (date, symbol)，只使用
    `date` 时点之前已实际披露（或已过保守兜底日）的最新报告期数据 —— 训练与
    实盘看到的完全一致，无前视。

    Args:
        financials: 财务快照长表（见 quart/data/factors.py）。
        feature_index: qlib 特征的 (datetime, instrument) MultiIndex。
        disclosure_lag_days: 报告期→可用最短时滞。

    Returns:
        DataFrame(index 同 feature_index)，列：
        vg_roe / vg_roe_improve / vg_profit_yoy / vg_ep / vg_bp / vg_score
    """
    if feature_index.empty:
        return pd.DataFrame()
    dates = feature_index.get_level_values(0)
    symbols = feature_index.get_level_values(1)

    fin = _normalize_financials(financials)
    fin["date"] = pd.to_datetime(fin["date"], errors="coerce")
    fin = fin.dropna(subset=["date"]).sort_values(["symbol", "date"])
    fin["usable_at"] = resolve_usable_at(fin, disclosure_lag_days)
    fin = _add_quality_candidates(fin)
    # 相邻报告期改善项（PIT 安全：改善只用历史报告期之间）
    fin["roe_improve"] = fin.groupby("symbol")["roe"].diff()
    fin["vg_ep"] = fin["eps"]
    fin["vg_bp"] = fin["bps"]

    feats = fin[
        [
            "symbol", "usable_at", "roe", "roe_improve", "profit_yoy",
            "roe_stability", "profit_accel", "earnings_surprise_proxy",
            "vg_ep", "vg_bp",
        ]
    ].rename(columns={"symbol": "instrument", "usable_at": "datetime"})
    feats = feats.dropna(subset=["datetime"])

    query = pd.DataFrame({"datetime": dates, "instrument": symbols}).reset_index(drop=True)
    # merge_asof 要求 on 键（datetime）全局有序，by 分组由其内部处理
    query = query.sort_values("datetime")

    merged = pd.merge_asof(
        query,
        feats.sort_values(["datetime", "instrument"]),
        on="datetime",
        by="instrument",
        direction="backward",
    )
    merged = merged.set_index(["datetime", "instrument"]).reindex(feature_index)
    # ep/bp 用当期 EPS/BPS 直接作特征（绝对量纲），截面 rank 留给模型/归一化层
    return merged[
        [
            "roe", "roe_improve", "profit_yoy", "roe_stability",
            "profit_accel", "earnings_surprise_proxy", "vg_ep", "vg_bp",
        ]
    ].rename(
        columns={
            "roe": "vg_roe",
            "roe_stability": "vg_roe_stability",
            "profit_accel": "vg_profit_accel",
            "earnings_surprise_proxy": "vg_earnings_surprise_proxy",
        }
    )

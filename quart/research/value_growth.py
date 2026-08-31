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
- financials.date 是**报告期**而非披露日，构建时统一回退 disclosure_lag_days
  （默认120天）避免前视；
- eps 为报告期累计口径（非 TTM），ep 因子有季节性，仅作排序用不作绝对估值。
"""
from __future__ import annotations

import pandas as pd


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
        disclosure_lag_days: 报告期→可用的最短披露时滞。

    Returns:
        DataFrame(index=symbol)，列：
        roe / roe_prev / roe_improve / profit_yoy / ep / bp / vg_score
    """
    as_of_ts = pd.Timestamp(as_of)
    usable_before = as_of_ts - pd.Timedelta(days=disclosure_lag_days)
    fin = financials.copy()
    fin["date"] = pd.to_datetime(fin["date"], errors="coerce")
    fin = fin.dropna(subset=["date"])
    fin = fin[fin["date"] <= usable_before]
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


def pit_features(
    financials: pd.DataFrame,
    feature_index: pd.MultiIndex,
    disclosure_lag_days: int = 120,
) -> pd.DataFrame:
    """构建与训练特征索引对齐的 PIT（point-in-time）价值成长特征长表。

    对 (datetime, instrument) 索引中的每个 (date, symbol)，只使用
    `date - disclosure_lag_days` 之前发布的最新报告期数据 —— 训练与
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

    fin = financials.copy()
    fin["date"] = pd.to_datetime(fin["date"], errors="coerce")
    fin = fin.dropna(subset=["date"]).sort_values(["symbol", "date"])
    fin["usable_at"] = fin["date"] + pd.Timedelta(days=disclosure_lag_days)
    # 相邻报告期改善项（PIT 安全：改善只用历史报告期之间）
    fin["roe_improve"] = fin.groupby("symbol")["roe"].diff()
    fin["vg_ep"] = fin["eps"]
    fin["vg_bp"] = fin["bps"]

    feats = fin[
        ["symbol", "usable_at", "roe", "roe_improve", "profit_yoy", "vg_ep", "vg_bp"]
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
        ["roe", "roe_improve", "profit_yoy", "vg_ep", "vg_bp"]
    ].rename(columns={"roe": "vg_roe"})

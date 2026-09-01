"""A 股事件因子：涨跌停拥挤、公告/新闻情绪与龙虎榜资金。

本模块只定义研究层的、时点安全的因子计算，不抓取外部数据，也不改变
正式策略配置。事件数据必须携带发布时间；没有本地事件文件时，调用方应
明确跳过，而不能用随机数或事后标签替代。

时点约定
--------
- 日线涨跌停因子只使用 T 日及以前的 OHLCV；信号在 T 日收盘后形成。
- 带时分秒的事件在 15:00 前发布可于当日收盘信号使用，15:00 后从下一
  个交易日可用；只有日期没有时间的记录保守地从下一交易日可用。
- 若数据源提供 ``available_at``，优先使用该字段，便于表达供应商延迟。
"""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.data.security_master import _board_of
from quart.execution.constraints import LIMIT_TOLERANCE
from quart.market_rules.rule_book import RuleBook, default_rule_book

PRICE_EVENT_FACTORS = (
    "limit_hit_count20_neg",
    "near_limit_count20_neg",
    "speculative_crowding20_neg",
)


def price_limit_panel(
    dates: pd.DatetimeIndex,
    symbols: Iterable[str],
    rule_book: RuleBook | None = None,
) -> pd.DataFrame:
    """按交易日和板块返回涨跌幅限制面板。

    IPO 无涨跌幅阶段需要真实上市日/交易日龄；研究入口已用 ``min_list_days``
    排除次新股，因此这里按存量股规则处理。ST 历史仍依赖 SecurityMaster
    状态区间；当前无状态记录时按普通上市股票处理，并在报告中保留该局限。
    """
    idx = pd.DatetimeIndex(dates)
    cols = [str(symbol) for symbol in symbols]
    book = rule_book or default_rule_book()
    out = pd.DataFrame(np.nan, index=idx, columns=cols, dtype="float32")

    grouped: dict[tuple[str, str, str], list[str]] = {}
    for symbol in cols:
        exchange, board, security_type, _ = _board_of(symbol)
        grouped.setdefault((exchange, board, security_type), []).append(symbol)

    for (exchange, board, security_type), members in grouped.items():
        values = []
        for date in idx:
            rules = book.lookup(
                date,
                exchange=exchange,
                board=board,
                security_type=security_type,
                status="listed",
            )
            values.append(np.nan if rules is None else rules.price_limit_pct)
        out.loc[:, members] = np.repeat(
            np.asarray(values, dtype="float32")[:, None], len(members), axis=1
        )
    return out


def limit_event_panels(
    market: MarketData,
    *,
    lookback: int = 20,
    near_limit_ratio: float = 0.80,
    rule_book: RuleBook | None = None,
) -> dict[str, pd.DataFrame]:
    """构建可做横截面审计的涨停/游资拥挤因子。

    三个因子均已定向为“越高越好”：近期涨停、接近涨停和放量追涨越多，
    得分越低。``speculative_crowding20_neg`` 用涨幅接近板位的程度和相对
    成交额共同衡量拥挤，比稀疏的“是否涨停”更适合全市场横截面检验。
    """
    if lookback < 2:
        raise ValueError("lookback must be >= 2")
    if not 0 < near_limit_ratio <= 1:
        raise ValueError("near_limit_ratio must be in (0, 1]")

    close = market.close_val.astype("float64")
    previous = close.shift(1)
    returns = close.pct_change(fill_method=None)
    limits = price_limit_panel(market.dates, market.symbols, rule_book).astype("float64")
    tradable = market.volumes.fillna(0).gt(0) & previous.notna()

    theoretical_up = (previous * (1.0 + limits)).round(2)
    hit = close.ge(theoretical_up - LIMIT_TOLERANCE) & tradable & limits.notna()
    progress = returns.div(limits.replace(0, np.nan)).where(tradable)
    near = progress.ge(float(near_limit_ratio)) & limits.notna()

    if market.amounts is None:
        amount_shock = pd.DataFrame(1.0, index=close.index, columns=close.columns)
    else:
        prior_adv = market.amounts.rolling(20, min_periods=10).mean().shift(1)
        amount_shock = market.amounts.div(prior_adv.replace(0, np.nan)).clip(0.0, 5.0)
    heat = (
        progress.clip(lower=0.0, upper=1.0).pow(4)
        * np.log1p(amount_shock.fillna(0.0))
    ).where(tradable, 0.0)

    return {
        "limit_hit_count20_neg": (-hit.astype("float32").rolling(lookback).sum()).astype("float32"),
        "near_limit_count20_neg": (-near.astype("float32").rolling(lookback).sum()).astype("float32"),
        "speculative_crowding20_neg": (-heat.rolling(lookback).mean()).astype("float32"),
    }


def market_limit_sentiment(
    market: MarketData,
    *,
    z_window: int = 60,
    rule_book: RuleBook | None = None,
) -> pd.DataFrame:
    """构建全市场涨跌停情绪时序，供择时研究而非横截面 RankIC。

    返回涨停/跌停家数、占可交易股票比例、净广度及滚动 z-score。该结果
    保持一维时间序列，避免把市场常数复制到每只股票后产生伪 IC。
    """
    if z_window < 5:
        raise ValueError("z_window must be >= 5")
    close = market.close_val.astype("float64")
    previous = close.shift(1)
    limits = price_limit_panel(market.dates, market.symbols, rule_book).astype("float64")
    tradable = market.volumes.fillna(0).gt(0) & previous.notna() & limits.notna()
    upper = (previous * (1.0 + limits)).round(2)
    lower = (previous * (1.0 - limits)).round(2)
    up = close.ge(upper - LIMIT_TOLERANCE) & tradable
    down = close.le(lower + LIMIT_TOLERANCE) & tradable
    denominator = tradable.sum(axis=1).replace(0, np.nan)
    up_breadth = up.sum(axis=1).div(denominator)
    down_breadth = down.sum(axis=1).div(denominator)
    net = up_breadth - down_breadth
    mean = net.rolling(z_window, min_periods=max(5, z_window // 3)).mean()
    std = net.rolling(z_window, min_periods=max(5, z_window // 3)).std().replace(0, np.nan)
    return pd.DataFrame(
        {
            "tradable_count": denominator,
            "limit_up_count": up.sum(axis=1).astype(float),
            "limit_down_count": down.sum(axis=1).astype(float),
            "limit_up_breadth": up_breadth,
            "limit_down_breadth": down_breadth,
            "limit_net_breadth": net,
            "limit_heat_z": (net - mean) / std,
        },
        index=market.dates,
    )


def neutralize_against(target: pd.DataFrame, control: pd.DataFrame) -> pd.DataFrame:
    """逐日截面回归去除单一控制因子，返回残差。

    这是无截距回归在逐日中心化后的向量化实现；每日至少 5 个共同有效值。
    用于判断事件因子是否只是低波/彩票性因子的换名版本。
    """
    target, control = target.align(control, join="outer", axis=None)
    valid = target.notna() & control.notna()
    count = valid.sum(axis=1)
    t_mean = target.where(valid).mean(axis=1)
    c_mean = control.where(valid).mean(axis=1)
    t_centered = target.sub(t_mean, axis=0).where(valid)
    c_centered = control.sub(c_mean, axis=0).where(valid)
    covariance = (t_centered * c_centered).sum(axis=1, min_count=1)
    variance = c_centered.pow(2).sum(axis=1, min_count=1).replace(0, np.nan)
    beta = covariance / variance
    residual = t_centered - c_centered.mul(beta, axis=0)
    return residual.where(count >= 5).astype("float32")


def _availability_dates(events: pd.DataFrame, trading_dates: pd.DatetimeIndex) -> pd.Series:
    """把 published_at/available_at 映射为首次可用交易日。"""
    idx = pd.DatetimeIndex(trading_dates).sort_values().normalize()
    if idx.empty:
        return pd.Series(pd.NaT, index=events.index, dtype="datetime64[ns]")
    published = pd.to_datetime(events["published_at"], errors="coerce", format="mixed")
    explicit = (
        pd.to_datetime(events["available_at"], errors="coerce", format="mixed")
        if "available_at" in events
        else pd.Series(pd.NaT, index=events.index, dtype="datetime64[ns]")
    )
    values: list[pd.Timestamp | pd.NaT] = []
    for row_index, timestamp in published.items():
        available = explicit.loc[row_index]
        if pd.notna(available):
            # 数据不可能在事件发布前真正可用；供应商脏值取两者较晚者。
            timestamp = max(available, timestamp) if pd.notna(timestamp) else available
        elif pd.isna(timestamp):
            values.append(pd.NaT)
            continue
        # 00:00 多数表示“只有日期无时间”，保守按收盘后处理。供应商
        # available_at 同样受收盘边界约束，避免 15:00 后数据回填至当日。
        has_clock = timestamp != timestamp.normalize()
        before_close = has_clock and timestamp.time() <= pd.Timestamp("15:00").time()
        side = "left" if before_close else "right"
        pos = int(idx.searchsorted(pd.Timestamp(timestamp).normalize(), side=side))
        values.append(idx[pos] if pos < len(idx) else pd.NaT)
    return pd.Series(values, index=events.index, dtype="datetime64[ns]")


def event_sentiment_panels(
    events: pd.DataFrame,
    trading_dates: pd.DatetimeIndex,
    symbols: Iterable[str],
    *,
    half_life: float = 5.0,
) -> dict[str, pd.DataFrame]:
    """把公告/新闻事件转换为衰减情绪、利好、利空和关注度面板。

    必需列：``symbol, published_at, sentiment``；sentiment 约定在 [-1, 1]。
    可选 ``confidence``、``relevance`` 和 ``available_at``。同日同票多事件先
    聚合再按交易日指数衰减，未来事件不会回填到过去。
    """
    required = {"symbol", "published_at", "sentiment"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"event data missing columns: {sorted(missing)}")
    if half_life <= 0:
        raise ValueError("half_life must be positive")

    idx = pd.DatetimeIndex(trading_dates).sort_values().normalize()
    cols = [str(symbol) for symbol in symbols]
    frame = events.copy()
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame["sentiment"] = pd.to_numeric(frame["sentiment"], errors="coerce").clip(-1.0, 1.0)
    confidence_raw = (
        frame["confidence"] if "confidence" in frame else pd.Series(1.0, index=frame.index)
    )
    relevance_raw = (
        frame["relevance"] if "relevance" in frame else pd.Series(1.0, index=frame.index)
    )
    confidence = pd.to_numeric(confidence_raw, errors="coerce").fillna(1.0).clip(0, 1)
    relevance = pd.to_numeric(relevance_raw, errors="coerce").fillna(1.0).clip(0, 1)
    frame["available_date"] = _availability_dates(frame, idx)
    frame["weight"] = confidence * relevance
    frame = frame[
        frame["available_date"].notna() & frame["symbol"].isin(cols) & frame["sentiment"].notna()
    ]

    def _panel(value: pd.Series) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(np.nan, index=idx, columns=cols, dtype="float32")
        work = frame.assign(value=value.loc[frame.index])
        daily = work.pivot_table(
            index="available_date", columns="symbol", values="value", aggfunc="sum"
        ).reindex(index=idx, columns=cols, fill_value=0.0).fillna(0.0)
        # 事件是脉冲而非平稳观测：当日完整计入，之后按半衰期衰减。
        decay = float(0.5 ** (1.0 / half_life))
        output = np.empty(daily.shape, dtype="float64")
        state = np.zeros(len(cols), dtype="float64")
        values = daily.to_numpy(dtype="float64", copy=False)
        for row_index in range(len(daily)):
            state = state * decay + values[row_index]
            output[row_index] = state
        return pd.DataFrame(output, index=idx, columns=cols, dtype="float32")

    weighted = frame["sentiment"] * frame["weight"] if not frame.empty else pd.Series(dtype=float)
    positive = weighted.clip(lower=0)
    negative = (-weighted.clip(upper=0))
    attention = frame["weight"] if not frame.empty else pd.Series(dtype=float)
    return {
        "event_sentiment_decay": _panel(weighted),
        "good_news_decay": _panel(positive),
        "bad_news_decay_neg": -_panel(negative),
        "event_attention_decay": _panel(attention),
    }


def dragon_tiger_panels(
    events: pd.DataFrame,
    trading_dates: pd.DatetimeIndex,
    symbols: Iterable[str],
    *,
    half_life: float = 3.0,
) -> dict[str, pd.DataFrame]:
    """把龙虎榜净买入转换为短衰减资金因子。

    必需列为 ``symbol, published_at, net_buy_amount, turnover_amount``；可选
    ``institution_net_buy_amount``。榜单是选择性披露样本，未上榜仅代表无事件，
    不得解释为净买入为零的完整市场观测。
    """
    required = {"symbol", "published_at", "net_buy_amount", "turnover_amount"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"dragon-tiger data missing columns: {sorted(missing)}")
    turnover = pd.to_numeric(events["turnover_amount"], errors="coerce").replace(0, np.nan)
    net_ratio = pd.to_numeric(events["net_buy_amount"], errors="coerce").div(turnover).clip(-1, 1)
    institution_raw = (
        events["institution_net_buy_amount"]
        if "institution_net_buy_amount" in events
        else pd.Series(0.0, index=events.index)
    )
    institution = pd.to_numeric(institution_raw, errors="coerce").div(turnover).clip(-1, 1)
    base = events[[c for c in events.columns if c in {"symbol", "published_at", "available_at"}]].copy()
    base["confidence"] = 1.0
    base["relevance"] = 1.0

    net_events = base.assign(sentiment=net_ratio)
    inst_events = base.assign(sentiment=institution)
    return {
        "dragon_tiger_net_buy_decay": event_sentiment_panels(
            net_events, trading_dates, symbols, half_life=half_life
        )["event_sentiment_decay"],
        "dragon_tiger_institution_decay": event_sentiment_panels(
            inst_events, trading_dates, symbols, half_life=half_life
        )["event_sentiment_decay"],
    }


__all__ = [
    "PRICE_EVENT_FACTORS",
    "dragon_tiger_panels",
    "event_sentiment_panels",
    "limit_event_panels",
    "market_limit_sentiment",
    "neutralize_against",
    "price_limit_panel",
]

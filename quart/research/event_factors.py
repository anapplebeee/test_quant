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

from quart.data.announcements import classify_event
from quart.data.market import MarketData
from quart.data.security_master import _board_of
from quart.execution.constraints import LIMIT_TOLERANCE
from quart.market_rules.rule_book import RuleBook, default_rule_book

PRICE_EVENT_FACTORS = (
    "limit_hit_count20_neg",
    "near_limit_count20_neg",
    "speculative_crowding20_neg",
    "crowding_liq20_neg",
    "sector_heat20_neg",
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

    panels = {
        "limit_hit_count20_neg": (-hit.astype("float32").rolling(lookback).sum()).astype("float32"),
        "near_limit_count20_neg": (-near.astype("float32").rolling(lookback).sum()).astype("float32"),
        "speculative_crowding20_neg": (-heat.rolling(lookback).mean()).astype("float32"),
        **liquidity_adjacent_crowding(heat, market.amounts, lookback=lookback),
    }
    try:
        from quart.strategy.industries import load_industry_series

        sector = sector_heat_panel(heat, load_industry_series("first"))
    except (FileNotFoundError, ValueError):
        sector = None
    if sector is not None:
        panels["sector_heat20_neg"] = sector
    return panels


def liquidity_adjacent_crowding(
    heat: pd.DataFrame,
    amounts: pd.DataFrame | None,
    *,
    lookback: int = 20,
    floor_quantile: float = 0.2,
) -> dict[str, pd.DataFrame]:
    """容量化拥挤反向：同等投机热度下，流动性越差的股票扣分越重。

    RESEARCH-002 复盘指出的容量死刑：纯事件拥挤因子的 Top 篮子集中在
    小票（10% ADV 容量代理仅 ~700 万元），2 倍成本下 CAGR 转负。本算子把
    "可投资容量"直接编码进因子值——热度除以 ADV 横截面分位（下限截断），
    使"最不拥挤"的选股结果自动偏向高 ADV 股票，Top 篮子容量成倍放大，
    同时保留"追涨透支 → 未来负收益"的核心信息。
    """
    if not 0 < floor_quantile <= 1:
        raise ValueError("floor_quantile must be in (0, 1]")
    if amounts is None:
        # 无成交额数据时退化为原始拥挤（不加容量权重），保持时点安全。
        return {}
    adv = amounts.rolling(lookback, min_periods=lookback // 2).mean()
    adv_quantile = adv.rank(axis=1, pct=True)
    # 下限截断：最低分位的股票最多放大 floor_quantile 倍扣分，避免极小
    # 流动性股票的因子值被噪声主导。
    liq_weight = adv_quantile.clip(lower=floor_quantile)
    adjusted = (-heat.div(liq_weight)).rolling(lookback).mean()
    return {"crowding_liq20_neg": adjusted.astype("float32")}


def sector_heat_panel(
    heat: pd.DataFrame,
    industry_mapping: pd.Series,
) -> pd.DataFrame | None:
    """板块层拥挤：个股投机热度聚合到一级行业后 broadcast 回板块内个股。

    RESEARCH-002 复盘的方向之一：个股层容量小，板块层的容量与换手结构
    完全不同。板块平均热度捕捉的是"资金在板块层面的聚集"，与个股自身
    拥挤（heat）相关性低，可作为独立的横截面维度审计。返回负向面板
    （高板块热度 → 未来跑输），无法获得行业映射时返回 None。
    """
    if heat.empty or industry_mapping is None or len(industry_mapping) == 0:
        return None
    groups = pd.Series(
        [industry_mapping.get(symbol, "UNKNOWN") for symbol in heat.columns],
        index=heat.columns,
    )
    industry_heat = heat.T.groupby(groups).mean().T
    broadcast = industry_heat.reindex(columns=groups.values)
    broadcast.columns = heat.columns
    return (-broadcast.rolling(20).mean()).astype("float32")


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


#: 内部人减持相关人（董事/高管/监事/实控人），用于精确筛选"内部人抛售"
#: 而非一般股东。注意**不含**"持股5%以上"——那会命中财务/战略投资者等
#: 非内部人（全市场约 4733 条），污染"董事抛售"的信号（用户问的是董事）。
DIRECTOR_SALE_PERSON_REGEX = r"董事|高管|高级管理人员|监事|董监高|实际控制人|实控人"


def director_sale_support_panels(
    events: pd.DataFrame,
    trading_dates: pd.DatetimeIndex,
    symbols: Iterable[str],
    returns: pd.DataFrame | None = None,
    *,
    support_window: int = 15,
    is_director_col: str | None = None,
) -> dict[str, pd.DataFrame]:
    """内部人减持拉升/支撑行为因子（PROVISIONAL，见假设卡）。

    机制：董事/高管/实控人在减持计划预披露后进入减持窗口（一般 ≤15 个交易
    日），有动机在窗口内维持甚至拉抬股价，以便在更高价位减持。一旦窗口
    结束，支撑撤走，前期被支撑的涨幅大概率回吐。因此"减持窗口内拉升越猛"
    是**负向**信号——未来倾向跑输，方向与直觉相反但符合均值回归。

    时点安全（协议不变量 1、6）：
    - 事件仅用 ``published_at/available_at`` 映射到**首次可用交易日**
      （公告无时分秒 → 下一交易日），绝不使用实际减持日或未来数据；
    - 选择性披露：未发生减持事件的股票-日期置 ``NaN``，**不是 0**，同时
      单独返回 ``active_mask`` 区分"未披露"与"披露的零拉升"；
    - 拉升度量（窗口内相对强度）只使用事件可用日及**之后**的价格，且该
      价格是 T+1 才可执行，因此对 T 收盘信号是时点安全的。

    必需列：``symbol, published_at``；``event_type`` 若缺失则按标题
    ``classify_event`` 判定。``title`` 用于筛内部人身份。``returns`` 为
    日收益横截面面板（index=交易日, columns=symbol），用于计算窗口内相对
    强度；缺失时仅返回 active 掩码（拉升因子为 NaN）。

    返回（面板与掩码同序，可按日期 index、symbol 列对齐）：
    - ``director_sale_support_neg``：减持窗口内相对强度（**负向**，值越大
      表示前期被拉抬越狠 → 未来越差）。仅事件窗口内的股票-日期有值，其余
      为 NaN。窗口内但无有效价格（停牌）时保持 NaN。
    - ``director_sale_active``：事件活跃掩码（1=事件窗口内，NaN=无事件），
      用于与事件活跃等权基线对比（协议 §诊断 6）。
    """
    required = {"symbol", "published_at"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"event data missing columns: {sorted(missing)}")
    if support_window <= 0:
        raise ValueError("support_window must be positive")

    idx = pd.DatetimeIndex(trading_dates).sort_values().normalize()
    cols = [str(symbol) for symbol in symbols]
    empty = {
        "director_sale_support_neg": pd.DataFrame(np.nan, index=idx, columns=cols, dtype="float32"),
        "director_sale_active": pd.DataFrame(np.nan, index=idx, columns=cols, dtype="float32"),
    }

    frame = events.copy()
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    if "event_type" not in frame:
        frame["event_type"] = frame["title"].astype(str).map(classify_event)
    red = frame[frame["event_type"] == "share_reduction"].copy()
    if red.empty:
        return empty
    # 内部人筛选：标题含董事/高管/实控人/持股5%以上；若调用方已给出
    # is_director 列则优先使用。
    if is_director_col is not None and is_director_col in red:
        red = red[red[is_director_col].fillna(False).astype(bool)]
    elif "title" in red:
        red = red[red["title"].astype(str).str.contains(DIRECTOR_SALE_PERSON_REGEX, na=False)]
    if red.empty:
        return empty

    red["available_date"] = _availability_dates(red, idx)
    red = red[red["available_date"].notna() & red["symbol"].isin(cols)]
    if red.empty:
        return empty

    active = pd.DataFrame(np.nan, index=idx, columns=cols, dtype="float32")
    pos = {d: k for k, d in enumerate(idx)}
    for _, row in red.iterrows():
        start = pos.get(row["available_date"])
        if start is None:
            continue
        col = row["symbol"]
        end = min(start + support_window, len(idx))
        active.iloc[start:end, active.columns.get_loc(col)] = 1.0

    # 拉升度量：事件窗口内的**逐日累计**相对强度。用个股收益减去当日横
    # 截面均值（等权市场收益）得到相对收益，再从窗口起点逐日累加——T 日
    # 的信号只使用 ≤T 的数据（协议不变量 1：不把未来拉到过去）。值越大
    # 表示截至 T 窗口内相对市场被拉抬越狠（减持支撑信号）。这是研究层的
    # 因子值，不是组合权重。
    if returns is not None:
        returns = returns.reindex(index=idx, columns=cols)
        rel = returns.sub(returns.mean(axis=1), axis=0)
        support = pd.DataFrame(np.nan, index=idx, columns=cols, dtype="float32")
        for _, row in red.iterrows():
            start = pos.get(row["available_date"])
            if start is None:
                continue
            col = row["symbol"]
            end = min(start + support_window, len(idx))
            if end - start < 1:
                continue
            window_rel = rel.iloc[start:end, rel.columns.get_loc(col)]
            if window_rel.dropna().empty:
                continue
            # 逐日累计：第 k 天的值 = 窗口起点到第 k 天的相对收益累计，
            # 保证 T 日信号不包含 T 之后的量价。
            cumsum = window_rel.fillna(0.0).cumsum().astype("float32")
            support.iloc[start:end, support.columns.get_loc(col)] = cumsum.to_numpy()
    else:
        support = pd.DataFrame(np.nan, index=idx, columns=cols, dtype="float32")

    return {
        "director_sale_support_neg": support.astype("float32"),
        "director_sale_active": active.astype("float32"),
    }


__all__ = [
    "DIRECTOR_SALE_PERSON_REGEX",
    "PRICE_EVENT_FACTORS",
    "director_sale_support_panels",
    "dragon_tiger_panels",
    "event_sentiment_panels",
    "limit_event_panels",
    "market_limit_sentiment",
    "neutralize_against",
    "price_limit_panel",
]

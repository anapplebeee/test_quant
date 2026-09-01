"""公司公告/新闻事件结构化层（RESEARCH-002 §8-4，P1）。

把公告标题流转换为带情绪与置信度的结构化事件：

1. ``classify_event``：规则分类为可交易事件类型（业绩预告/快报、分红回购、
   监管处罚、诉讼、减持增持、重组等）；
2. ``rule_sentiment``：事件类型 + 关键词的规则情绪标签（弱监督来源）；
3. 模型情绪由 ``scripts/train_event_sentiment.py`` 在规则标签上做
   **时间切分**训练（TimeSeriesSplit），预测保留置信度 —— 规则标签与
   模型预测都记录，情绪列可配置取哪个来源。

时点合同（§3.4）：``published_at`` 仅有日期的记录保守地从下一交易日可用；
带时分秒的记录 15:00 前当日可用（由 ``event_factors._availability_dates``
统一执行，本模块只负责打标签）。
"""
from __future__ import annotations

import re

import pandas as pd

#: 事件类型 → (正则模式列表, 规则情绪, 规则置信度)
EVENT_RULES: dict[str, tuple[tuple[str, ...], int, float]] = {
    "earnings_forecast": ((
        r"业绩预告", r"业绩预[增减盈亏]", r"预计.{0,6}(净利润|业绩|盈利|亏损)",
    ), 0, 0.5),  # 方向由预告关键词细分，见 _forecast_sentiment
    "earnings_flash": ((r"业绩快报",), 0, 0.6),
    "dividend": ((r"利润分配", r"分红", r"派息", r"派发现金", r"权益分派"), 1, 0.6),
    "buyback": ((r"回购", r"购回股份"), 1, 0.7),
    "penalty": ((r"处罚", r"警告", r"罚款", r"立案[调查告]", r"监管函", r"问询函",
                 r"警示函", r"公开谴责", r"纪律处分"), -1, 0.8),
    "lawsuit": ((r"诉讼", r"仲裁", r"起诉", r"被诉"), -1, 0.6),
    "share_reduction": ((r"减持",), -1, 0.6),
    "share_increase": ((r"增持", r"举牌", r"要约收购"), 1, 0.6),
    "pledge": ((r"质押", r"冻结"), -1, 0.4),
    "restructuring": ((r"重组", r"收购", r"资产注入", r"借壳", r"定增", r"非公开发行",
                       r"发行股份购买资产"), 1, 0.4),
    "delisting_risk": ((r"退市风险", r"终止上市", r"摘牌", r"\*ST"), -1, 0.9),
}

_EVENT_TYPE_COLUMN = "event_type"
SENTIMENT_COLUMN = "sentiment"
CONFIDENCE_COLUMN = "confidence"


def classify_event(title: str) -> str:
    """公告标题 → 事件类型（未命中 → ``other``）。"""
    text = str(title)
    for event_type, (patterns, _, _) in EVENT_RULES.items():
        for pat in patterns:
            if re.search(pat, text):
                return event_type
    return "other"


def _forecast_sentiment(title: str) -> int:
    """业绩预告/快报的方向细分（预增/预盈 +1，预减/预亏 -1）。"""
    if re.search(r"预[增盈]|扭亏|增长|盈利", title):
        return 1
    if re.search(r"预[减亏]|亏损|下降|下滑", title):
        return -1
    return 0


def rule_sentiment(title: str, event_type: str | None = None) -> tuple[int, float]:
    """规则情绪：(sentiment, confidence)。未命中事件 → (0, 0.1)。"""
    et = event_type or classify_event(title)
    if et not in EVENT_RULES:
        return 0, 0.1
    _, sentiment, confidence = EVENT_RULES[et]
    if et in ("earnings_forecast", "earnings_flash"):
        sentiment = _forecast_sentiment(title)
        if sentiment == 0:
            return 0, 0.3
        return sentiment, confidence
    return sentiment, confidence


def build_event_frame(raw: pd.DataFrame, fetched_at: pd.Timestamp | None = None) -> pd.DataFrame:
    """公告原始表 → 事件合同表（RESEARCH-002 §3.4 列）。

    Args:
        raw: 需含 ``代码, 公告标题, 公告日期``（可选 ``公告类型``）。

    Returns:
        symbol, published_at, sentiment, confidence, relevance, available_at,
        event_type, rule_sentiment, title, source
    """
    need = {"代码", "公告标题", "公告日期"}
    missing = need - set(raw.columns)
    if missing:
        raise ValueError(f"announcement raw missing columns: {sorted(missing)}")
    ts = fetched_at or pd.Timestamp.now()
    out = pd.DataFrame({
        "symbol": raw["代码"].astype(str).str.zfill(6),
        "title": raw["公告标题"].astype(str),
        "published_at": pd.to_datetime(raw["公告日期"], errors="coerce", format="mixed"),
    })
    out = out.dropna(subset=["published_at"])
    out[_EVENT_TYPE_COLUMN] = out["title"].map(classify_event)
    labeled = [rule_sentiment(t, e) for t, e in zip(out["title"], out[_EVENT_TYPE_COLUMN])]
    out["rule_sentiment"] = [s for s, _ in labeled]
    out[CONFIDENCE_COLUMN] = [c for _, c in labeled]
    # 情绪列默认取规则标签；模型来源由 train_event_sentiment.py 覆盖写回
    out[SENTIMENT_COLUMN] = out["rule_sentiment"].astype(float)
    out["relevance"] = 1.0
    # 回填数据：available_at = published_at（仅日期）。事件映射函数会把
    # 仅日期的 available_at 推迟到下一交易日，保证 PIT（公告通常盘后可见）。
    # 不写抓取时刻 —— 那会把全部事件推到未来，面板变成空。
    out["available_at"] = out["published_at"]
    out["source"] = "notice_report_em"
    # 未分类事件不进情绪研究流（噪声占比过高）
    return out[out[_EVENT_TYPE_COLUMN] != "other"].reset_index(drop=True)


def merge_forecast_events(forecast_raw: pd.DataFrame, fetched_at: pd.Timestamp | None = None) -> pd.DataFrame:
    """业绩预告接口（yjyg）行 → 事件合同行（预告类型直接给方向）。"""
    if forecast_raw.empty:
        return pd.DataFrame()
    ts = fetched_at or pd.Timestamp.now()
    direction = {"预增": 1, "略增": 1, "扭亏": 1, "预减": -1, "略减": -1, "首亏": -1,
                 "续亏": -1, "预亏": -1, "不确定": 0}
    out = pd.DataFrame({
        "symbol": forecast_raw["股票代码"].astype(str).str.zfill(6),
        "title": ("业绩预告：" + forecast_raw["预告类型"].astype(str) + " "
                  + forecast_raw["业绩变动"].fillna("").astype(str)),
        "published_at": pd.to_datetime(forecast_raw["公告日期"], errors="coerce", format="mixed"),
    })
    out = out.dropna(subset=["published_at"])
    out[_EVENT_TYPE_COLUMN] = "earnings_forecast"
    out["rule_sentiment"] = forecast_raw["预告类型"].map(direction).fillna(0).astype(float)
    out[CONFIDENCE_COLUMN] = 0.9  # 结构化字段，方向明确
    out[SENTIMENT_COLUMN] = out["rule_sentiment"]
    out["relevance"] = 1.0
    # 同 build_event_frame：available_at = published_at（仅日期，下一交易日可用）
    out["available_at"] = out["published_at"]
    out["source"] = "yjyg_em"
    return out

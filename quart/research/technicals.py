"""个股技术指标因子库（截面因子面板）。

研报来源与选材依据：
- R3（光大《司空见惯叙指标》2018-11）：对 30+ 常用技术指标按"胜率×盈亏比×
  行业稳定性"三维度系统评价，最终推荐 8 个：MACD、DDI、DMA、TRIX、B3612、
  ENV、EMV、TAPI（前 5 个趋势/反趋势类全部收录）。
- R4（广发《125个经典技术指标择时分析》2020-01）：125 个指标在沪深300 上
  多空回测，动量类头部：ER(27.4%)、DPO(26.3%)、TII(25.7%)、MAAMT(29.6%)；
  价量类头部：VWAP(23.6%)。按研究结论收录 ER/DPO/TII/MAAMT/VWAP。

所有因子均为"逐符号时间序列→宽表面板"（index=date, columns=symbol），
使用滚动窗口计算，无前视（当日值仅用含当日的过去数据，回测侧由
signal_offset 再减一天执行，与本模块无关）。

用法：
    from quart.research.technicals import compute_panels
    panels = compute_panels(md)          # dict[str, pd.DataFrame]
    ic = factor_ic(panels["er"], fwd_ret)  # 供因子研究页/ML 特征使用
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.research.momentum import rank_momentum, remove_limit_up_momentum, signed_smooth

#: 因子注册表：name -> (类别, 说明)。新增因子需同步注册，因子研究页自动展示。
FACTOR_CATALOG: dict[str, tuple[str, str]] = {
    # ---- R3 推荐：趋势类 ----
    "macd_dif": ("trend", "MACD DIF/收盘价：EMA12-EMA26 归一化"),
    "dma": ("trend", "平均差：MA10-MA50 归一化，交叉信号快于 MACD"),
    "trix": ("trend", "三重指数平滑均线变化率（12），长期趋势"),
    # ---- R3 推荐：反趋势类 ----
    "b3612": ("mean_revert", "3/6/12 日乖离组合 B3612"),
    "env_pos": ("mean_revert", "ENV 通道位置（MA20±2σ 标准化）"),
    # ---- R3 推荐：量价类 ----
    "emv": ("pv", "简易波动指标（20 日均值，量标准化）"),
    # ---- R4 动量类头部 ----
    "er": ("trend", "效率比率（20）：净位移/路径总长，趋势质量"),
    "dpo": ("trend", "去趋势价格振荡器（20），剔除长周期后短期循环"),
    "tii": ("trend", "趋势强度（20）：收盘价站上 MA20 的天数占比"),
    # ---- R4 量能/价量类头部 ----
    "maamt": ("volume", "成交额均线比 MA5/MA20（量能加速度）"),
    "vwap_dev": ("pv", "收盘价相对 20 日滚动 VWAP 偏离"),
    # ---- R9 路径感知动量 ----
    "rank_mom120_20": ("path_momentum", "120日动量跳过最近20日的横截面排名"),
    "smooth240": ("path_momentum", "240日带方向路径平滑度"),
    "ret240_20_remove_uplimit": (
        "path_momentum",
        "剔除涨停日贡献后的240/20日复合动量",
    ),
}


def _ema(s: pd.DataFrame, span: int) -> pd.DataFrame:
    return s.ewm(span=span, adjust=False).mean()


def macd_dif(md: MarketData) -> pd.DataFrame:
    """MACD DIF（12,26）按收盘价归一化，消除价格量纲。"""
    dif = _ema(md.closes, 12) - _ema(md.closes, 26)
    return dif / md.closes


def dma(md: MarketData) -> pd.DataFrame:
    """DMA 平均差（10,50）归一化：短期均线上方程度。"""
    return (md.closes.rolling(10).mean() - md.closes.rolling(50).mean()) / md.closes


def trix(md: MarketData) -> pd.DataFrame:
    """TRIX(12)：三重 EMA 的日变化率，长期趋势方向的平滑表达。"""
    e1 = _ema(md.closes, 12)
    e2 = _ema(e1, 12)
    e3 = _ema(e2, 12)
    return e3.pct_change(fill_method=None)


def b3612(md: MarketData) -> pd.DataFrame:
    """B3612 乖离组合：BIAS3 - BIAS6 + BIAS12（R3 反趋势推荐）。"""
    ma3 = md.closes.rolling(3).mean()
    ma6 = md.closes.rolling(6).mean()
    ma12 = md.closes.rolling(12).mean()
    return (md.closes - ma3) / ma3 - (md.closes - ma6) / ma6 + (md.closes - ma12) / ma12


def env_pos(md: MarketData, window: int = 20) -> pd.DataFrame:
    """ENV 通道位置：收盘价在 MA±k·σ 通道中的标准化位置（约 ±1 界定通道边）。"""
    ma = md.closes.rolling(window).mean()
    sd = md.closes.rolling(window).std()
    return (md.closes - ma) / (2.0 * sd)


def emv(md: MarketData, window: int = 20) -> pd.DataFrame:
    """简易波动指标 EMV：价格中点位移 / （成交量/价格振幅 的"箱体比率"）。

    原始 EMV 量纲随价格/股本漂移，这里再做 20 日滚动均值平滑。
    成交量缺失（0/NaN）时结果为 NaN，不伪造。
    """
    mid = (md.highs + md.lows) / 2.0
    mid_shift = mid.shift(1)
    box = (md.volumes / 1e8) / (md.highs - md.lows).replace(0, np.nan)
    raw = (mid - mid_shift) * (md.highs - md.lows) / box.replace(0, np.nan)
    return raw.rolling(window).mean()


def er(md: MarketData, window: int = 20) -> pd.DataFrame:
    """效率比率 ER：|净位移| / Σ|逐日位移|（R4 动量类第一，年化 27.4%）。

    趋势市中接近 1，震荡市接近 0 —— 度量"趋势质量"而非方向。
    """
    change = md.closes.diff()
    net = md.closes.diff(window).abs()
    path = change.abs().rolling(window).sum().replace(0, np.nan)
    return net / path


def dpo(md: MarketData, window: int = 20) -> pd.DataFrame:
    """去趋势价格振荡器 DPO：close - MA(20) 前移 window/2+1，归一化。

    剔除长周期趋势后暴露短周期循环位置（R4 动量类第二）。
    """
    shifted = window // 2 + 1
    dpo_raw = md.closes - md.closes.rolling(window).mean().shift(shifted)
    return dpo_raw / md.closes


def tii(md: MarketData, window: int = 20) -> pd.DataFrame:
    """趋势强度指数 TII：近 window 日中收盘价高于 MA(window) 的天数占比。

    R4 动量类第三（年化 25.7%），无前视：当日计入窗口内。
    """
    ma = md.closes.rolling(window).mean()
    above = (md.closes > ma).astype(float)
    return above.rolling(window).mean()


def maamt(md: MarketData) -> pd.DataFrame:
    """成交额均线比 MA5/MA20（R4 成交量类第一 MAAMT，年化 29.6%）。

    >1 代表量能加速，量在价先。
    """
    if md.amounts is None:
        raise ValueError("maamt requires amounts panel")
    ma5 = md.amounts.rolling(5).mean()
    ma20 = md.amounts.rolling(20).mean().replace(0, np.nan)
    return ma5 / ma20


def vwap_dev(md: MarketData, window: int = 20) -> pd.DataFrame:
    """收盘价相对滚动 VWAP 偏离：close / (Σamount/Σvol) - 1（R4 价量类头部）。"""
    if md.amounts is None or md.volumes is None:
        raise ValueError("vwap_dev requires amounts & volumes panels")
    amount = md.amounts.rolling(window, min_periods=window).sum()
    volume = md.volumes.rolling(window, min_periods=window).sum().replace(0, np.nan)
    vwap = amount / volume
    return md.closes / vwap - 1.0


def rank_mom120_20(md: MarketData) -> pd.DataFrame:
    """报告 R9 的 RankMom(120,20)。"""
    return rank_momentum(md.closes, window=120, skip_days=20)


def smooth240(md: MarketData) -> pd.DataFrame:
    """报告 R9 的带方向 Smooth(240)。"""
    return signed_smooth(md.closes, window=240, skip_days=0)


def ret240_20_remove_uplimit(md: MarketData) -> pd.DataFrame:
    """报告 R9 的剔除涨停日收益的 240/20 动量。"""
    return remove_limit_up_momentum(md.closes, window=240, skip_days=20)


#: 计算器注册表（name -> callable(md)）
_COMPUTERS = {
    "macd_dif": macd_dif,
    "dma": dma,
    "trix": trix,
    "b3612": b3612,
    "env_pos": env_pos,
    "emv": emv,
    "er": er,
    "dpo": dpo,
    "tii": tii,
    "maamt": maamt,
    "vwap_dev": vwap_dev,
    "rank_mom120_20": rank_mom120_20,
    "smooth240": smooth240,
    "ret240_20_remove_uplimit": ret240_20_remove_uplimit,
}


def stack_panels(panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """把宽表面板堆叠为训练用长表：MultiIndex(datetime, instrument) × 因子列。

    与 qlib Alpha158 fetch 出的特征索引对齐（export_to_qlib.py 用原始 symbol
    作为 instrument 代码，因此 stack 的 symbol 可直接 join）。
    """
    stacked = []
    for name, panel in panels.items():
        s = panel.stack(future_stack=True).rename(name)
        stacked.append(s)
    if not stacked:
        return pd.DataFrame()
    out = pd.concat(stacked, axis=1)
    out.index = out.index.set_names(["datetime", "instrument"])
    # future_stack 保留全 NaN 行，去掉以减小体积
    return out.dropna(how="all")


def compute_panels(
    md: MarketData,
    names: list[str] | None = None,
    min_history: int = 60,
) -> dict[str, pd.DataFrame]:
    """批量计算技术指标因子面板。

    Args:
        md: 行情面板。
        names: 要计算的因子名（默认全部）。
        min_history: 不足该历史长度的符号整列剔除（前 min_history 行不足以
            计算长窗口指标，保留全 NaN 列只会污染截面排名）。

    Returns:
        dict[name, DataFrame(index=date, columns=symbol)]
    """
    selected = names or list(_COMPUTERS)
    out: dict[str, pd.DataFrame] = {}
    for name in selected:
        if name not in _COMPUTERS:
            raise KeyError(f"unknown technical factor: {name}, known: {sorted(_COMPUTERS)}")
        panel = _COMPUTERS[name](md)
        if min_history > 0 and not panel.empty:
            # 剔除"近期才上市"的符号：首个有效值出现得过晚（距末日不足
            # min_history 行），说明历史不足以支撑长窗口指标，截面排名会失真。
            # 注意 EMA 类因子从序列起点就有值，first_valid 在开头属正常保留。
            n = len(panel)
            keep = [
                c for c in panel.columns
                if panel[c].first_valid_index() is not None
                and n - panel.index.get_loc(panel[c].first_valid_index()) >= min_history
            ]
            panel = panel[keep]
        out[name] = panel
    return out

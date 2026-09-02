"""智兔数服 A 股分钟K线数据源（research 级，2026-09-02 接入）。

接口文档：https://www.zhituapi.com/hsstockapi.html（智兔数服）
实测（token 验证，2026-09-02）：
- 历史分钟K线：GET https://api.zhituapi.com/hs/history/{code}.{exch}/{level}/{adj}
     参数 st=YYYYMMDD & et=YYYYMMDD，返回 JSON 数组，每项字段:
     {o,h,l,c,v,a,pc,t} = open/high/low/close/volume/amount/prev_close/time
- 最新K线：   GET .../latest/{code}.{exch}/{level}/{adj}?limit=N
- level 支持 5/15/30/60/d/w/m/y（**最低 5 分钟，无 1 分钟**）
- adj：n=不复权、f=前复权、b=后复权等
- **历史回溯起点：约 2023 年中**（2023-06 之前返回空），实测 5 分钟可回溯近 2.5-3 年

鉴权
----
token 从环境变量 ``ZHITU_API_TOKEN`` 读取（不落版本库，settings.yaml 已被 git 跟踪
不适合放 secret）。未设置时调用会明确报错而非静默返回空（fail-closed）。
也可显式 ``ZhituSource(token="...")`` 注入（测试/脚本临时场景）。

频控
----
体验/包月 1000 次/分，包年版 3000 次/分。批量抓取须按时间/天切片并限速。
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass

import pandas as pd
import requests
from loguru import logger

_API_BASE = "https://api.zhituapi.com/hs"
_TOKEN_ENV = "ZHITU_API_TOKEN"
_RETRYABLE = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
_LEVELS = {"5", "15", "30", "60", "d", "w", "m", "y"}
_ADJS = {"n", "f", "b", ""}

_MINUTE_COLUMNS = ["ts", "open", "high", "low", "close", "volume", "amount"]
#: 分钟级数据最早回溯起点（实测：约 2023-06 前为空）。抓取早于此会被跳过。
MINUTE_HISTORY_START = pd.Timestamp("2023-06-01")


def exchange_suffix(symbol: str) -> str:
    """按 6 位代码返回智兔所需交易所后缀（.SH/.SZ/.BJ）。"""
    code = str(symbol).zfill(6)
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    return "BJ"  # 4/8/9(除9开头沪B?)——北交所/三板按 BJ


def _token() -> str:
    tok = os.environ.get(_TOKEN_ENV, "").strip()
    if not tok:
        raise RuntimeError(
            f"缺少智兔 token：请设置环境变量 {_TOKEN_ENV}（勿写进版本库的 settings.yaml）"
        )
    return tok


@dataclass
class ZhituSource:
    """智兔数服 A 股分钟行情客户端。线程安全（请求间无共享可变状态）。"""

    token: str | None = None
    sleep_seconds: float = 0.15
    retries: int = 3

    def __post_init__(self):
        self._token = self.token or _token()

    # ---------------- 原始请求 ----------------

    def _get(self, url: str, timeout: float = 20.0) -> list:
        """带指数退避重试的 GET；网络层错误重试，其余直接抛。"""
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                payload = r.json()
            except _RETRYABLE as exc:
                last_exc = exc
                if attempt == self.retries:
                    raise
                time.sleep(0.6 * (2 ** (attempt - 1)) + random.uniform(0, 0.4))
                continue
            if isinstance(payload, dict) and "msg" in payload and "code" in payload:
                # 智兔错误以 {code,msg} 返回（如 404 无数据 / 421 超配额）
                code = payload.get("code")
                msg = str(payload.get("msg", ""))
                if code == 200:
                    return payload.get("data") or []
                raise RuntimeError(f"zhitu api error {code}: {msg}")
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"] or []
            if isinstance(payload, list):
                return payload
            raise RuntimeError(f"zhitu unexpected payload type: {type(payload).__name__}")
        raise last_exc  # pragma: no cover

    def fetch_minute_kline(
        self,
        symbol: str,
        level: str = "5",
        start_date: str | None = None,
        end_date: str | None = None,
        adj: str = "n",
    ) -> pd.DataFrame:
        """拉取单只股票的历史分钟K线。

        返回列 ts/open/high/low/close/volume/amount（ts 为纳秒时间戳，已排序去重）。
        ``end_date`` 缺省=今日；``start_date`` 缺省=分钟历史起点。返回空 df 且不抛错
        当该区间无数据（数据源空 / 早于回溯起点 / 停牌）。
        """
        if level not in _LEVELS:
            raise ValueError(f"unsupported zhitu level: {level!r}（支持 5/15/30/60/d/w/m/y）")
        adj = adj or "n"
        if adj not in _ADJS:
            raise ValueError(f"unsupported zhitu adjust: {adj!r}")

        end = pd.Timestamp(end_date) if end_date else pd.Timestamp.today()
        start = pd.Timestamp(start_date) if start_date else MINUTE_HISTORY_START
        # 数据源只回溯到约 2023 年中；区间更早部分直接返回空（不浪费请求）
        if end < MINUTE_HISTORY_START:
            return pd.DataFrame(columns=_MINUTE_COLUMNS)

        url = (
            f"{_API_BASE}/history/{str(symbol).zfill(6)}.{exchange_suffix(symbol)}/"
            f"{level}/{adj}?token={self._token}&st={start:%Y%m%d}&et={min(end, pd.Timestamp.today()):%Y%m%d}"
        )
        rows = self._get(url)
        if not rows:
            return pd.DataFrame(columns=_MINUTE_COLUMNS)
        df = pd.DataFrame(rows)
        if df.empty or "t" not in df.columns:
            return pd.DataFrame(columns=_MINUTE_COLUMNS)
        out = pd.DataFrame(
            {
                "ts": pd.to_datetime(df["t"], format="%Y-%m-%d %H:%M:%S", errors="coerce"),
                "open": pd.to_numeric(df.get("o"), errors="coerce"),
                "high": pd.to_numeric(df.get("h"), errors="coerce"),
                "low": pd.to_numeric(df.get("l"), errors="coerce"),
                "close": pd.to_numeric(df.get("c"), errors="coerce"),
                "volume": pd.to_numeric(df.get("v"), errors="coerce"),
                "amount": pd.to_numeric(df.get("a"), errors="coerce"),
            }
        )
        out = out.dropna(subset=["ts"]).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
        return out[_MINUTE_COLUMNS]

    def close(self) -> None:  # noqa: B027 - 无连接池，纯 HTTP，无需清理
        return None


__all__ = ["ZhituSource", "fetch_minute_kline", "exchange_suffix", "MINUTE_HISTORY_START"]


def fetch_minute_kline(
    symbol: str,
    level: str = "5",
    start_date: str | None = None,
    end_date: str | None = None,
    adj: str = "n",
    token: str | None = None,
) -> pd.DataFrame:
    """便捷函数：一次调用拉取单只历史分钟K线。"""
    return ZhituSource(token=token).fetch_minute_kline(
        symbol=symbol, level=level, start_date=start_date, end_date=end_date, adj=adj
    )

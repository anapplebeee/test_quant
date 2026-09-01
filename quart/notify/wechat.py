from __future__ import annotations

import os
import time

import httpx
from loguru import logger

from quart.config import load_config

# 重试：PushPlus 偶发限流/网络抖动时避免信号告警静默丢失
_MAX_RETRIES = 2
_RETRY_SLEEP_S = 1.0

_PUSHPLUS_URL = "https://www.pushplus.plus/send"


def send_markdown(title: str, text: str) -> bool:
    """推送 markdown 到个人微信（PushPlus 服务号）。

    配置优先级：环境变量 QUART_WECHAT_PUSHPLUS_TOKEN > settings.yaml。
    PushPlus 个人令牌从 https://www.pushplus.plus 获取。
    未配置时静默跳过（不阻断主流程）。
    """
    cfg = load_config()["notify"]
    token = os.environ.get("QUART_WECHAT_PUSHPLUS_TOKEN") or cfg.get("wechat_pushplus_token") or ""
    if not token:
        logger.info("微信(PushPlus) token 未配置，跳过推送")
        return False
    payload = {
        "token": token,
        "title": title,
        "content": text,
        "template": "markdown",
    }
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = httpx.post(_PUSHPLUS_URL, json=payload, timeout=10)
            try:
                data = resp.json()
            except Exception:
                data = {}
            # PushPlus 成功 code == 200
            ok = data.get("code") == 200
            if ok:
                return True
            logger.warning("微信(PushPlus) push failed (attempt {}): {}", attempt + 1, data)
        except Exception as exc:
            logger.warning("微信(PushPlus) push error (attempt {}): {}", attempt + 1, exc)
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_SLEEP_S)
    return False

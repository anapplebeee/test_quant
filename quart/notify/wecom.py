from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from urllib.parse import quote

import httpx
from loguru import logger

from quart.config import load_config

# 重试：企业微信偶发限流/网络抖动时避免信号告警静默丢失
_MAX_RETRIES = 2
_RETRY_SLEEP_S = 1.0


def _signed_url(webhook: str, secret: str) -> str:
    """企业微信群机器人「加签」模式：timestamp + secret 做 HMAC-SHA256。"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode(), string_to_sign.encode(), digestmod=hashlib.sha256).digest()
    sign = base64.b64encode(hmac_code).decode()
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={timestamp}&sign={quote(sign)}"


def send_markdown(title: str, text: str) -> bool:
    """推送 markdown 到企业微信群机器人。

    配置优先级：环境变量 QUART_WEBCOM_WEBHOOK / QUART_WEBCOM_SECRET > settings.yaml。
    未配置时静默跳过（不阻断主流程）。
    """
    cfg = load_config()["notify"]
    webhook = os.environ.get("QUART_WEBCOM_WEBHOOK") or cfg.get("wecom_webhook") or ""
    secret = os.environ.get("QUART_WEBCOM_SECRET") or cfg.get("wecom_secret") or ""
    if not webhook:
        logger.info("企业微信 webhook 未配置，跳过推送")
        return False
    url = _signed_url(webhook, secret) if secret else webhook
    # 企业微信 markdown 无独立 title 字段，把标题并入正文
    content = f"## {title}\n\n{text}"
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = httpx.post(url, json=payload, timeout=10)
            data = resp.json()
            ok = data.get("errcode") == 0
            if ok:
                return True
            logger.warning("企业微信 push failed (attempt {}): {}", attempt + 1, data)
        except Exception as exc:
            logger.warning("企业微信 push error (attempt {}): {}", attempt + 1, exc)
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_SLEEP_S)
    return False

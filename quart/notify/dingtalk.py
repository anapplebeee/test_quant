from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import quote

import httpx
from loguru import logger

from quart.config import load_config


def _signed_url(webhook: str, secret: str) -> str:
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode(), string_to_sign.encode(), digestmod=hashlib.sha256).digest()
    sign = base64.b64encode(hmac_code).decode()
    return f"{webhook}&timestamp={timestamp}&sign={quote(sign)}"


def send_markdown(title: str, text: str) -> bool:
    cfg = load_config()["notify"]
    webhook = cfg.get("dingtalk_webhook") or ""
    secret = cfg.get("dingtalk_secret") or ""
    if not webhook:
        logger.info("dingtalk webhook 未配置，跳过推送")
        return False
    url = _signed_url(webhook, secret) if secret else webhook
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        data = resp.json()
        ok = data.get("errcode") == 0
        if not ok:
            logger.warning("dingtalk push failed: {}", data)
        return ok
    except Exception as exc:
        logger.warning("dingtalk push error: {}", exc)
        return False

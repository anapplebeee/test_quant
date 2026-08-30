from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 设为 "1" 时跳过配置结构校验（仅在排查配置加载问题时应关闭）
_SKIP_VALIDATION_ENV = "QUART_SKIP_CONFIG_VALIDATION"


@lru_cache
def load_config(path: str | None = None, validate: bool = True) -> dict:
    """加载配置。

    validate=True 时做结构校验，把"少一个键/类型错"提前到启动瞬间暴露，
    而不是等回测跑一小时后在某一行炸 KeyError。
    """
    import os

    config_path = Path(path) if path else PROJECT_ROOT / "config" / "settings.yaml"
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    if validate and os.environ.get(_SKIP_VALIDATION_ENV) != "1":
        from quart.config_schema import ensure_valid

        ensure_valid(cfg)
    return cfg


def reload_config() -> None:
    """清除配置缓存。修改了 settings.yaml 后需要调用。"""
    load_config.cache_clear()
    data_root.cache_clear()


@lru_cache
def data_root() -> Path:
    cfg = load_config()["data"]["root"]
    path = Path(cfg)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


#: 与 BarStore 同义的数据根，供 api/frontend 使用，避免各处硬编码 "data"
def data_dir() -> Path:
    return data_root()


def reports_dir() -> Path:
    return PROJECT_ROOT / "reports"

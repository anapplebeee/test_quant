from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@lru_cache
def load_config(path: str | None = None) -> dict:
    config_path = Path(path) if path else PROJECT_ROOT / "config" / "settings.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache
def data_root() -> Path:
    cfg = load_config()["data"]["root"]
    path = Path(cfg)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path

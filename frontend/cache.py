"""简易 TTL 缓存"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Callable


_cache: dict[str, tuple[Any, datetime]] = {}
DEFAULT_TTL = 300  # 5分钟


def ttl_cache(ttl: int = DEFAULT_TTL):
    """TTL 缓存装饰器"""
    def decorator(fn: Callable):
        key = f"{fn.__module__}.{fn.__name__}"

        @wraps(fn)
        def wrapper(*args, **kwargs):
            cache_key = f"{key}:{args}:{sorted(kwargs.items())}"
            if cache_key in _cache:
                result, ts = _cache[cache_key]
                if datetime.now() - ts < timedelta(seconds=ttl):
                    return result
            result = fn(*args, **kwargs)
            _cache[cache_key] = (result, datetime.now())
            return result

        wrapper.invalidate = lambda: _cache.pop(cache_key, None)
        return wrapper
    return decorator


def clear_cache():
    """清空所有缓存"""
    _cache.clear()

"""数据版本总线 - 前端跨页联动刷新的最小实现。

问题背景（2026-08-31）：
- 各页面数据在 render() 时静态加载，操作中心的任务（数据刷新/信号/回测等）
  执行完后，其他页面的 DataFrame/摘要仍停留在启动时的旧数据。
- 逐页各自加固定间隔 Timer 要么太浪费（每 5s 全量重扫文件），要么太迟钝。

方案：
- 模块级版本号 `_version`：任务队列每成功完成一个任务，`bump()` 一次。
- 每个页面挂一个 5s 的 gr.Timer，tick 处理器先 `poll(seen)` 判断版本是否变化：
  - 未变化 → 返回 gr.skip()（不产生任何组件更新流量）
  - 变化 → 重算页面数据并返回新值，同时把 seen 更新为当前版本

线程安全：bump() 会被任务队列的后台线程调用，用锁保护。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_version = 0


def bump(reason: str = "") -> int:
    """数据已变化，版本号 +1。任何线程可调用。"""
    global _version
    with _lock:
        _version += 1
        return _version


def current() -> int:
    with _lock:
        return _version


def poll(seen: int) -> tuple[bool, int]:
    """判断自 seen 之后数据是否有变化。

    Returns:
        (是否有变化, 当前版本号)  调用方应把返回的版本号存回 gr.State。
    """
    cur = current()
    return cur != seen, cur

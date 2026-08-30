"""研究层：基线参照、随机信号定标等研究专用工具。

与 `quart.strategy` 的区别：strategy 里的策略是**可上线**的候选，
research 里的策略是**度量标尺**（随机基线等），不用于实盘。
"""
from __future__ import annotations

from quart.research.baseline import RandomTopKStrategy, k_day_rebal

__all__ = ["RandomTopKStrategy", "k_day_rebal"]

"""复权口径钉住（hfq pins）：被 qfq 伪影污染后改用 hfq 重拉的股票清单。

背景：qfq 前复权在极端分红/股本变动下会把历史价格压成负数或"仙股价"
（如 601088 中国神华 2020 年 qfq close≈0.2 元，600809 山西汾酒 2019 年 close≈1 元）。
这些股票经 scripts/repair_qfq_negative.py 用 hfq 重拉后，若增量更新器再次用
qfq 全量刷新（drift 检测必然触发，因为 hfq/qfq 价位不同），损坏会复发。
因此修复脚本把股票代码写入本文件，updater 对钉住股票始终用 hfq 拉取。
"""
from __future__ import annotations

from pathlib import Path

from quart.config import data_root

PIN_FILE = "hfq_pins.txt"


def pins_path(root: str | Path | None = None) -> Path:
    return (Path(root) if root else data_root()) / PIN_FILE


def read_hfq_pins(root: str | Path | None = None) -> set[str]:
    path = pins_path(root)
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def add_hfq_pins(symbols: list[str], root: str | Path | None = None) -> None:
    path = pins_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_hfq_pins(root)
    merged = sorted(existing | set(symbols))
    header = "# hfq-pinned symbols (repair_qfq_negative.py) - updater must fetch these with adjust=hfq\n"
    path.write_text(header + "\n".join(merged) + "\n", encoding="utf-8")

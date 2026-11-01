"""终端输出小工具：中英文混排也能对齐。"""

from __future__ import annotations

import unicodedata

BAR = "=" * 68


def display_width(text: str) -> int:
    """中文/全角字符占 2 列，用它来算真实占位宽度。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def ljust(text: str, width: int) -> str:
    return text + " " * max(1, width - display_width(text))


def section(text: str) -> None:
    print(f"\n{BAR}\n {text}\n{BAR}")


def line(label: str, value: object, indent: int = 4, width: int = 40) -> None:
    print(f"{' ' * indent}{ljust(label, width)}{value}")

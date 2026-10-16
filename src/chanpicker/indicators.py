"""技术指标基元——独立成模块，避免 core 与 masystem 互相 import 形成环。"""

from __future__ import annotations

from typing import Sequence


def ema(values: Sequence[float], period: int) -> list[float]:
    """指数移动平均。period <= 0 视为参数错误，空序列返回空。"""

    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result

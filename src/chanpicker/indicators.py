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


def sma(values: Sequence[float], period: int) -> list[float]:
    """简单移动平均。前 period-1 个位置用已见数据的均值（与 pandas 语义不同，
    只用于特征提取，足够）。返回与输入等长的列表。"""

    if period <= 0:
        raise ValueError("period must be positive")
    vals = list(values)
    if not vals:
        return []
    result: list[float] = []
    running = 0.0
    for i, v in enumerate(vals):
        running += float(v)
        if i >= period:
            running -= float(vals[i - period])
        result.append(running / min(i + 1, period))
    return result


def rsi(values: Sequence[float], period: int = 14) -> list[float]:
    """相对强弱指标（Wilder 平滑）。前 period 根用简单平均，之后用平滑递推。"""

    vals = list(values)
    if len(vals) <= period:
        return [50.0] * len(vals)  # 数据不足给中性值，特征层再决定是否采用
    result = [50.0] * period
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = vals[i] - vals[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    result.append(_rsi_from(avg_gain, avg_loss))
    for i in range(period + 1, len(vals)):
        delta = vals[i] - vals[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        result.append(_rsi_from(avg_gain, avg_loss))
    return result


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def kdj(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
    period: int = 9,
) -> list[dict]:
    """KDJ 指标（K/D/J 三条线）。返回与输入等长的 [{'k','d','j'}]。

    初始 K/D 取 50。RSV 用 period 日最高/最低。
    """

    n = len(closes)
    if n == 0:
        return []
    k, d = 50.0, 50.0
    result: list[dict] = []
    for i in range(n):
        lo = min(lows[max(0, i - period + 1):i + 1])
        hi = max(highs[max(0, i - period + 1):i + 1])
        rsv = (closes[i] - lo) / (hi - lo) * 100.0 if hi > lo else 50.0
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        j = 3 * k - 2 * d
        result.append({"k": k, "d": d, "j": j})
    return result


def returns_std(values: Sequence[float], period: int = 20) -> list[float]:
    """滚动收益率标准差（波动率/股性活跃度）。前 period 根返回 None。"""

    vals = list(values)
    n = len(vals)
    result: list[float | None] = [None] * n
    if n <= period:
        return result
    rets = [vals[i] / vals[i - 1] - 1.0 for i in range(1, n)]
    for i in range(period, n):
        window = rets[i - period:i]
        mean = sum(window) / period
        var = sum((r - mean) ** 2 for r in window) / period
        result[i] = var ** 0.5
    return result


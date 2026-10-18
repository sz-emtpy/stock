"""技术指标基元（indicators.py）的回归：RSI / KDJ / SMA / 波动率。

这些是自成长体系新特征的地基，必须单独锁死数值正确性——
增益分析再花哨，指标算错了一切都是空中楼阁。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.indicators import kdj, returns_std, rsi, sma  # noqa: E402


class SmaTest(unittest.TestCase):
    def test_basic_mean(self) -> None:
        result = sma([1, 2, 3, 4, 5], 3)
        # 前 3 个是部分均值：1, 1.5, 2；之后是满窗口均值
        self.assertAlmostEqual(result[0], 1.0)
        self.assertAlmostEqual(result[1], 1.5)
        self.assertAlmostEqual(result[2], 2.0)
        self.assertAlmostEqual(result[3], 3.0)   # (2+3+4)/3
        self.assertAlmostEqual(result[4], 4.0)   # (3+4+5)/3

    def test_empty(self) -> None:
        self.assertEqual(sma([], 5), [])


class RsiTest(unittest.TestCase):
    def test_all_up_is_100(self) -> None:
        # 一直上涨 → 无亏损 → RSI 100
        result = rsi([float(i) for i in range(1, 40)], 14)
        self.assertAlmostEqual(result[-1], 100.0, places=2)

    def test_all_down_is_zero(self) -> None:
        result = rsi([float(i) for i in range(40, 0, -1)], 14)
        self.assertAlmostEqual(result[-1], 0.0, places=2)

    def test_insufficient_data_is_neutral(self) -> None:
        result = rsi([1.0, 2.0, 3.0], 14)
        self.assertEqual(result, [50.0, 50.0, 50.0])


class KdjTest(unittest.TestCase):
    def test_high_at_top_gives_high_k(self) -> None:
        # 连续多根收盘贴近最高 → RSV 持续 100 → K 被拉到高位
        closes = [20.0] * 10
        highs = [20.0] * 10
        lows = [10.0] * 10
        result = kdj(highs, lows, closes, 9)
        self.assertGreater(result[-1]["k"], 50.0)

    def test_low_at_bottom_gives_low_k(self) -> None:
        closes = [9.0] * 10
        highs = [20.0] * 10
        lows = [9.0] * 10
        result = kdj(highs, lows, closes, 9)
        self.assertLess(result[-1]["k"], 50.0)

    def test_j_relationship(self) -> None:
        result = kdj([10.0] * 20, [10.0] * 20, [10.0] * 20, 9)
        last = result[-1]
        # J = 3K - 2D 恒成立
        self.assertAlmostEqual(last["j"], 3 * last["k"] - 2 * last["d"], places=6)


class VolatilityTest(unittest.TestCase):
    def test_flat_is_zero(self) -> None:
        result = returns_std([10.0] * 30, 20)
        self.assertIsNone(result[0])
        self.assertAlmostEqual(result[-1], 0.0, places=8)

    def test_volatile_is_positive(self) -> None:
        import math
        values = [10.0 + (5.0 if i % 2 == 0 else -5.0) for i in range(30)]
        result = returns_std(values, 20)
        self.assertGreater(result[-1], 0.0)

    def test_insufficient_returns_none(self) -> None:
        result = returns_std([10.0] * 10, 20)
        self.assertTrue(all(v is None for v in result))


if __name__ == "__main__":
    unittest.main()

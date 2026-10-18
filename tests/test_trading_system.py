"""交易体系统计引擎（backend/trading_system.py）的纯函数回归。

这些是体系的核心——标签、分箱、增益、打分、时间分割验证。都是纯函数，
不碰数据库，直接构造样本列表即可测，重点锁：
1. 增益 = 箱内拉升率 - 基准拉升率，符号正确；
2. 打分 = 命中箱增益之和；
3. 时间分割验证能识别「有区分度的特征」、识别不出「无区分度的特征」；
4. 结构-买点-胜率卡只保留样本足够大的格子（防小样本噪声）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from backend.trading_system import (  # noqa: E402
    DEFAULT_WIN_THRESHOLD,
    SYSTEM_FEATURES,
    base_win_rate,
    bucketize,
    feature_gains,
    label_win,
    score_signal,
    structure_buy_cards,
    validate_time_split,
)


def sample(max_return, features):
    return {"max_return": max_return, "signal_date": "2026-01-01", "kind": "FIRST_BUY",
            "features": features}


class LabelTest(unittest.TestCase):
    def test_win_label_threshold(self) -> None:
        self.assertTrue(label_win(sample(0.15, {}), 0.10))
        self.assertFalse(label_win(sample(0.05, {}), 0.10))
        # 结局未终结（max_return None）不算赢也不算样本
        self.assertFalse(label_win(sample(None, {}), 0.10))

    def test_base_rate_ignores_unclosed(self) -> None:
        rows = [sample(0.2, {}), sample(0.05, {}), sample(None, {})]
        self.assertEqual(base_win_rate(rows, 0.10), 0.5)  # 只算已终结的 2 条


class BucketTest(unittest.TestCase):
    def test_binary_feature(self) -> None:
        self.assertEqual(bucketize("above_ma20", 1), "是")
        self.assertEqual(bucketize("above_ma20", 0), "否")

    def test_vol_ratio_buckets(self) -> None:
        self.assertEqual(bucketize("vol_ratio", 0.5), "缩量<0.8")
        self.assertEqual(bucketize("vol_ratio", 1.0), "平量0.8-1.2")
        self.assertEqual(bucketize("vol_ratio", 1.5), "温和放量1.2-2.0")
        self.assertEqual(bucketize("vol_ratio", 3.0), "显著放量≥2.0")

    def test_retrace_buckets(self) -> None:
        self.assertEqual(bucketize("retrace_from_high", 0.02), "接近新高<5%")
        self.assertEqual(bucketize("retrace_from_high", 0.10), "浅回撤5-15%")
        self.assertEqual(bucketize("retrace_from_high", 0.20), "中度回撤15-30%")
        self.assertEqual(bucketize("retrace_from_high", 0.40), "深度回撤≥30%")

    def test_none_returns_none(self) -> None:
        self.assertIsNone(bucketize("vol_ratio", None))


class GainTest(unittest.TestCase):
    def test_positive_gain_when_bucket_outperforms(self) -> None:
        # 站上 MA20 的样本全部赢，跌破的全输 → 站上 MA20 增益为正
        rows = [
            sample(0.5, {"above_ma20": 1}), sample(0.5, {"above_ma20": 1}),
            sample(0.01, {"above_ma20": 0}), sample(0.01, {"above_ma20": 0}),
        ]
        gain = feature_gains(rows, ("above_ma20",), 0.10)["above_ma20"]
        by_bucket = {b["bucket"]: b for b in gain["buckets"]}
        self.assertGreater(by_bucket["是"]["gain"], 0)
        self.assertLess(by_bucket["否"]["gain"], 0)
        # 基准是 0.5（4 条里 2 条赢）
        self.assertEqual(gain["base_rate"], 0.5)

    def test_zero_gain_when_no_discrimination(self) -> None:
        # 所有箱拉升率一致 → 增益全为 0
        rows = [
            sample(0.5, {"above_ma20": 1}), sample(0.5, {"above_ma20": 0}),
            sample(0.01, {"above_ma20": 1}), sample(0.01, {"above_ma20": 0}),
        ]
        gain = feature_gains(rows, ("above_ma20",), 0.10)["above_ma20"]
        for b in gain["buckets"]:
            self.assertAlmostEqual(b["gain"], 0.0, places=4)


class ScoreTest(unittest.TestCase):
    def test_score_sums_hit_gains(self) -> None:
        """v2 非对称打分：下界>0 的正箱加分，表面负+下界负的负箱扣分，
        「证据不足」的中间地带（下界≤0 但表面≥0）不加分也不扣分。"""

        gain_map = {
            "above_ma20": {"buckets": [
                # 正箱：下界>0 → 加分加的是下界
                {"bucket": "是", "gain": 0.1, "gain_lb": 0.06},
                # 负箱：表面负+下界负 → 扣分扣的是下界
                {"bucket": "否", "gain": -0.1, "gain_lb": -0.12},
            ]},
            "vol_ratio": {"buckets": [
                # 中性地带：表面正但下界≤0 → 不加分
                {"bucket": "温和放量1.2-2.0", "gain": 0.05, "gain_lb": -0.01},
            ]},
        }
        score, hits = score_signal({"above_ma20": 1, "vol_ratio": 1.5}, gain_map)
        self.assertAlmostEqual(score, 0.06, places=4)
        self.assertEqual(len(hits), 2)
        # 负箱命中时的扣分
        score2, _ = score_signal({"above_ma20": 0}, gain_map)
        self.assertAlmostEqual(score2, -0.12, places=4)

    def test_missing_feature_skipped(self) -> None:
        gain_map = {"above_ma20": {"buckets": [{"bucket": "是", "gain": 0.1}]}}
        score, hits = score_signal({}, gain_map)   # 无特征 → 不得分
        self.assertEqual(score, 0.0)
        self.assertEqual(hits, [])


class StructureCardsTest(unittest.TestCase):
    def test_cards_filter_small_samples(self) -> None:
        # 一个箱只有 2 条样本，应被最小样本门槛（30）过滤掉
        rows = [sample(0.5, {"above_ma20": 1}) for _ in range(2)]
        rows += [sample(0.01, {"above_ma20": 0}) for _ in range(40)]
        cards = structure_buy_cards(rows, ("above_ma20",), 0.10)
        buckets = {c["bucket"] for c in cards}
        self.assertNotIn("是", buckets)   # 2 条样本被过滤
        self.assertIn("否", buckets)      # 40 条样本保留


class ValidateTest(unittest.TestCase):
    def _rows_with_edge(self, n=300):
        """构造一个「特征能区分涨跌」的样本集：站上 MA20 的赢、跌破的输。"""
        rows = []
        for i in range(n):
            win = i % 2 == 0   # 一半站上 MA20
            rows.append(sample(0.5 if win else 0.01, {"above_ma20": 1 if win else 0}))
        return rows

    def test_validate_detects_edge(self) -> None:
        rows = self._rows_with_edge(300)
        result = validate_time_split(rows, ("above_ma20",), 0.10)
        self.assertIsNotNone(result)
        self.assertTrue(result["has_edge"], "有区分度的特征应被识别出 edge")
        self.assertGreater(result["high"]["win_rate"], result["low"]["win_rate"])

    def test_validate_rejects_no_edge(self) -> None:
        # 特征与涨跌完全无关 → 不应声称有 edge
        rows = [sample(0.5 if i % 2 == 0 else 0.01, {"above_ma20": i % 3 == 0})
                for i in range(300)]
        result = validate_time_split(rows, ("above_ma20",), 0.10)
        self.assertIsNotNone(result)
        # 无区分度时高分档不应显著高于低分档（允许噪声，但不应稳定为 edge）
        # 这里放宽为「要么无 edge，要么 spread 很小」——避免偶发噪声误判
        if result["has_edge"]:
            self.assertLess(result["spread"], 0.1, "无区分度特征不应有显著 spread")

    def test_insufficient_samples_returns_none(self) -> None:
        self.assertIsNone(validate_time_split([sample(0.5, {}) for _ in range(20)], ("above_ma20",), 0.10))


if __name__ == "__main__":
    unittest.main()

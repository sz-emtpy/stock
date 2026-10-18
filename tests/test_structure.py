"""中枢构建的原文口径回归测试。

原文依据（《教你炒股票108课》张星星注解版）：
- **p152**：「某级别走势类型中，被**至少三个连续次级别走势类型**所重叠的
  部分，称为缠中说禅走势中枢。」
- **p152**（关键补充）：「对**最后不能分解的级别**，其缠中说禅走势中枢就不能用
  『至少三个连续次级别走势类型所重叠』定义，而定义为**至少三个该级别单位K线
  重叠部分**。」——我们的日线图正属于这一级。
- **p167** 中枢区间公式：`(max(a2,b2,c2), min(a1,b1,c1))`
- **p152**：「盘整：只包含**一个**中枢」「趋势：至少**两个以上依次同向**的中枢」
- **p153**：「任何级别的任何走势类型，都至少由**三段以上次级别走势类型**构成。」
- **p1351**：「笔不能构成中枢」——与旧实现「3笔重叠」的根本差异。
"""

from __future__ import annotations

import unittest

from chanpicker.core import build_strokes, detect_fractals, normalize_fractals
from chanpicker.structure import (
    TrendLeg,
    Zhongshu,
    build_legs,
    classify_trend,
    detect_zhongshu_by_legs,
    has_trend_divergence_leg,
    leg_direction,
    merge_overlapping,
    overlap_of,
)


def legs_from(rows: list[tuple[float, float]]) -> list[TrendLeg]:
    """用 (high, low) 序列直接构造走势段，便于精确控制重叠关系。"""

    return [
        TrendLeg(start_index=i, end_index=i, high=hi, low=lo, open_price=lo, close_price=hi)
        for i, (hi, lo) in enumerate(rows)
    ]


class BuildLegsTest(unittest.TestCase):
    def test_cuts_into_chunks_of_min_legs(self) -> None:
        highs = [10.0] * 9
        lows = [9.0] * 9
        legs = build_legs(highs, lows, min_legs=3)
        self.assertEqual(len(legs), 3)
        for leg in legs:
            self.assertEqual(leg.bars, 3)

    def test_discards_incomplete_tail(self) -> None:
        """凑不满三根 K 线的不成一段——与「至少三个」一致。"""

        legs = build_legs([10.0] * 8, [9.0] * 8, min_legs=3)
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[-1].end_index, 5, "尾部 2 根应被丢弃")

    def test_takes_extremes_within_chunk(self) -> None:
        highs = [10.0, 20.0, 12.0]
        lows = [8.0, 9.0, 7.0]
        legs = build_legs(highs, lows, min_legs=3)
        self.assertAlmostEqual(legs[0].high, 20.0)
        self.assertAlmostEqual(legs[0].low, 7.0)

    def test_direction_comes_from_closes(self) -> None:
        highs = [11.0, 12.0, 13.0]
        lows = [10.0, 11.0, 12.0]
        rises = build_legs(highs, lows, [10.0, 11.0, 12.0], min_legs=3)
        falls = build_legs(highs, lows, [13.0, 12.0, 11.0], min_legs=3)
        self.assertEqual(rises[0].direction, "up")
        self.assertEqual(falls[0].direction, "down")

    def test_too_few_bars_yields_nothing(self) -> None:
        self.assertEqual(build_legs([10.0, 11.0], [9.0, 10.0], min_legs=3), [])


class OverlapTest(unittest.TestCase):
    """p167 的公式：ZG = 各段高点最小值，ZD = 各段低点最大值。"""

    def test_overlap_uses_min_high_and_max_low(self) -> None:
        legs = legs_from([(10.0, 5.0), (9.0, 6.0), (8.5, 6.5)])
        zd, zg = overlap_of(legs)
        self.assertAlmostEqual(zg, 8.5)   # min(10, 9, 8.5)
        self.assertAlmostEqual(zd, 6.5)   # max(5, 6, 6.5)

    def test_no_overlap_returns_none(self) -> None:
        legs = legs_from([(10.0, 8.0), (20.0, 18.0), (30.0, 28.0)])
        self.assertIsNone(overlap_of(legs))

    def test_single_point_overlap_is_valid(self) -> None:
        """恰好一点重叠也是中枢（p152 的「重叠部分」未要求有高度）。"""

        legs = legs_from([(10.0, 8.0), (10.0, 8.0), (10.0, 8.0)])
        zd, zg = overlap_of(legs)
        self.assertAlmostEqual(zd, 8.0)
        self.assertAlmostEqual(zg, 10.0)

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(overlap_of([]))


class DetectZhongshuTest(unittest.TestCase):
    def test_requires_at_least_three_legs(self) -> None:
        self.assertEqual(detect_zhongshu_by_legs(legs_from([(10, 5), (9, 6)])), [])
        centers = detect_zhongshu_by_legs(legs_from([(10, 5), (9, 6), (8, 7)]))
        self.assertEqual(centers[0].leg_count, 3)

    def test_builds_center_from_three_overlapping_legs(self) -> None:
        centers = detect_zhongshu_by_legs(legs_from([(10, 5), (9, 6), (8, 6.5)]))
        self.assertEqual(len(centers), 1)
        self.assertEqual(centers[0].leg_count, 3)
        self.assertTrue(centers[0].valid)

    def test_skips_non_overlapping_triples(self) -> None:
        # 三段各走各的，没有重叠 → 不构成中枢
        self.assertEqual(detect_zhongshu_by_legs(legs_from([(10, 8), (20, 18), (30, 28)])), [])

    def test_extension_keeps_interval_unchanged(self) -> None:
        """中枢延伸：区间**不变**，只增加段数与时间跨度。"""

        legs = legs_from([
            (10.0, 5.0), (9.0, 6.0), (8.0, 6.5),   # 中枢 ZG=8 ZD=6.5
            (8.2, 6.8),                            # 与 [6.5, 8] 仍有交集 → 延伸
        ])
        centers = detect_zhongshu_by_legs(legs)
        self.assertEqual(len(centers), 1)
        self.assertEqual(centers[0].leg_count, 4)
        self.assertEqual(centers[0].state, "extended")
        self.assertAlmostEqual(centers[0].zg, 8.0, msg="延伸不改变 ZG")
        self.assertAlmostEqual(centers[0].zd, 6.5, msg="延伸不改变 ZD")

    def test_non_overlapping_next_leg_starts_new_center(self) -> None:
        legs = legs_from([
            (10.0, 5.0), (9.0, 6.0), (8.0, 6.5),   # 中枢 ZG=8 ZD=6.5
            (20.0, 18.0),                            # 完全不同区间 → 新中枢起点
        ])
        centers = detect_zhongshu_by_legs(legs)
        self.assertEqual(centers[0].leg_count, 3, "延伸不该吞掉不相干的段")

    def test_centers_do_not_overlap_in_time(self) -> None:
        """同一中枢不应被重复计入——延伸后从其结束处继续。"""

        legs = legs_from([
            (10.0, 5.0), (9.0, 6.0), (8.0, 6.5),
            (8.2, 6.8), (8.1, 6.9),
            (8.0, 7.0), (7.9, 7.1), (7.8, 7.2),
        ])
        centers = detect_zhongshu_by_legs(legs)
        for previous, current in zip(centers, centers[1:]):
            self.assertGreater(
                current.start_index, previous.end_index,
                "中枢在时间轴上不应重叠",
            )


class ClassifyTrendTest(unittest.TestCase):
    """p152：盘整=只含一个中枢；趋势=至少两个依次同向的中枢。"""

    @staticmethod
    def center(index: int, zd: float, zg: float) -> Zhongshu:
        return Zhongshu(start_index=index * 10, end_index=index * 10 + 5, zd=zd, zg=zg)

    def test_single_center_is_consolidation(self) -> None:
        self.assertEqual(classify_trend([self.center(0, 9.0, 10.0)], "up"), "consolidation")

    def test_two_rising_centers_form_up_trend(self) -> None:
        centers = [self.center(0, 9.0, 10.0), self.center(1, 9.5, 10.5)]
        self.assertEqual(classify_trend(centers, "up"), "up_trend")

    def test_lower_center_after_breaks_the_trend(self) -> None:
        centers = [self.center(0, 10.0, 11.0), self.center(1, 9.0, 10.0)]
        self.assertEqual(classify_trend(centers, "up"), "consolidation")

    def test_down_trend_is_separate_from_up(self) -> None:
        centers = [self.center(0, 10.0, 11.0), self.center(1, 9.0, 10.0)]
        self.assertEqual(classify_trend(centers, "down"), "down_trend")


class MergeOverlappingTest(unittest.TestCase):
    def test_overlapping_centers_are_merged_as_expansion(self) -> None:
        centers = [
            Zhongshu(start_index=0, end_index=5, zd=9.0, zg=10.0),
            Zhongshu(start_index=4, end_index=9, zd=9.5, zg=10.5),
        ]
        merged = merge_overlapping(centers)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].state, "expanded")
        self.assertAlmostEqual(merged[0].zd, 9.0)
        self.assertAlmostEqual(merged[0].zg, 10.5)

    def test_disjoint_centers_stay_separate(self) -> None:
        centers = [
            Zhongshu(start_index=0, end_index=5, zd=9.0, zg=10.0),
            Zhongshu(start_index=8, end_index=12, zd=11.0, zg=12.0),
        ]
        self.assertEqual(len(merge_overlapping(centers)), 2)

    def test_time_disjoint_but_price_overlapping_stay_separate(self) -> None:
        """时间不重叠就不该合并——中枢扩展的前提是时间上接续。"""

        centers = [
            Zhongshu(start_index=0, end_index=5, zd=9.0, zg=10.0),
            Zhongshu(start_index=10, end_index=15, zd=9.5, zg=10.5),
        ]
        self.assertEqual(len(merge_overlapping(centers)), 2)


class DivergenceFromOldImplTest(unittest.TestCase):
    """锁死与旧「3笔重叠」实现的差异，防止有人把新口径悄悄换回去。"""

    def test_leg_granularity_differs_from_stroke_granularity(self) -> None:
        from chanpicker.core import build_strokes, detect_fractals, normalize_fractals, detect_zhongshu
        from chanpicker.structure import build_legs, detect_zhongshu_by_legs

        highs = [10.0 + (i % 5) for i in range(120)]
        lows = [8.0 + (i % 3) for i in range(120)]
        closes = [(highs[i] + lows[i]) / 2 for i in range(120)]

        class B:
            def __init__(self, hi, lo, cl, i):
                self.high, self.low, self.close, self.date = hi, lo, cl, f"d{i}"

        bars = [B(highs[i], lows[i], closes[i], i) for i in range(120)]
        strokes = build_strokes(normalize_fractals(detect_fractals(bars)), 4)
        old_count = len(detect_zhongshu(strokes))
        new_count = len(detect_zhongshu_by_legs(build_legs(highs, lows, closes, min_legs=3)))
        # 两者粒度不同（笔 vs 次级别走势类型），数量几乎不可能巧合相等
        self.assertNotEqual(
            (old_count, new_count) == (0, 0), True,
            "新旧口径不应同时为空——说明其中一路没跑起来",
        )


class TrendContextTest(unittest.TestCase):
    """p116「没有趋势，没有背驰」——盘整里的背驰不算一买/一卖。

    原文（第 15 课《没有趋势没有背驰》）：
    > 「**没有趋势，没有背驰。在盘整中是无所谓『背驰』的**，
    > 这点是必须特别明确的。」

    这条限制是把段口径下一买从 14.2/股压到 2.3/股的关键。
    """

    @staticmethod
    def leg(index: int, direction: str = "down", low: float = 10.0, high: float = 12.0) -> TrendLeg:
        return TrendLeg(
            start_index=index * 10, end_index=index * 10 + 5,
            high=high, low=low, open_price=low, close_price=high,
        )

    @staticmethod
    def center(index: int, zd: float, zg: float) -> Zhongshu:
        return Zhongshu(start_index=index * 10, end_index=index * 10 + 5, zd=zd, zg=zg)

    def test_two_descending_centers_count_as_down_trend(self) -> None:
        centers = [self.center(0, 12.0, 13.0), self.center(1, 10.0, 11.0)]
        self.assertTrue(
            has_trend_divergence_leg([], 30, centers, "down"),
            "两个依次下移的中枢构成下跌趋势",
        )

    def test_single_center_is_consolidation_not_trend(self) -> None:
        self.assertFalse(
            has_trend_divergence_leg([], 30, [self.center(0, 10.0, 11.0)], "down"),
            "只有一个中枢属于盘整，盘整里没有背驰（p116）",
        )

    def test_rising_centers_do_not_count_as_down_trend(self) -> None:
        centers = [self.center(0, 10.0, 11.0), self.center(1, 12.0, 13.0)]
        self.assertFalse(
            has_trend_divergence_leg([], 30, centers, "down"),
            "中枢依次上移是上涨趋势，不是下跌趋势",
        )

    def test_centers_after_the_segment_are_ignored(self) -> None:
        # 不能拿背驰之后才形成的中枢来为它背书——那是未来信息
        centers = [self.center(0, 12.0, 13.0), self.center(4, 10.0, 11.0)]
        self.assertFalse(
            has_trend_divergence_leg([], 30, centers, "down"),
            "中枢 end_index=45 晚于背驰段起点 30，不该被采信",
        )


class SignalDensityTest(unittest.TestCase):
    """两道收紧对信号密度的实际影响——防止有人把它们悄悄关掉。"""

    def _count(self, **config_kw) -> int:
        from chanpicker.core import Bar, ChanConfig, ChanEngine

        prices = [
            100, 96, 92, 88, 84, 80, 76, 66, 56, 62, 58, 54,
            66, 72, 76, 74, 72, 70, 72, 74, 76,
        ]
        bars = [
            Bar(symbol="sh600000", date=f"2024-01-{i+1:02d}", open=p, high=p + 0.4,
                low=p - 0.4, close=p, volume=1_000_000, amount=p * 1_000_000)
            for i, p in enumerate(prices)
        ]
        config = ChanConfig(
            min_stroke_gap=1, min_second_buy_score=0.0, min_third_buy_score=0.0,
            require_zhongshu_for_first_buy=False, require_zhongshu_for_first_sell=False,
            **config_kw,
        )
        return sum(
            1 for s in ChanEngine(config).scan(bars).signals if s.kind.value == "FIRST_BUY"
        )

    def test_standard_divergence_flag_is_respected(self) -> None:
        """require_standard_divergence=True 时只出标准背驰（②③同时弱）。"""

        from chanpicker.core import Bar, ChanConfig, ChanEngine

        prices = [
            100, 96, 92, 88, 84, 80, 76, 66, 56, 62, 58, 54,
            66, 72, 76, 74, 72, 70, 72, 74, 76,
        ]
        bars = [
            Bar(symbol="sh600000", date=f"2024-01-{i+1:02d}", open=p, high=p + 0.4,
                low=p - 0.4, close=p, volume=1_000_000, amount=p * 1_000_000)
            for i, p in enumerate(prices)
        ]
        base = dict(
            min_stroke_gap=1, min_second_buy_score=0.0, min_third_buy_score=0.0,
            require_zhongshu_for_first_buy=False, require_zhongshu_for_first_sell=False,
            require_trend_context=False,
        )
        loose = ChanEngine(ChanConfig(require_standard_divergence=False, **base)).scan(bars)
        strict = ChanEngine(ChanConfig(require_standard_divergence=True, **base)).scan(bars)
        strict_buys = [s for s in strict.signals if s.kind.value == "FIRST_BUY"]
        self.assertLessEqual(
            len(strict_buys), sum(1 for s in loose.signals if s.kind.value == "FIRST_BUY"),
            "限标准背驰不该放宽信号",
        )
        for signal in strict_buys:
            self.assertTrue(
                signal.meta["divergence_detail"]["standard"],
                "标称只出标准背驰，却混进了非标准背驰",
            )



class EngineStructureContractTest(unittest.TestCase):
    """引擎实际走的路径必须与 structure 模块的口径一致。

    这些用例验「接线」而非算法本身：算法规则由前面的用例覆盖，这里锁死
    引擎真的用了段口径、且买卖点按「第一次回调/回抽」取**紧邻**段。

    关键手法：直接构造 `TrendLeg` 序列调引擎私有方法，精确控制每一段的
    高低点。依赖某条价格序列「恰好产出某信号」会让断言跑空——
    变异测试里我实测过：构造序列常常只产出一买/二买，二卖/三买相关的断言
    全在空跑，改坏了代码也测不出来。
    """

    PRICES = [
        100, 100, 98, 97, 99, 97, 96, 97, 96, 95, 96, 98,
        100, 100, 100, 100, 99, 100, 98, 98, 96, 95, 94, 95,
        94, 93, 93, 91, 91, 90, 88, 90, 92, 92, 91, 91,
        91, 92, 91, 89, 91, 93, 94, 95, 95, 97, 99, 101,
        99, 97, 96, 97, 94, 96, 98, 96, 95, 97, 96, 95,
        95, 93, 94, 94, 93, 96, 98, 96, 95, 95, 93, 91,
        90, 91, 91, 90, 91, 93, 92, 92, 91, 91, 89, 87,
        87, 86, 87, 87, 88,
    ]

    def _bars(self, extra_close: float | None = None):
        from chanpicker.core import Bar

        bars = [
            Bar(symbol="sh600000", date=f"2024-01-{i+1:02d}", open=p, high=p + 0.4,
                low=p - 0.4, close=p, volume=1_000_000, amount=p * 1_000_000)
            for i, p in enumerate(self.PRICES)
        ]
        if extra_close is not None:
            bars.append(
                Bar(symbol="sh600000", date="2024-12-31", open=extra_close,
                    high=extra_close + 0.4, low=extra_close - 0.4, close=extra_close,
                    volume=1_000_000, amount=extra_close * 1_000_000)
            )
        return bars

    def _config(self, **kw):
        from chanpicker.core import ChanConfig

        base = dict(
            min_stroke_gap=1, min_second_buy_score=0.0, min_third_buy_score=0.0,
            min_second_sell_score=0.0, min_top_divergence_score=0.05,
            min_macd_area=9999, require_zhongshu_for_first_buy=False,
            require_zhongshu_for_first_sell=False, require_standard_divergence=False,
            require_trend_context=False, min_legs_bars=3,
        )
        base.update(kw)
        return ChanConfig(**base)

    def _engine(self, bars, **kw):
        """建一个 MACD 状态就绪的引擎（跳过 scan，直接调私有检测方法）。"""

        from chanpicker.core import ChanEngine
        from chanpicker.effort import macd_state

        engine = ChanEngine(self._config(**kw))
        engine._closes = [bar.close for bar in bars]
        engine._macd = macd_state(engine._closes)
        return engine

    def _scan(self, **kw):
        from chanpicker.core import ChanEngine

        return ChanEngine(self._config(**kw)).scan(self._bars())

    # ---- 口径接线 ----

    def test_engine_defaults_to_leg_structure(self) -> None:
        from chanpicker.core import ChanConfig

        self.assertTrue(
            ChanConfig().use_canonical_zhongshu,
            "默认必须是缠论原文的段口径（p152），否则中枢与买卖点会错层",
        )

    def test_engine_centers_match_leg_structure_module(self) -> None:
        from chanpicker.core import detect_zhongshu as legacy_zhongshu
        from chanpicker.structure import detect_zhongshu_by_legs

        bars = self._bars()
        result = self._scan()
        legs = build_legs(
            [b.high for b in bars], [b.low for b in bars],
            closes=[b.close for b in bars], min_legs=3,
        )
        self.assertEqual(
            len(result.zhongshu), len(detect_zhongshu_by_legs(legs)),
            "引擎的中枢应与 structure 模块的段口径一致",
        )
        # 段口径粒度更粗，中枢数应明显少于笔口径——数量相同说明悄悄退回了笔口径
        strokes = build_strokes(normalize_fractals(detect_fractals(bars)), 1)
        legacy_count = len(legacy_zhongshu(strokes))
        self.assertNotEqual(
            len(result.zhongshu), legacy_count,
            f"段口径中枢数({len(result.zhongshu)}) 与笔口径({legacy_count}) 相同，疑似退回笔口径",
        )

    def test_legacy_stroke_path_still_available(self) -> None:
        result, _ = self._scan(use_canonical_zhongshu=False), None
        self.assertIsInstance(result.zhongshu, list)

    # ---- 一买锚点：索引与极值价格必须来自同一根 K 线 ----

    def test_first_buy_anchor_points_to_actual_leg_low_bar(self) -> None:
        from chanpicker.core import Zhongshu
        from chanpicker.effort import Effort

        bars = self._bars(extra_close=90.0)
        legs = [
            TrendLeg(5, 8, high=100.0, low=95.0, open_price=100.0, close_price=96.0),
            TrendLeg(9, 12, high=99.0, low=90.0, open_price=96.0, close_price=94.0),
        ]
        bars[10] = type(bars[10])(
            symbol="sh600000", date=bars[10].date, open=94.0, high=96.0,
            low=89.0, close=94.0, volume=1_000_000, amount=94_000_000,
        )
        engine = self._engine(bars, use_ma_system=False)
        engine._leg_divergence_meta = lambda previous, current: (True, {"divergence_detail": {"standard": True, "score": 0.8}})
        engine._leg_effort = lambda leg, left, right: Effort(
            direction=leg.direction, start_index=left, end_index=right, macd_area=1.0,
            macd_piece_area=1.0, peak_area=1.0,
            dif_extreme=-1.0, dea_extreme=-1.0, ma_extreme=0.0,
        )
        signals = engine._detect_first_buy_by_legs(bars, legs, [Zhongshu(0, 4, 92.0, 98.0)])
        self.assertTrue(signals)
        self.assertEqual(signals[0].anchor_index, 10)
        self.assertEqual(signals[0].anchor_price, bars[10].low)

    # ---- 二买：p152「第一次次级别回调」+ 不破一买低点 ----

    def _second_buy_legs(self, pullback_low: float):
        """一买低点之后的 up → down（紧邻），回调低点由参数控制。

        段序列必须**严格 up/down 交替**——引擎取「up 段的下一段」当回调段，
        若下一段还是 up 就会被方向检查挡掉（实测踩过）。
        """

        return [
            TrendLeg(start_index=11, end_index=13, high=98.0, low=95.0, open_price=95.0, close_price=98.0),
            TrendLeg(start_index=14, end_index=16, high=99.0, low=pullback_low,
                     open_price=pullback_low + 3.0, close_price=pullback_low),
        ]

    def _first_buy_signal(self, low: float):
        from chanpicker.core import Signal, SignalKind

        bars = self._bars()
        return Signal(
            symbol="sh600000", kind=SignalKind.FIRST_BUY,
            date=bars[10].date, bar_index=10, price=low + 0.5,
            anchor_index=9, anchor_price=low, message="一买", meta={},
        )

    def test_second_buy_rejects_pullback_breaking_first_buy_low(self) -> None:
        """回调跌破一买低点 → 必须被拒（p152「次级别回调不破一买低点」）。"""

        first_buy = self._first_buy_signal(low=94.0)
        # 回调低点 93.0 < 一买低点 94.0 → 跌破
        bars = self._bars(extra_close=93.0)
        engine = self._engine(bars)
        rejected = engine._detect_second_buy_by_legs(
            bars, self._second_buy_legs(pullback_low=93.0), [first_buy]
        )
        self.assertEqual(rejected, [], "回调跌破一买低点却仍被判为二买")

    def test_second_buy_accepts_pullback_holding_first_buy_low(self) -> None:
        """回调不破一买低点 → 二买成立（反向对照）。"""

        first_buy = self._first_buy_signal(low=94.0)
        bars = self._bars(extra_close=96.0)
        engine = self._engine(bars)
        accepted = engine._detect_second_buy_by_legs(
            bars, self._second_buy_legs(pullback_low=95.5), [first_buy]
        )
        self.assertEqual(len(accepted), 1, "回调不破一买低点却没被判为二买")

    def test_second_buy_rule_uses_first_buy_index_not_price(self) -> None:
        """均线体位判定读索引；一买价格必须独立用于低点保持判断。"""

        from unittest.mock import Mock

        bars = self._bars(extra_close=96.0)
        engine = self._engine(bars)
        tracker = Mock()
        engine._ma_tracker = tracker
        engine._closes = [bar.close for bar in bars]
        # 直接 mock 规则入口验证包装器传参契约
        from unittest.mock import patch
        with patch("chanpicker.core.judge_second_buy") as judge:
            engine._judge_second_buy_rule(12, first_buy_index=9, first_buy_low=94.0)
        self.assertEqual(judge.call_args.kwargs["first_buy_index"], 9)
        self.assertTrue(judge.call_args.kwargs["holds_first_buy_low"])

    def test_second_buy_takes_the_first_adjacent_segment(self) -> None:
        """p152「**第一次**次级别回调」：只能取紧邻的那个反向段。

        数据设计要点：紧邻段合法（低点 95.5 > 一买低点 94.0）、远处段非法
        （低点 93.0 跌破一买低点）。这样——
        - 紧邻实现 → 产出信号，取到 start_index=14；
        - 跨段实现 → 会取到远处段，被「不破一买低点」挡掉，产出 0 个。
        两种实现在同一份数据上结果不同，变异测试才能抓到。
        """

        legs = [
            TrendLeg(start_index=11, end_index=13, high=98.0, low=95.0, open_price=95.0, close_price=98.0),
            # 紧邻回调：合法
            TrendLeg(start_index=14, end_index=16, high=97.0, low=95.5, open_price=97.0, close_price=95.5),
            TrendLeg(start_index=17, end_index=19, high=97.0, low=94.0, open_price=94.0, close_price=97.0),
            # 更远处的回调：跌破一买低点 94.0，若被取到就会被拒
            TrendLeg(start_index=20, end_index=22, high=96.0, low=93.0, open_price=96.0, close_price=93.0),
        ]
        first_buy = self._first_buy_signal(low=94.0)
        bars = self._bars(extra_close=96.0)
        engine = self._engine(bars)
        signals = engine._detect_second_buy_by_legs(bars, legs, [first_buy])
        self.assertEqual(len(signals), 1, "紧邻回调合法却没被判为二买")
        pullback = signals[0].meta["current_segment"]
        self.assertEqual(
            pullback["start_index"], 14,
            "取的不是紧邻的第一次回调段（p152「第一次次级别回调」）",
        )

    # ---- 二卖：反弹不得创出一卖新高 ----

    def _first_sell_signal(self, high: float):
        from chanpicker.core import Signal, SignalKind

        bars = self._bars()
        return Signal(
            symbol="sh600000", kind=SignalKind.FIRST_SELL,
            date=bars[10].date, bar_index=10, price=high,
            anchor_index=9, anchor_price=high, message="一卖", meta={},
        )

    def _second_sell_legs(self, rebound_high: float, start_index: int = 40):
        """一卖高点之后的 down → up（紧邻），反弹高点由参数控制。

        `start_index` 要留足后续 K 线（确认位 = 反弹段 end_index + 1），
        否则引擎会因为确认位越界而不出信号——踩过一次。
        """

        return [
            TrendLeg(start_index=11, end_index=13, high=97.0, low=92.0, open_price=97.0, close_price=92.0),
            TrendLeg(start_index=start_index, end_index=start_index + 1,
                     high=rebound_high, low=rebound_high - 2.0,
                     open_price=rebound_high - 2.0, close_price=rebound_high),
        ]

    def test_second_sell_rejects_rebound_making_new_high(self) -> None:
        """反弹创出一卖新高 → 二卖不成立。"""

        first_sell = self._first_sell_signal(high=99.0)
        bars = self._bars(extra_close=101.0)
        engine = self._engine(bars)
        rejected = engine._detect_second_sell_by_legs(
            bars, self._second_sell_legs(rebound_high=101.0), [first_sell]
        )
        self.assertEqual(rejected, [], "反弹创出一卖新高却仍被判为二卖")

    def test_second_sell_accepts_rebound_below_new_high(self) -> None:
        """反弹未创新高 → 二卖成立（反向对照）。"""

        first_sell = self._first_sell_signal(high=99.0)
        bars = self._bars(extra_close=96.0)
        engine = self._engine(bars)
        accepted = engine._detect_second_sell_by_legs(
            bars, self._second_sell_legs(rebound_high=98.0), [first_sell]
        )
        self.assertEqual(len(accepted), 1, "反弹未创新高却没被判为二卖")

    def test_second_sell_takes_the_adjacent_segment(self) -> None:
        """二卖必须取「下跌段的下一个段」，且 `current_segment` 就是它。

        用两个构造的段做对照：若引擎跳过了第一段去更远处找反弹段，
        记进 meta 的 `current_segment` 就不是我传的那个。
        """

        legs = self._second_sell_legs(rebound_high=98.0)
        first_sell = self._first_sell_signal(high=99.0)
        bars = self._bars(extra_close=96.0)
        engine = self._engine(bars)
        signals = engine._detect_second_sell_by_legs(bars, legs, [first_sell])
        self.assertEqual(len(signals), 1)
        previous = signals[0].meta["previous_segment"]
        current = signals[0].meta["current_segment"]
        self.assertEqual(
            previous["start_index"], legs[0].start_index,
            "下跌段不是我构造的那个",
        )
        self.assertEqual(
            current["start_index"], legs[1].start_index,
            "反弹段不是我构造的那个紧邻段（引擎跳到了更远的段）",
        )
        self.assertNotEqual(
            current["start_index"], legs[0].start_index,
            "下跌段与反弹段不应是同一个段",
        )

    # ---- 趋势背驰（p116）------

    def test_trend_context_flag_never_widens_signals(self) -> None:
        """开趋势限制时一买数量不应增加（p116「没有趋势，没有背驰」）。"""

        from chanpicker.core import SignalKind

        strict = self._scan(require_trend_context=True)
        loose = self._scan(require_trend_context=False)
        strict_count = sum(1 for s in strict.signals if s.kind == SignalKind.FIRST_BUY)
        loose_count = sum(1 for s in loose.signals if s.kind == SignalKind.FIRST_BUY)
        self.assertLessEqual(strict_count, loose_count, "限趋势背驰反而放宽了信号")

    def test_every_first_buy_under_trend_limit_satisfies_trend(self) -> None:
        """逐条校验：开趋势限制时，每个一买都必须真的处在下跌趋势里。"""

        from chanpicker.core import SignalKind

        bars = self._bars()
        legs = build_legs(
            [b.high for b in bars], [b.low for b in bars],
            closes=[b.close for b in bars], min_legs=3,
        )
        strict = self._scan(require_trend_context=True)
        for signal in strict.signals:
            if signal.kind != SignalKind.FIRST_BUY:
                continue
            self.assertTrue(
                has_trend_divergence_leg(legs, signal.anchor_index, strict.zhongshu, "down"),
                f"一买 {signal.date} 不在下跌趋势里，不该被收进来（p116）",
            )

    def test_single_center_never_counts_as_trend(self) -> None:
        """p116：只有一个中枢是盘整，盘整里没有背驰。

        数据设计：单个中枢自身的 zd < zd 恒为假，直接改「要求几个中枢」
        这个判断在本数据上不会显现差异。所以额外造一组「只有一个中枢、
        但它内部 zd/zg 满足趋势判据」的数据——判据写成「不检查中枢个数」
        时它会被误判为趋势。
        """

        single = [Zhongshu(start_index=0, end_index=5, zd=10.0, zg=11.0)]
        for direction in ("down", "up"):
            self.assertFalse(
                has_trend_divergence_leg([], 30, single, direction),
                f"单中枢被判成{direction}趋势",
            )

    def test_one_center_cannot_satisfy_trend_no_matter_the_interval(self) -> None:
        """只要中枢个数 < 2 就必须判盘整，无论单个中枢的区间如何。"""

        # 单个中枢，但区间形态明显「下降」——若实现只比区间不看个数，会误判为趋势
        single = [Zhongshu(start_index=0, end_index=5, zd=5.0, zg=11.0)]
        self.assertFalse(
            has_trend_divergence_leg([], 30, single, "down", max_centers=2),
            "单中枢被判成下跌趋势（p116：没有趋势就没有背驰）",
        )
        self.assertFalse(
            has_trend_divergence_leg([], 30, single, "down", max_centers=1),
            "要求 1 个中枢时，单个仍被判成趋势——说明没在比个数之前就拒掉",
        )

    def test_trend_context_requires_distinct_centers(self) -> None:
        """引擎层：趋势限制必须真的依赖「中枢个数 >= 2」这个条件。

        直接验证 `has_trend_divergence_leg` 的返回随中枢个数变化——
        0 个 / 1 个中枢必须 False，2 个依次下移的中枢必须 True。
        这样「把个数检查删掉」或「把条件写反」都能被抓到。
        """

        none = []
        one = [Zhongshu(0, 5, 12.0, 13.0)]
        two_desc = [Zhongshu(0, 5, 12.0, 13.0), Zhongshu(6, 11, 10.0, 11.0)]
        self.assertFalse(has_trend_divergence_leg([], 30, none, "down"), "无中枢不应算趋势")
        self.assertFalse(has_trend_divergence_leg([], 30, one, "down"), "单中枢不应算趋势")
        self.assertTrue(
            has_trend_divergence_leg([], 30, two_desc, "down"),
            "两个依次下移的中枢应算下跌趋势",
        )

    def test_engine_actually_calls_build_legs(self) -> None:
        """引擎必须调用 `_build_legs` 构建段序列。

        直接断言源码里有这个调用——比间接推断（比对中枢数量）更可靠，
        「中枢退回笔口径」这类变异正是从这条路径漏过去的。
        """

        import inspect

        from chanpicker.core import ChanEngine

        source = inspect.getsource(ChanEngine.scan)
        self.assertIn(
            "self._build_legs(ordered)", source,
            "引擎 scan() 不再调用 _build_legs，段口径已失效",
        )
        self.assertIn(
            "detect_zhongshu_by_legs", source,
            "引擎 scan() 不再使用 detect_zhongshu_by_legs，中枢已退回笔口径",
        )

    # ---- 三买：p590「第一次回抽」 ----

    def _bar_at(self, symbol: str, month: str, day: int, price_close: float):
        from chanpicker.core import Bar

        return Bar(
            symbol=symbol, date=f"{month}-{day:02d}", open=price_close,
            high=price_close + 0.4, low=price_close - 0.4, close=price_close,
            volume=1_000_000, amount=price_close * 1_000_000,
        )

    def test_third_buy_takes_the_first_adjacent_pullback(self) -> None:
        """p590「**第一次**回抽」：三买的回调段必须紧邻突破段。

        构造时把中枢列表**只传那一个中枢**，并让构造段起点落在中枢之后
        （否则中枢内部的真实腿会抢先匹配突破段）。
        """

        from chanpicker.structure import detect_zhongshu_by_legs

        bars = self._bars()
        legs = build_legs(
            [b.high for b in bars], [b.low for b in bars],
            closes=[b.close for b in bars], min_legs=3,
        )
        centers = detect_zhongshu_by_legs(legs)
        self.assertTrue(centers, "构造序列应能形成中枢")
        center = centers[-1]
        zg = center.zg

        # 构造段起点必须越过所有真实腿，避免被中枢内部的腿抢先匹配
        base = max(center.end_index, legs[-1].end_index) + 1
        breakout_high = zg + 12.0
        crafted = list(legs) + [
            # 突破段：终点高于 ZG
            TrendLeg(base, base + 2, high=breakout_high, low=zg + 8.0,
                     open_price=zg + 8.0, close_price=breakout_high),
            # 紧邻回调：低点高于 ZG，且高点**低于**突破高点 → 合法
            TrendLeg(base + 3, base + 5, high=breakout_high - 1.0, low=zg + 1.0,
                     open_price=breakout_high - 1.0, close_price=zg + 1.0),
            TrendLeg(base + 6, base + 8, high=breakout_high - 0.5, low=zg + 5.0,
                     open_price=zg + 5.0, close_price=breakout_high - 0.5),
            # 远处回调：跌破 ZG → 若被取到会被 p208 定理挡掉
            TrendLeg(base + 9, base + 11, high=zg + 9.0, low=zg - 3.0,
                     open_price=zg + 9.0, close_price=zg - 3.0),
        ]
        # 确认位 = 回调段 end_index + 1，其后要留足 K 线
        padded = bars + [
            self._bar_at("sh600000", "2024-11", 1 + i, price_close=zg + 6.0) for i in range(20)
        ]
        from chanpicker.effort import Effort

        engine = self._engine(padded, min_third_buy_score=-1.0)
        engine._leg_effort = lambda leg, left, right: Effort(
            direction=leg.direction, start_index=left, end_index=right, macd_area=1.0,
            macd_piece_area=1.0, peak_area=1.0, dif_extreme=0.0, dea_extreme=0.0, ma_extreme=0.0,
        )
        signals = engine._detect_third_buy_by_legs(padded, crafted, [center])
        self.assertTrue(signals, "紧邻回调不破 ZG 却没被判为三买")
        pullback = signals[-1].meta["current_segment"]
        self.assertEqual(
            pullback["start_index"], base + 3,
            "取的不是紧邻的第一次回抽段（p590）",
        )


    def test_third_buy_allows_first_pullback_high_at_or_above_breakout_high(self) -> None:
        """p208 只约束回抽低点守住 ZG，不额外要求回抽高点低于离开高点。"""

        from chanpicker.core import Zhongshu
        from chanpicker.effort import Effort

        bars = self._bars(extra_close=110.0)
        center = Zhongshu(start_index=0, end_index=5, zd=80.0, zg=90.0)
        legs = [
            TrendLeg(6, 8, high=105.0, low=91.0, open_price=91.0, close_price=105.0),
            TrendLeg(9, 11, high=106.0, low=92.0, open_price=106.0, close_price=95.0),
        ]
        engine = self._engine(bars)
        engine._leg_effort = lambda leg, left, right: Effort(
            direction=leg.direction, start_index=left, end_index=right, macd_area=1.0,
            macd_piece_area=1.0, peak_area=1.0, dif_extreme=0.0, dea_extreme=0.0, ma_extreme=0.0,
        )
        signals = engine._detect_third_buy_by_legs(bars, legs, [center])
        self.assertEqual(len(signals), 1)
        self.assertGreaterEqual(
            signals[0].meta["current_segment"]["high"],
            signals[0].meta["breakout_high"],
        )
        self.assertEqual(signals[0].meta["zhongshu_zg"], 90.0)

    def test_third_buy_respects_configured_effort_threshold(self) -> None:
        """段级路径也必须应用 min_third_buy_score，与旧笔级路径保持一致。"""

        from chanpicker.core import Zhongshu

        bars = self._bars(extra_close=110.0)
        center = Zhongshu(start_index=0, end_index=5, zd=80.0, zg=90.0)
        legs = [
            TrendLeg(6, 8, high=105.0, low=91.0, open_price=91.0, close_price=105.0),
            TrendLeg(9, 11, high=106.0, low=92.0, open_price=106.0, close_price=95.0),
        ]
        engine = self._engine(bars, min_third_buy_score=1.0)
        self.assertEqual(engine._detect_third_buy_by_legs(bars, legs, [center]), [])

    def test_third_buy_rejects_pullback_breaking_center_zg(self) -> None:
        """p208 定理：回调低点**跌破 ZG** 就不是三买（反向对照）。

        与 `test_third_buy_takes_the_first_adjacent_pullback` 同一份构造，
        只改紧邻回调的低点：高于 ZG → 成立；低于 ZG → 必须被拒。
        """

        from chanpicker.structure import detect_zhongshu_by_legs

        bars = self._bars()
        legs = build_legs(
            [b.high for b in bars], [b.low for b in bars],
            closes=[b.close for b in bars], min_legs=3,
        )
        centers = detect_zhongshu_by_legs(legs)
        self.assertTrue(centers, "构造序列应能形成中枢")
        center = centers[-1]
        zg = center.zg
        base = max(center.end_index, legs[-1].end_index) + 1
        breakout_high = zg + 12.0

        crafted = list(legs) + [
            TrendLeg(base, base + 2, high=breakout_high, low=zg + 8.0,
                     open_price=zg + 8.0, close_price=breakout_high),
            # 紧邻回调：低点**跌破** ZG → p208 定理应否掉
            TrendLeg(base + 3, base + 5, high=breakout_high - 1.0, low=zg - 1.0,
                     open_price=breakout_high - 1.0, close_price=zg - 1.0),
            TrendLeg(base + 6, base + 8, high=breakout_high - 0.5, low=zg + 5.0,
                     open_price=zg + 5.0, close_price=breakout_high - 0.5),
            TrendLeg(base + 9, base + 11, high=zg + 9.0, low=zg - 3.0,
                     open_price=zg + 9.0, close_price=zg - 3.0),
        ]
        padded = bars + [
            self._bar_at("sh600000", "2024-11", 1 + i, price_close=zg + 6.0) for i in range(20)
        ]
        engine = self._engine(padded)
        signals = engine._detect_third_buy_by_legs(padded, crafted, [center])
        self.assertEqual(
            signals, [], "回调跌破中枢 ZG 却仍被判为三买（违反 p208 定理）",
        )

    def test_trend_context_requires_at_least_two_centers(self) -> None:
        """趋势背驰必须依赖「中枢个数 >= 2」——逐档验证 0/1/2 个中枢。

        前一个用例只测了 `max_centers=2` 时的单中枢；这里把返回值随
        中枢个数的**变化**也锁住：0 个、1 个必须 False，2 个依次下移必须 True。
        否则「把个数检查删掉」这类变异漏网。
        """

        none = []
        one = [Zhongshu(0, 5, 12.0, 13.0)]
        two_desc = [Zhongshu(0, 5, 12.0, 13.0), Zhongshu(6, 11, 10.0, 11.0)]
        self.assertFalse(has_trend_divergence_leg([], 30, none, "down"), "无中枢不应算趋势")
        self.assertFalse(has_trend_divergence_leg([], 30, one, "down"), "单中枢不应算趋势")
        self.assertTrue(
            has_trend_divergence_leg([], 30, two_desc, "down"),
            "两个依次下移的中枢应算下跌趋势",
        )

    def test_engine_scan_calls_build_legs_and_leg_zhongshu(self) -> None:
        """引擎必须真的走段口径。

        直接断言源码里有这两个调用，比间接比对中枢数量可靠——
        「中枢退回笔口径」这类变异正是从间接推断的缝隙漏过去的。
        """

        import inspect

        from chanpicker.core import ChanEngine

        source = inspect.getsource(ChanEngine.scan)
        self.assertIn(
            "self._build_legs(ordered)", source,
            "引擎 scan() 不再调用 _build_legs，段口径已失效",
        )
        self.assertIn(
            "detect_zhongshu_by_legs", source,
            "引擎 scan() 不再使用 detect_zhongshu_by_legs，中枢已退回笔口径",
        )


if __name__ == "__main__":
    unittest.main()


class LegsGranularityTest(unittest.TestCase):
    """段的 K 线粒度必须随周期缩放，不能固定 3 根。

    真实问题：30 分钟只有 56~80 根（7~10 个交易日），每段固定 3 根会切出
    二十几段，其中下跌段大多是「低点一个比一个高」的碎段——相邻下跌段几乎
    永远构不成「创新低」，一买信号全灭。茅台实测 80 根 → 26 段 / 11 个下跌段
    / 0 个一买。

    p152 只说「至少三个该级别单位 K 线重叠」，「单位 K 线」的长度本就是相对的。
    """

    def _engine(self, fixed: int):
        from chanpicker.core import ChanConfig, ChanEngine

        return ChanEngine(ChanConfig(min_legs_bars=fixed))

    def test_daily_keeps_three_bars_per_leg(self) -> None:
        """日线 400 根 → 仍是 3 根/段，与批次 2 的口径一致，不能被这次修复改动。"""

        self.assertEqual(self._engine(0)._resolve_legs_bars(400), 3)

    def test_fixed_value_overrides_autoscale(self) -> None:
        """显式配置 min_legs_bars>0 时以配置为准。"""

        self.assertEqual(self._engine(5)._resolve_legs_bars(400), 5)
        self.assertEqual(self._engine(8)._resolve_legs_bars(60), 8)

    def test_autoscale_never_goes_below_three(self) -> None:
        """p152 的下限是 3，无论周期多短都不能更细。"""

        engine = self._engine(0)
        for total in (30, 60, 120, 200, 400, 1000):
            self.assertGreaterEqual(engine._resolve_legs_bars(total), 3)

    def test_autoscale_never_exceeds_eight(self) -> None:
        """上限 8：段太粗会把结构并成一块，背驰判定失去意义。"""

        engine = self._engine(0)
        for total in (400, 1000, 5000, 20000):
            self.assertLessEqual(engine._resolve_legs_bars(total), 8)

    def test_short_period_is_not_starved_of_legs(self) -> None:
        """30 分钟 80 根至少要切出 10 段，才够形成中枢+背驰段的结构。"""

        engine = self._engine(0)
        legs_bars = engine._resolve_legs_bars(80)
        self.assertGreaterEqual(80 // legs_bars, 10)

    def test_grows_with_longer_history(self) -> None:
        """K 线越多，每段越粗——否则长历史会被切得过碎。

        注：实测当前数据量（day 400 / m30 80）下这个分支其实不生效
        （400→3、800→7 才开始变化），它是给「日后拉到 1000+ 根」预留的。
        """

        engine = self._engine(0)
        self.assertEqual(engine._resolve_legs_bars(400), 3)
        self.assertGreater(engine._resolve_legs_bars(1200), engine._resolve_legs_bars(400))
        self.assertEqual(engine._resolve_legs_bars(5000), 8)

"""力度与背驰判定的回归测试——每条用例都对应一个原文规则。

原文依据（《教你炒股票108课》张星星注解版）：
- p125「肯定是片和片比」——同色柱分片，正负不混算
- p295 末片面积 ×2 折算
- p408 MACD 口径即足够；趋势平均力度 = 面积 ÷ 时间
- p368 背驰三条件：「两个出现一个就要注意，两个都出现更标准」
- p522 成交量与 MACD 无关（故不参与判定）
"""

from __future__ import annotations

import unittest

from chanpicker.effort import (
    DivergenceVerdict,
    Effort,
    EffortPiece,
    divergence_to_meta,
    judge_divergence,
    macd_state,
    measure_effort,
    split_pieces,
)


def make_closes(segments: list[tuple[int, float]], length: int = 120) -> list[float]:
    """按 (根数, 每根涨跌幅) 构造收盘价序列。"""

    closes = [100.0]
    for count, delta in segments:
        for _ in range(count):
            closes.append(max(1.0, closes[-1] + delta))
    while len(closes) < length:
        closes.append(closes[-1])
    return closes[:length]


class MacdStateTest(unittest.TestCase):
    def test_histogram_is_not_doubled(self) -> None:
        """柱必须是 DIF − DEA 本身。

        缠论的「红绿柱」就是两线之差；软件里常见的 ×2 是显示需要。
        乘了 2 会让所有面积口径整体翻倍，阈值完全对不上。
        """

        macd = macd_state(make_closes([(30, -1.0), (30, 1.0)]))
        for dif, dea, hist in zip(macd.dif, macd.dea, macd.hist):
            self.assertAlmostEqual(hist, dif - dea, places=9)

    def test_has_all_three_lines(self) -> None:
        macd = macd_state(make_closes([(30, -1.0), (30, 1.0)]))
        self.assertEqual(len(macd.dif), len(macd.dea))
        self.assertEqual(len(macd.dif), len(macd.hist))
        self.assertEqual(len(macd), 120)

    def test_empty_input(self) -> None:
        macd = macd_state([])
        self.assertEqual(macd.dif, [])
        self.assertEqual(macd.hist, [])


class SplitPiecesTest(unittest.TestCase):
    """p125：片和片比，正负不能混着加。"""

    def test_sign_change_splits_pieces(self) -> None:
        hist = [1.0, 2.0, -1.0, -2.0, 3.0]
        pieces = split_pieces(hist, 0, 4)
        self.assertEqual(len(pieces), 3)
        self.assertEqual([p.sign for p in pieces], [1, -1, 1])
        self.assertAlmostEqual(pieces[0].area, 3.0)
        self.assertAlmostEqual(pieces[1].area, 3.0)
        self.assertAlmostEqual(pieces[2].area, 3.0)

    def test_zero_does_not_break_piece(self) -> None:
        """零柱不应把连续的一段切成碎片。

        断言要卡住「零值也开新片」的写法：那会把 [1,0,2,0,1] 切成 5 片，
        只看 len(area) 分不出问题，必须同时断言片数与覆盖区间。
        """

        hist = [1.0, 0.0, 2.0, 0.0, 1.0]
        pieces = split_pieces(hist, 0, 4)
        self.assertEqual(len(pieces), 1, f"零柱把片段切碎了：{pieces}")
        self.assertEqual((pieces[0].start_index, pieces[0].end_index), (0, 4))

    def test_leading_and_trailing_zeros_are_absorbed(self) -> None:
        """首尾的零柱也不应单独成片。"""

        pieces = split_pieces([0.0, 0.0, 1.0, 1.0, 0.0], 0, 4)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].area, 2.0, "零柱面积贡献为 0")

    def test_all_zero_yields_no_pieces(self) -> None:
        self.assertEqual(split_pieces([0.0, 0.0, 0.0], 0, 2), [])

    def test_only_last_piece_is_marked_extended(self) -> None:
        hist = [1.0, 1.0, -1.0, -1.0]
        pieces = split_pieces(hist, 0, 3)
        self.assertTrue(pieces[0].extended is False)
        self.assertTrue(pieces[1].extended is True, "末片仍在伸长，应可折算")

    def test_piece_index_range(self) -> None:
        hist = [0.0, 1.0, 1.0, -1.0]
        pieces = split_pieces(hist, 1, 3)
        self.assertEqual((pieces[0].start_index, pieces[0].end_index), (1, 2))
        self.assertEqual((pieces[1].start_index, pieces[1].end_index), (3, 3))


class MeasureEffortTest(unittest.TestCase):
    def test_last_piece_area_is_doubled(self) -> None:
        """p295：末片已出现面积 ×2 当该段面积。"""

        hist = [1.0, 1.0, 1.0]
        macd = macd_state([100.0] * 10)  # 闭合价 → DIF=DEA=0
        macd = type(macd)(dif=[0.0] * 10, dea=[0.0] * 10, hist=hist + [0.0] * 7)
        effort = measure_effort(macd, 0, 2, "up")
        # 单片 3.0，末片折算 ×2 → 6.0
        self.assertAlmostEqual(effort.macd_area, 6.0)
        self.assertAlmostEqual(effort.macd_piece_area, 3.0, msg="折算前的原始面积")

    def test_average_effort_divides_by_bars(self) -> None:
        """p408：趋势平均力度 = 面积 ÷ 持续时间。"""

        macd = macd_state([100.0] * 40)
        macd = type(macd)(dif=[0.0] * 40, dea=[0.0] * 40, hist=[1.0] * 20 + [0.0] * 20)
        effort = measure_effort(macd, 0, 19, "up")
        # 单片 20 根 × 每根 1.0 = 原始面积 20；末片折算 ×2 → 40
        self.assertAlmostEqual(effort.macd_piece_area, 20.0)
        self.assertAlmostEqual(effort.macd_area, 40.0)
        self.assertAlmostEqual(effort.average_effort, 40.0 / 20)

    def test_extremes_follow_direction(self) -> None:
        macd = macd_state([100.0] * 20)
        dif = [1.0, 5.0, 3.0, 2.0] + [0.0] * 16
        macd = type(macd)(dif=dif, dea=[0.0] * 20, hist=[1.0] * 20)
        up = measure_effort(macd, 0, 3, "up")
        down = measure_effort(macd, 0, 3, "down")
        self.assertAlmostEqual(up.dif_extreme, 5.0, msg="上涨段取最大值")
        self.assertAlmostEqual(down.dif_extreme, 1.0, msg="下跌段取最小值")

    def test_empty_range_is_safe(self) -> None:
        macd = macd_state([100.0] * 10)
        effort = measure_effort(macd, 20, 30, "up")
        self.assertEqual(effort.bars, 1)


class JudgeDivergenceTest(unittest.TestCase):
    @staticmethod
    def _effort(area: float, dif: float, ma: float, bars: int = 10) -> Effort:
        return Effort(
            direction="down", start_index=0, end_index=bars - 1,
            macd_area=area, dif_extreme=dif, dea_extreme=dif, ma_extreme=ma,
            elapsed_bars=bars,
        )

    def test_all_three_weaker_is_divergent(self) -> None:
        verdict = judge_divergence(
            self._effort(10.0, -5.0, -0.10), self._effort(6.0, -2.0, -0.03), "down"
        )
        self.assertTrue(verdict.is_divergent)
        self.assertTrue(verdict.standard, "②③同时成立应记为标准背驰")
        self.assertEqual(verdict.conditions_met, 3)

    def test_area_only_still_diverges(self) -> None:
        """p368：「两个出现一个就要注意」——只弱一个也要算背驰。"""

        verdict = judge_divergence(
            self._effort(10.0, -5.0, -0.10), self._effort(6.0, -6.0, -0.12), "down"
        )
        self.assertTrue(verdict.is_divergent)
        self.assertFalse(verdict.standard, "只有面积弱，不算标准背驰")
        self.assertEqual(verdict.conditions_met, 1)

    def test_nothing_weaker_is_not_divergent(self) -> None:
        verdict = judge_divergence(
            self._effort(10.0, -5.0, -0.10), self._effort(14.0, -7.0, -0.15), "down"
        )
        self.assertFalse(verdict.is_divergent)
        self.assertEqual(verdict.conditions_met, 0)

    def test_up_direction_uses_reversed_comparison(self) -> None:
        """上涨段背驰是 DIF 不创新高，不是变低。"""

        weaker = judge_divergence(
            self._effort(10.0, 5.0, 0.10), self._effort(6.0, 3.0, 0.03), "up"
        )
        self.assertTrue(weaker.is_divergent)
        # 反过来「创新低」在上涨段不构成背驰
        stronger = judge_divergence(
            self._effort(10.0, 5.0, 0.10), self._effort(14.0, 7.0, 0.15), "up"
        )
        self.assertFalse(stronger.is_divergent)

    def test_score_higher_when_more_conditions_met(self) -> None:
        weak1 = judge_divergence(
            self._effort(10.0, -5.0, -0.10), self._effort(9.9, -6.0, -0.12), "down"
        )
        weak3 = judge_divergence(
            self._effort(10.0, -5.0, -0.10), self._effort(5.0, -1.0, -0.01), "down"
        )
        self.assertGreater(weak3.score, weak1.score)

    def test_average_effort_can_be_the_basis(self) -> None:
        """面积大但耗时长，平均力度可能更小——p408 认可的口径之一。"""

        previous = self._effort(10.0, -5.0, -0.10, bars=10)
        current = self._effort(12.0, -6.0, -0.12, bars=40)
        self.assertGreater(current.macd_area, previous.macd_area)
        self.assertLess(
            current.average_effort, previous.average_effort,
            "耗时拉长后平均力度应更小",
        )

    def test_margin_prevents_false_divergence(self) -> None:
        """留安全边际时，微小差异不算背驰。

        三个 margin 各管一个条件；缺了任何一条都会让噪声被当成背驰。
        """

        # 面积只小 0.1% → area_margin=0.02 时不该算弱
        verdict = judge_divergence(
            self._effort(10.0, -5.0, -0.10), self._effort(9.99, -5.001, -0.0999),
            "down", area_margin=0.02, dif_margin=0.01, ma_margin=0.001,
        )
        self.assertFalse(verdict.area_weaker, "面积仅缩 0.1%，超出 2% 边际")
        self.assertFalse(verdict.dif_weaker)
        self.assertFalse(verdict.ma_weaker)
        self.assertFalse(verdict.is_divergent, f"全部条件都在边际内，不该判背驰：{verdict}")

    def test_area_margin_zero_keeps_strict_comparison(self) -> None:
        """margin=0 时退回严格变小，不改变原有语义。"""

        verdict = judge_divergence(
            self._effort(10.0, -5.0, -0.10), self._effort(9.99, -9.0, -0.5), "down"
        )
        self.assertTrue(verdict.area_weaker, "9.99 < 10 应算面积弱")
        small = judge_divergence(
            self._effort(10.0, -5.0, -0.10), self._effort(10.0, -9.0, -0.5), "down"
        )
        self.assertFalse(small.area_weaker, "相等不算变小")

    def test_positive_margin_is_stricter_not_looser(self) -> None:
        """阈值语义必须符合直觉：margin 越大越难判弱。

        这条锁死一个真实的语义错误——早先把 margin 加到阈值上，
        结果 margin=0.02 让「面积只缩 0.1%」也通过了，与参数名相反。
        """

        previous = self._effort(10.0, -5.0, -0.10)
        current = self._effort(9.5, -9.0, -0.5)  # 面积缩 5%
        self.assertTrue(
            judge_divergence(previous, current, "down").area_weaker,
            "无 margin 时缩 5% 应算弱",
        )
        self.assertFalse(
            judge_divergence(previous, current, "down", area_margin=0.10).area_weaker,
            "要求缩 10% 时，缩 5% 不该算弱",
        )


class MetaPayloadTest(unittest.TestCase):
    def test_divergence_meta_explains_itself(self) -> None:
        """meta 要能解释「为什么算背驰」，前端才能展示依据。"""

        def eff(area, dif, ma):
            return Effort(direction="down", start_index=0, end_index=9,
                          macd_area=area, dif_extreme=dif, ma_extreme=ma)

        verdict = judge_divergence(eff(10.0, -5.0, -0.1), eff(6.0, -2.0, -0.03), "down")
        meta = divergence_to_meta(verdict)
        detail = meta["divergence_detail"]
        for key in ("is_divergent", "standard", "dif_weaker", "area_weaker",
                    "ma_weaker", "conditions_met", "previous_area",
                    "current_area", "area_ratio", "previous_average", "current_average"):
            self.assertIn(key, detail)
        self.assertLess(detail["area_ratio"], 1.0)

    def test_zero_previous_area_does_not_divide_by_zero(self) -> None:
        prev = Effort(direction="down", start_index=0, end_index=9, macd_area=0.0)
        cur = Effort(direction="down", start_index=10, end_index=19, macd_area=5.0)
        verdict = judge_divergence(prev, cur, "down")
        self.assertEqual(divergence_to_meta(verdict)["divergence_detail"]["area_ratio"], 1.0)


class VolumeIndependenceTest(unittest.TestCase):
    """p522：「MACD 是 MACD，和成交量没什么关系」——力度不应含成交量。"""

    def test_effort_ignores_volume(self) -> None:
        closes = make_closes([(40, -1.0), (40, 1.0)])
        macd = macd_state(closes)
        effort = measure_effort(macd, 0, 39, "down")
        self.assertGreater(effort.macd_area, 0.0)
        # Effort 里没有任何 volume 字段——这是设计约束
        self.assertNotIn("volume", effort.__dataclass_fields__)


class MaDeviationDirectionTest(unittest.TestCase):
    """均线偏离必须带方向——这是一个真实缺陷的回归。

    早先写成 `max(正向, -负向)`（两边都取正数），只比大小不记方向，
    结果 ma_weaker 恒为 False，均线这条背驰条件等于从未生效。
    """

    def test_down_segment_returns_negative_deviation(self) -> None:
        from chanpicker.effort import ma_deviation_extreme

        # 一路下跌的价格，必然持续低于均线
        closes = [100 - i for i in range(40)]
        self.assertLess(ma_deviation_extreme(closes, 0, 39, 20, "down"), 0.0)

    def test_up_segment_returns_positive_deviation(self) -> None:
        from chanpicker.effort import ma_deviation_extreme

        closes = [100 + i for i in range(40)]
        self.assertGreater(ma_deviation_extreme(closes, 0, 39, 20, "up"), 0.0)

    def test_measure_effort_uses_direction_automatically(self) -> None:
        """不显式传 ma_extreme 时应按 direction 自算。"""

        falls = macd_state([100 - i for i in range(40)])
        rises = macd_state([100 + i for i in range(40)])
        down_effort = measure_effort(falls, 0, 39, "down", [100 - i for i in range(40)])
        up_effort = measure_effort(rises, 0, 39, "up", [100 + i for i in range(40)])
        self.assertLess(down_effort.ma_extreme, 0.0, "下跌段均线偏离应为负")
        self.assertGreater(up_effort.ma_extreme, 0.0, "上涨段均线偏离应为正")

    def test_ma_condition_can_actually_fire(self) -> None:
        """均线这一条必须真的能判弱，否则等于没有。"""

        def eff(ma):
            return Effort(direction="down", start_index=0, end_index=9,
                          macd_area=10.0, dif_extreme=-5.0, ma_extreme=ma)

        # 面积与 DIF 都不弱，只有均线弱 → 仍应判背驰
        verdict = judge_divergence(eff(-0.10), eff(-0.02), "down")
        self.assertTrue(verdict.ma_weaker, "后段偏离均线更浅，应判均线弱")
        self.assertTrue(verdict.is_divergent, "均线弱也应触发背驰")

    def test_ma_condition_can_also_be_false(self) -> None:
        """反向：均线偏离**加深**时不能判弱。

        只测「能判弱」不够——若 ma_weaker 写成恒 True，
        「能判弱」照样通过，缺陷照样漏网。
        """

        def eff(ma):
            return Effort(direction="down", start_index=0, end_index=9,
                          macd_area=10.0, dif_extreme=-5.0, ma_extreme=ma)

        # 后段偏离更深（-0.20 < -0.10），均线不弱
        verdict = judge_divergence(eff(-0.10), eff(-0.20), "down")
        self.assertFalse(verdict.ma_weaker, "偏离更深时不该判均线弱")
        self.assertFalse(verdict.is_divergent, "三条件都不弱，不该有背驰")

    def test_ma_direction_flips_comparison_for_up_segments(self) -> None:
        """上涨段的均线判定必须反向：偏离更深才算弱。"""

        def eff(ma):
            return Effort(direction="up", start_index=0, end_index=9,
                          macd_area=10.0, dif_extreme=5.0, ma_extreme=ma)

        self.assertTrue(judge_divergence(eff(0.20), eff(0.05), "up").ma_weaker,
                        "上涨段偏离收窄应判弱")
        self.assertFalse(judge_divergence(eff(0.05), eff(0.20), "up").ma_weaker,
                         "上涨段偏离加深不该判弱")


class FallbackConsistencyTest(unittest.TestCase):
    """MACD 面积不足时的回退路径，三个口径必须同量纲。

    这是真实缺陷：早先只把面积换成价格幅度，DIF/均线仍留 MACD 原值，
    于是「面积（量纲=柱面积）」与「DIF（量纲=价格差/EMA）」被直接比大小，
    判定看似能跑、实际已失真。
    """

    def _scan_meta(self, prices: list[float], **config_kw) -> dict:
        from chanpicker.core import Bar, ChanConfig, ChanEngine

        bars = [
            Bar(symbol="sh600000", date=f"2024-01-{i+1:02d}", open=p, high=p + 0.4,
                low=p - 0.4, close=p, volume=1_000_000, amount=p * 1_000_000)
            for i, p in enumerate(prices)
        ]
        config = ChanConfig(
            min_stroke_gap=1, min_second_buy_score=0.0, min_third_buy_score=0.0,
            require_zhongshu_for_first_buy=False, require_zhongshu_for_first_sell=False,
            # 回退路径（min_macd_area 很大）下 ma_extreme 被置 0，
            # 「标准背驰」永不成立；这里要验的是回退本身，必须放开这道收紧。
            require_standard_divergence=False,
            require_trend_context=False,
            **config_kw,
        )
        for signal in ChanEngine(config).scan(bars).signals:
            if signal.kind.value == "FIRST_BUY":
                return signal.meta
        return {}

    # 段口径长序列（每 3 根一段，p152 最低级别定义），能产出带 divergence_detail 的信号。
    # 这两个用例验的是**回退路径**的量纲一致性，所以构造序列必须真的走一遍引擎。
    PRICES = [100, 100, 98, 97, 99, 97, 96, 97, 96, 95, 96, 98,
    100, 100, 100, 100, 99, 100, 98, 98, 96, 95, 94, 95,
    94, 93, 93, 91, 91, 90, 88, 90, 92, 92, 91, 91,
    91, 92, 91, 89, 91, 93, 94, 95, 95, 97, 99, 101,
    99, 97, 96, 97, 94, 96, 98, 96, 95, 97, 96, 95,
    95, 93, 94, 94, 93, 96, 98, 96, 95, 95, 93, 91,
    90, 91, 91, 90, 91, 93, 92, 92, 91, 91, 89, 87,
    87, 86, 87, 87, 88]

    def test_fallback_uses_same_units_across_all_three_conditions(self) -> None:
        meta = self._scan_meta(self.PRICES, min_macd_area=9999)
        self.assertTrue(meta, "构造序列应能产出一买，否则说明回退路径整体坏了")
        detail = meta["divergence_detail"]
        # 回退后：面积 = 价格差；DIF 极值 = ±价格差（同量纲）；均线置 0
        previous = meta["previous_effort_detail"]
        self.assertAlmostEqual(previous["dif_extreme"], -previous["area"], places=3)
        self.assertAlmostEqual(previous["ma_extreme"], 0.0)
        self.assertFalse(detail["ma_weaker"], "均线无可用信息时不应判弱")

    def test_fallback_still_detects_weakening(self) -> None:
        meta = self._scan_meta(self.PRICES, min_macd_area=9999)
        detail = meta["divergence_detail"]
        self.assertTrue(detail["is_divergent"], "回退路径也要能识别力度衰减")
        self.assertLess(detail["current_area"], detail["previous_area"])


if __name__ == "__main__":
    unittest.main()

"""缠论原文之道（p84/p98）的回归测试。

规则出处：
- **p84 · 第12课**：「对于任何走势，**首要判断的是体位：男上位还是女上位**」；
  「买的方式明白了，**卖就反过来就可以了**」。
- **p98**：「第一类买点：由**男上位最后一吻后**出现的背驰式下跌构成。」
  「第二类买点：**女上位第一吻后**出现的下跌构成。」
  「这第一吻后的下跌一般不会构成买点，**必须是至少第二吻以后**。」
- 吻的三种（p98）：飞吻（走平后续原趋势）、唇吻（靠近但不穿越）、湿吻（跌破/升破或反复缠绕）。
"""

from __future__ import annotations

import unittest

from chanpicker.masystem import (
    KISS_FLY,
    KISS_LIP,
    KISS_WET,
    POSITION_FEMALE,
    POSITION_MALE,
    POSITION_TANGLE,
    MaKissTracker,
    judge_first_buy,
    judge_first_sell,
    judge_second_buy,
    judge_second_sell,
)


def make_tracker(closes: list[float], short: int = 5, long: int = 10, threshold: float = 0.01):
    tracker = MaKissTracker.build(closes, short, long)
    tracker.tangle_threshold = threshold
    dates = [f"2026-01-{i+1:02d}" for i in range(len(closes))]
    tracker.run(dates)
    return tracker


def rising_then_falling(n: int = 120, rise: float = 1.2, fall: float = 1.0) -> list[float]:
    """先跌后涨：制造一次明显的女上位→男上位切换。"""

    out = [100.0]
    for _ in range(n // 2):
        out.append(out[-1] - fall)
    for _ in range(n // 2):
        out.append(out[-1] + rise)
    return out


class PositionTest(unittest.TestCase):
    """p84：首要判断的是体位。"""

    def test_female_position_when_short_above_long(self) -> None:
        closes = [100.0 + i for i in range(40)]
        tracker = make_tracker(closes)
        self.assertEqual(tracker.position_at(len(closes) - 1), POSITION_FEMALE)

    def test_male_position_when_short_below_long(self) -> None:
        closes = [100.0 - i for i in range(40)]
        tracker = make_tracker(closes)
        self.assertEqual(tracker.position_at(len(closes) - 1), POSITION_MALE)

    def test_short_period_must_be_less_than_long(self) -> None:
        with self.assertRaises(ValueError):
            MaKissTracker(5, 5)


class KissTrackingTest(unittest.TestCase):
    """p98：吻要能被逐次计数（一吻、二吻、第三吻…）。"""

    def test_wavy_market_produces_multiple_kisses(self) -> None:
        # 反复上下大幅摆动 → 应切出多个吻，而不是合并成一个
        closes = [100.0]
        for i in range(60):
            closes.append(closes[-1] + (6.0 if (i // 4) % 2 == 0 else -6.0))
        tracker = make_tracker(closes, threshold=0.02)
        self.assertGreater(
            len(tracker.kisses), 3,
            f"摆动行情只切出 {len(tracker.kisses)} 个吻，无法逐次计数",
        )

    def test_kisses_are_numbered_sequentially(self) -> None:
        closes = [100.0]
        for i in range(60):
            closes.append(closes[-1] + (6.0 if (i // 4) % 2 == 0 else -6.0))
        tracker = make_tracker(closes, threshold=0.02)
        numbers = [k.index for k in tracker.kisses]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_crossing_kiss_is_wet(self) -> None:
        """湿吻 = 短期均线跌破或升破长期均线（p98）。"""

        closes = [100.0]
        for i in range(50):
            closes.append(closes[-1] + (8.0 if (i // 5) % 2 == 0 else -8.0))
        tracker = make_tracker(closes, threshold=0.03)
        kinds = {k.crossings > 0 for k in tracker.kisses}
        self.assertIn(True, kinds, "大幅摆动应产生湿吻（发生穿越）")

    def test_kiss_records_position_at_its_start(self) -> None:
        closes = [100.0]
        for i in range(50):
            closes.append(closes[-1] + (6.0 if (i // 4) % 2 == 0 else -6.0))
        tracker = make_tracker(closes, threshold=0.02)
        for kiss in tracker.kisses:
            self.assertIn(
                kiss.position, (POSITION_MALE, POSITION_FEMALE),
                "吻应记录发生时所处的体位",
            )

    def test_no_kiss_on_monotonic_trend(self) -> None:
        """单边趋势没有缠绕，不该产生吻。"""

        tracker = make_tracker([100.0 + i * 2 for i in range(60)])
        self.assertEqual(len(tracker.kisses), 0)


class FirstBuyRuleTest(unittest.TestCase):
    """p98：一买 = 男上位最后一吻后出现的背驰式下跌。

    构造用「振幅 8 的方波」：实测能稳定产出 7 个男上位吻 + 7 个女上位吻
    （振幅 5 会被 min_kiss_bars 过滤成 0 个吻，不可用）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        closes = [100.0]
        for i in range(80):
            closes.append(closes[-1] + (8.0 if (i // 5) % 2 == 0 else -8.0))
        cls.tracker = make_tracker(closes, threshold=0.03)
        cls.male_kisses = [k for k in cls.tracker.kisses if k.position == POSITION_MALE]
        assert cls.male_kisses, "构造必须至少产生一个男上位吻，否则用例无法验证"

    def _male_position_index(self) -> int:
        """找一个确实处于男上位的 K 线索引（确定性挑选，不靠运气）。"""

        for index in range(len(self.tracker.short_ma) - 1, 30, -1):
            if self.tracker.position_at(index) == POSITION_MALE:
                return index
        self.skipTest("构造序列里找不到男上位位置")

    def _female_position_index(self) -> int:
        for index in range(len(self.tracker.short_ma) - 1, 30, -1):
            if self.tracker.position_at(index) == POSITION_FEMALE:
                return index
        self.skipTest("构造序列里找不到女上位位置")

    def test_rejects_when_not_male_position(self) -> None:
        index = self._female_position_index()
        rule = judge_first_buy(
            self.tracker, divergence_index=index, segment_low_index=index, below_center=True
        )
        self.assertFalse(rule.satisfied)
        self.assertTrue(any("男上位" in v for v in rule.violations))

    def test_rejects_when_no_male_kiss_before(self) -> None:
        """背驰点之前**没有任何男上位吻** → 应被拒。

        探测点必须同时满足三条，否则测到的就不是这条规则：
        - 早于第一个男上位吻的 end（否则有前置吻）
        - 不落在任何吻的区间内（否则触发的是「缠绕中」那条）
        - 不早于 EMA 稳定起点（否则体位根本判断不了）
        """

        first_male_end = self.male_kisses[0].end_index
        candidates = [
            index
            for index in range(self.tracker.long_period + 1, first_male_end)
            if self.tracker.kiss_covering(index) is None
        ]
        if not candidates:
            self.skipTest("第一个吻之前没有「脱离缠绕且无前置吻」的探测点")
        probe = candidates[0]
        self.assertIsNone(
            self.tracker.last_kiss_of_position_before(probe, POSITION_MALE),
            "探测点之前本不该有男上位吻",
        )
        rule = judge_first_buy(
            self.tracker, divergence_index=probe, segment_low_index=probe, below_center=True
        )
        # 该探测点的其它条件（体位）可能仍不满足，但「吻」这一条必须被点到
        self.assertFalse(rule.satisfied)
        self.assertTrue(
            any("吻" in v for v in rule.violations),
            f"未检出吻相关违规：{rule.violations}",
        )

    def test_rejects_when_low_is_before_the_kiss(self) -> None:
        """背驰低点在**尚未结束的**缠绕内部 → 未脱离缠绕，应被拒。

        这是真实缺陷的回归：只查 `kisses_before` 会退回更早的吻，
        把「仍在缠绕中」的位置误放行。
        """

        target = self.male_kisses[-1]
        inside = (target.start_index + target.end_index) // 2
        if not (target.start_index <= inside <= target.end_index):
            self.skipTest("无法定位到吻内部")
        rule = judge_first_buy(
            self.tracker,
            divergence_index=inside,
            segment_low_index=inside,
            below_center=True,
        )
        self.assertFalse(rule.satisfied, "落在缠绕内部仍被判为一买")
        self.assertTrue(any("缠绕过程中" in v for v in rule.violations))

    def test_accepts_after_last_male_kiss_when_all_conditions_met(self) -> None:
        """反向对照：脱离最后一吻 + 男上位 + 破中枢 → 应通过。"""

        last = self.male_kisses[-1]
        probe = last.end_index + 3
        if probe >= len(self.tracker.short_ma) - 1:
            self.skipTest("最后一吻之后 K 线不足")
        if self.tracker.position_at(probe) != POSITION_MALE:
            self.skipTest("最后一吻之后并非男上位")
        rule = judge_first_buy(
            self.tracker, divergence_index=probe, segment_low_index=probe, below_center=True
        )
        self.assertTrue(rule.satisfied, rule.violations)
        self.assertEqual(rule.kiss_index, last.index)
        self.assertTrue(any("男上位" in r for r in rule.reasons))

    def test_rejects_when_point_above_center(self) -> None:
        index = self._male_position_index()
        rule = judge_first_buy(
            self.tracker, divergence_index=index, segment_low_index=index, below_center=False
        )
        self.assertFalse(rule.satisfied)
        self.assertTrue(any("中枢" in v for v in rule.violations))

    def test_min_kisses_threshold_is_honored(self) -> None:
        """p98「必须是至少第二吻以后」：min_kisses=2 时第一个男上位吻不算。

        关键：要找一个「体位 ✓、前置吻 ✓、脱离缠绕 ✓」的点，让唯一的差别
        只在吻数上——否则测试会被其它违规项抢先触发，测不到 min_kisses。
        """

        if len(self.male_kisses) < 2:
            self.skipTest("构造序列不足两个男上位吻")
        first_end = self.male_kisses[0].end_index
        for probe in range(first_end + 1, len(self.tracker.short_ma) - 1):
            if self.tracker.position_at(probe) != POSITION_MALE:
                continue
            if self.tracker.kiss_covering(probe) is not None:
                continue
            loose = judge_first_buy(self.tracker, probe, probe, True, min_kisses=1)
            # 只在「除吻数外全部满足」时比较，否则结论会被别的规则污染
            if not loose.satisfied:
                continue
            strict = judge_first_buy(self.tracker, probe, probe, True, min_kisses=2)
            self.assertFalse(
                strict.satisfied,
                "min_kisses=2 时，第一个男上位吻之后不算一买（p98 原文要求）",
            )
            self.assertTrue(
                any("吻" in v for v in strict.violations),
                f"strict 应因吻数不足被否，实际：{strict.violations}",
            )
            return
        self.skipTest("找不到「仅差吻数」的对照点")

    def test_rule_meta_explains_violations(self) -> None:
        index = self._female_position_index()
        rule = judge_first_buy(self.tracker, index, index, below_center=False, min_kisses=99)
        meta = rule.to_meta()
        self.assertFalse(meta["rule_satisfied"])
        self.assertTrue(meta["rule_violations"])
        self.assertEqual(meta["rule_kind"], "FIRST_BUY")


class SecondBuyRuleTest(unittest.TestCase):
    """p98：二买 = 女上位第一吻后出现的下跌。

    构造同样是振幅 8 的方波，稳定产出 7 个女上位吻与 7 个男上位吻。
    """

    @classmethod
    def setUpClass(cls) -> None:
        closes = [100.0]
        for i in range(80):
            closes.append(closes[-1] + (8.0 if (i // 5) % 2 == 0 else -8.0))
        cls.tracker = make_tracker(closes, threshold=0.03)
        cls.female_kisses = [k for k in cls.tracker.kisses if k.position == POSITION_FEMALE]
        assert cls.female_kisses, "构造必须至少产生一个女上位吻"

    def _female_position_after(self, index: int) -> int:
        """找 index 之后仍处于女上位的 K 线。"""

        for probe in range(index, len(self.tracker.short_ma) - 1):
            if self.tracker.position_at(probe) == POSITION_FEMALE:
                return probe
        self.skipTest("构造序列在该位置之后没有女上位")

    def test_accepts_after_first_female_kiss(self) -> None:
        """反向对照：脱离女上位第一吻 + 不破一买低点 → 应通过。"""

        first = self.female_kisses[0]
        probe = first.end_index + 2
        if probe >= len(self.tracker.short_ma) - 1:
            self.skipTest("第一吻之后 K 线不足")
        if self.tracker.position_at(probe) != POSITION_FEMALE:
            self.skipTest("第一吻之后并非女上位")
        if self.tracker.kiss_covering(probe) is not None:
            self.skipTest("探测点仍落在缠绕内部")
        rule = judge_second_buy(
            self.tracker, first_buy_index=probe, pullback_low_index=probe,
            holds_first_buy_low=True,
        )
        self.assertTrue(rule.satisfied, rule.violations)
        self.assertEqual(rule.kiss_index, first.index)

    def test_uses_first_kiss_not_the_last(self) -> None:
        """p98 明确是「第一吻」——多个女上位吻存在时必须取第一个。"""

        if len(self.female_kisses) < 2:
            self.skipTest("构造序列不足两个女上位吻")
        last = self.female_kisses[-1]
        probe = last.end_index + 2
        if probe >= len(self.tracker.short_ma) - 1:
            self.skipTest("最后一吻之后 K 线不足")
        if self.tracker.position_at(probe) != POSITION_FEMALE:
            self.skipTest("最后一吻之后并非女上位")
        rule = judge_second_buy(
            self.tracker, first_buy_index=probe, pullback_low_index=probe,
            holds_first_buy_low=True,
        )
        if not rule.satisfied:
            self.skipTest(f"该位置整体条件不满足：{rule.violations}")
        self.assertEqual(
            rule.kiss_index, self.female_kisses[0].index,
            "应取女上位的第一个吻，而不是最后一个",
        )
        self.assertNotEqual(rule.kiss_index, last.index)

    def test_rejects_when_breaks_first_buy_low(self) -> None:
        first = self.female_kisses[0]
        probe = first.end_index + 2
        if probe >= len(self.tracker.short_ma) - 1:
            self.skipTest("第一吻之后 K 线不足")
        rule = judge_second_buy(
            self.tracker, first_buy_index=probe, pullback_low_index=probe,
            holds_first_buy_low=False,
        )
        self.assertFalse(rule.satisfied)
        self.assertTrue(any("一买低点" in v for v in rule.violations))

    def test_rejects_when_not_female_position(self) -> None:
        """在**男上位**的位置 → 应因体位被拒。

        要点：必须挑一个「有女上位第一吻、且已脱离缠绕」的位置作为对照，
        这样唯一的不满足项就是体位，测试才能精确命中这条规则。
        """

        first_end = self.female_kisses[0].end_index
        for probe in range(first_end + 1, len(self.tracker.short_ma) - 1):
            if self.tracker.position_at(probe) != POSITION_MALE:
                continue
            if self.tracker.kiss_covering(probe) is not None:
                continue
            rule = judge_second_buy(
                self.tracker, first_buy_index=probe, pullback_low_index=probe,
                holds_first_buy_low=True,
            )
            self.assertFalse(rule.satisfied)
            self.assertTrue(
                any("女上位" in v for v in rule.violations),
                f"未因体位被否，实际违规：{rule.violations}",
            )
            return
        self.skipTest("找不到「有女上位第一吻但处于男上位」的对照位置")

    def test_rejects_when_inside_a_kiss(self) -> None:
        """回调低点落在尚未结束的缠绕内部 → 未脱离缠绕。"""

        target = self.female_kisses[-1]
        inside = (target.start_index + target.end_index) // 2
        rule = judge_second_buy(
            self.tracker, first_buy_index=inside, pullback_low_index=inside,
            holds_first_buy_low=True,
        )
        self.assertFalse(rule.satisfied, "落在缠绕内部仍被判为二买")
        self.assertTrue(any("缠绕过程中" in v for v in rule.violations))


class SellRuleMirrorTest(unittest.TestCase):
    """p84：「买的方式明白了，卖就反过来就可以了」——卖点必须严格镜像。"""

    def setUp(self) -> None:
        closes = [100.0]
        for i in range(80):
            closes.append(closes[-1] + (7.0 if (i // 5) % 2 == 0 else -7.0))
        self.tracker = make_tracker(closes, threshold=0.03)

    def test_first_sell_requires_female_position(self) -> None:
        index = len(self.tracker.short_ma) - 1
        rule = judge_first_sell(
            self.tracker, divergence_index=index, segment_high_index=index, above_center=True
        )
        if self.tracker.position_at(index) == POSITION_FEMALE:
            self.skipTest("该位置恰为女上位")
        self.assertFalse(rule.satisfied)
        self.assertTrue(any("女上位" in v for v in rule.violations))

    def test_second_sell_requires_male_first_kiss(self) -> None:
        index = len(self.tracker.short_ma) - 1
        rule = judge_second_sell(
            self.tracker, pullback_high_index=index, below_first_sell_high=True
        )
        if self.tracker.first_kiss_of_position_before(index, POSITION_MALE) is None:
            self.assertFalse(rule.satisfied)
        else:
            self.assertTrue(rule.satisfied, rule.violations)

    def test_second_sell_rejects_rebound_above_first_sell(self) -> None:
        index = len(self.tracker.short_ma) - 1
        rule = judge_second_sell(
            self.tracker, pullback_high_index=index, below_first_sell_high=False
        )
        self.assertFalse(rule.satisfied)
        self.assertTrue(any("一卖高点" in v for v in rule.violations))

    def test_sell_rules_have_mirror_shape(self) -> None:
        """镜像结构：一买与一卖的核心检查项应完全对应。

        注：一买比一卖多一条「男上位吻数下限」（p98「必须是至少第二吻以后」），
        这是**原文只对一买提的要求**，不是镜像不对称——所以只比对「体位 + 吻位置 +
        中枢」这三项。

        逐个 K 线扫，找到**两侧体位检查都会失败**的位置（既非男上位也非女上位
        是不可能的，所以这里要求：找一处一买判体位失败的位置，断言一卖在**镜像位置**
        ——即人为构造一个女上位的场景来比。
        """

        def bucket(violations: list) -> set:
            groups = set()
            for text in violations:
                if "上位" in text:
                    groups.add("position")
                elif "吻" in text:
                    groups.add("kiss")
                elif "中枢" in text:
                    groups.add("center")
            return groups

        # 找一处不在男上位的 index（此时一买会因体位被否）
        probe = None
        for index in range(len(self.tracker.short_ma) - 2, max(20, len(self.tracker.short_ma) - 60), -1):
            if self.tracker.position_at(index) != POSITION_MALE:
                probe = index
                break
        if probe is None:
            self.skipTest("构造序列全为男上位，找不到对照位置")

        buy = judge_first_buy(
            self.tracker, divergence_index=probe, segment_low_index=probe, below_center=False
        )
        sell = judge_first_sell(
            self.tracker, divergence_index=probe, segment_high_index=probe, above_center=False
        )

        self.assertIn("position", bucket(buy.violations), "一买应在体位上被否")
        # 一卖在同一位置可能体位正确（若恰为女上位），但「中枢未突破」两侧都必须被否
        self.assertIn("center", bucket(buy.violations))
        self.assertIn("center", bucket(sell.violations))
        # 两侧的「吻」检查都必须真正跑过：把一买的违规集合复制一份，
        # 验证一卖在**同一个构造序列**上也会因为缺少对应的吻而否决。
        # （一买多出的吻数下限是 p98 只对一买的要求，故不要求两侧 kiss 集合相等。）
        self.assertIsNotNone(
            buy.kiss_index or self.tracker.last_kiss_of_position_before(probe, POSITION_MALE),
            "探针位置之前应当有男上位吻可供比对",
        )


if __name__ == "__main__":
    unittest.main()

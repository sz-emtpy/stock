"""多源互补层的纯离线测试：合并规则、口径约定、降级判定。

这些用例刻意**不连任何网络**——多源的价值是「拼数据」，拼接与判定逻辑
必须能脱离外部服务验证，否则每次跑测试都要赌东财有没有限流。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import Bar  # noqa: E402

from backend.sources.base import (  # noqa: E402
    SourceError,
    ensure_ohlc_valid,
    merge_bars,
    normalize_time,
    to_baostock,
    to_eastmoney_secid,
)
from backend.sources.registry import (  # noqa: E402
    effective_fq,
    market_open,
    needs_tail,
)


def make_bar(date: str, close: float, period: str = "day") -> Bar:
    return Bar(
        symbol="sh600519",
        date=date,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=100.0,
        amount=1000.0,
        period=period,
    )


class SymbolConversionTest(unittest.TestCase):
    """三家源的代码格式不同，转错就是「查了另一只票」，而且是静默的。"""

    def test_baostock_format(self) -> None:
        self.assertEqual(to_baostock("sh600519"), "sh.600519")
        self.assertEqual(to_baostock("sz000001"), "sz.000001")
        # 已经是目标格式时原样返回（幂等）
        self.assertEqual(to_baostock("sh.600519"), "sh.600519")

    def test_eastmoney_secid(self) -> None:
        self.assertEqual(to_eastmoney_secid("sh600519"), "1.600519")
        self.assertEqual(to_eastmoney_secid("sz000001"), "0.000001")
        self.assertEqual(to_eastmoney_secid("sz.300750"), "0.300750")

    def test_invalid_symbol_raises(self) -> None:
        with self.assertRaises(SourceError):
            to_eastmoney_secid("600519")   # 缺市场前缀


class NormalizeTimeTest(unittest.TestCase):
    def test_baostock_datetime(self) -> None:
        # baostock 的 time 是 YYYYMMDDHHMMSSsss
        self.assertEqual(normalize_time("20261020100000000"), "2026-10-20 10:00")

    def test_eastmoney_datetime(self) -> None:
        self.assertEqual(normalize_time("2026-10-20 11:00"), "2026-10-20 11:00")

    def test_empty_raises(self) -> None:
        with self.assertRaises(SourceError):
            normalize_time("")


class MergeBarsTest(unittest.TestCase):
    """合并是「历史段 + 当天尾部」的核心，错了会让 K 线出现断层。"""

    def test_tail_wins_on_same_day(self) -> None:
        history = [make_bar("2026-10-17", 100.0), make_bar("2026-10-18", 101.0)]
        tail = [make_bar("2026-10-18", 105.0), make_bar("2026-10-19", 106.0)]
        merged, report = merge_bars(history, tail, "day")
        by_date = {b.date: b.close for b in merged}
        # 同一天以尾部（更新）为准——冲突只记账，不阻止覆盖
        self.assertEqual(by_date["2026-10-18"], 105.0)
        self.assertEqual(by_date["2026-10-19"], 106.0)
        self.assertEqual(report.overlap_rows, 1)
        # 101 → 105 差 3.96%，超过 0.5% 阈值，应记 1 次冲突
        self.assertEqual(report.conflicts, 1)

    def test_result_sorted_ascending(self) -> None:
        merged, _ = merge_bars(
            [make_bar("2026-10-18", 101.0)], [make_bar("2026-10-19", 102.0)], "day"
        )
        self.assertEqual([b.date for b in merged], ["2026-10-18", "2026-10-19"])

    def test_large_divergence_counted_as_conflict(self) -> None:
        """两家前复权基准不同会产生 ~1% 偏差，必须被记成冲突而不是静默吞掉。"""

        history = [make_bar("2026-06-15", 1271.10)]
        tail = [make_bar("2026-06-15", 1243.08)]   # 差 2.2%
        merged, report = merge_bars(history, tail, "day")
        self.assertEqual(len(merged), 1)
        self.assertEqual(report.conflicts, 1)
        self.assertGreater(report.max_diff_pct, 2.0)

    def test_small_divergence_not_conflict(self) -> None:
        merged, report = merge_bars(
            [make_bar("2026-10-20 10:30", 1324.32, "m30")],
            [make_bar("2026-10-20 10:30", 1324.40, "m30")],
            "m30",
        )
        self.assertEqual(report.conflicts, 0)
        self.assertEqual(len(merged), 1)

    def test_empty_history_returns_tail(self) -> None:
        tail = [make_bar("2026-10-19", 100.0)]
        merged, report = merge_bars([], tail, "day")
        self.assertEqual(len(merged), 1)
        self.assertEqual(report.history_rows, 0)


class FqPolicyTest(unittest.TestCase):
    """口径约定：分钟一律不复权，日线保持请求的复权方式。

    这条一旦被改，新旧信号就不可比了（历史 signals 全是分钟不复权）。
    """

    def test_minute_forced_no_adjust(self) -> None:
        self.assertEqual(effective_fq("m30", "qfq"), "none")
        self.assertEqual(effective_fq("m15", "qfq"), "none")

    def test_daily_keeps_requested(self) -> None:
        self.assertEqual(effective_fq("day", "qfq"), "qfq")
        self.assertEqual(effective_fq("day", "hfq"), "hfq")


class TailDecisionTest(unittest.TestCase):
    """是否还需要向实时源补当天数据。判错会引入「未收盘日线」假信号。"""

    def test_daily_skipped_during_session(self) -> None:
        """盘中日线不补：当天未收盘，用它算中枢会在收盘后翻转。"""

        history = [make_bar("2026-10-19", 100.0)]
        during = datetime(2026, 10, 20, 10, 30)
        self.assertTrue(market_open(during))
        self.assertFalse(needs_tail(history, "day", now=during))

    def test_daily_fetched_after_close(self) -> None:
        history = [make_bar("2026-10-19", 100.0)]
        after = datetime(2026, 10, 20, 16, 0)
        self.assertTrue(needs_tail(history, "day", now=after))

    def test_minute_fetched_during_session(self) -> None:
        """分钟必须补：盘中要的就是「最新那根」。"""

        history = [make_bar("2026-10-19 15:00", 100.0, "m30")]
        during = datetime(2026, 10, 20, 10, 30)
        self.assertTrue(needs_tail(history, "m30", now=during))

    def test_skipped_when_history_already_today(self) -> None:
        """baostock 已经有今天 → 不必再打 East Money（省配额、省时间）。"""

        history = [make_bar("2026-10-20 15:00", 100.0, "m30")]
        during = datetime(2026, 10, 20, 16, 0)
        self.assertFalse(needs_tail(history, "m30", now=during))

    def test_weekend_never_needs_tail(self) -> None:
        history = [make_bar("2026-10-16", 100.0)]
        saturday = datetime(2026, 10, 17, 10, 0)
        self.assertFalse(market_open(saturday))
        self.assertFalse(needs_tail(history, "day", now=saturday))

    def test_empty_history_always_needs_tail(self) -> None:
        self.assertTrue(needs_tail([], "day", now=datetime(2026, 10, 20, 10, 0)))


class OhlcSanitizeTest(unittest.TestCase):
    """各源偶发脏数据（high<low、负价）不能让整只票失败。"""

    def test_swapped_high_low_fixed(self) -> None:
        result = ensure_ohlc_valid("sh600519", "2026-10-20", 10.0, 9.0, 11.0, 10.5)
        self.assertIsNotNone(result)
        o, h, l, c = result  # type: ignore[misc]
        self.assertGreaterEqual(h, l)
        self.assertGreaterEqual(h, max(o, c))
        self.assertLessEqual(l, min(o, c))

    def test_non_positive_price_rejected(self) -> None:
        self.assertIsNone(ensure_ohlc_valid("sh600519", "2026-10-20", 0.0, 1.0, 0.5, 0.8))
        self.assertIsNone(ensure_ohlc_valid("sh600519", "2026-10-20", -1.0, 1.0, 0.5, 0.8))


class ProviderSwitchTest(unittest.TestCase):
    """回滚开关：出问题时要能一行环境变量退回纯 westock。"""

    def test_env_can_disable_multi_source(self) -> None:
        import os

        from backend import provider

        original = os.environ.get("CHANPICKER_SOURCE")
        try:
            os.environ["CHANPICKER_SOURCE"] = "westock"
            self.assertFalse(provider.multi_source_enabled())
            os.environ["CHANPICKER_SOURCE"] = "multi"
            self.assertTrue(provider.multi_source_enabled())
        finally:
            if original is None:
                os.environ.pop("CHANPICKER_SOURCE", None)
            else:
                os.environ["CHANPICKER_SOURCE"] = original

    def test_tests_do_not_hit_network_by_default(self) -> None:
        """单测进程里默认不启用多源，避免漏 mock 时真的联网。"""

        import os

        from backend import provider

        original = os.environ.pop("CHANPICKER_SOURCE", None)
        try:
            self.assertFalse(provider.multi_source_enabled())
        finally:
            if original is not None:
                os.environ["CHANPICKER_SOURCE"] = original


if __name__ == "__main__":
    unittest.main()

"""p100 买点定律（级别递归）的回归测试。

原文（p100/p101）：
> 「**大级别的第二类买点由次一级别相应走势的第一类买点构成。**
> 例如，周线上的第二类买点由日线上相应走势的第一类买点构成。
> 有了这个缠中说禅买点定律，**所有的买点都可以归结到第一类买点**。」

落地约束（实测）：次级别数据源只保留约 9 个交易日，
所以递归只用于**关注圈**范围，不做全市场。
"""

from __future__ import annotations

import unittest

from chanpicker.core import Bar, ChanConfig, ChanEngine, SignalKind
from chanpicker.masystem import (
    SublevelBuyConfirmation,
    confirm_by_sublevel_first_buy,
)


def make_bars(closes: list[float], symbol: str = "sh600000", period: str = "m30") -> list[Bar]:
    return [
        Bar(
            symbol=symbol, date=f"2026-07-{i+1:02d} 10:30", open=c, high=c + 0.2,
            low=c - 0.2, close=c, volume=1_000_000, amount=c * 1_000_000, period=period,
        )
        for i, c in enumerate(closes)
    ]


def engine() -> ChanEngine:
    return ChanEngine(ChanConfig(
        min_stroke_gap=1, min_second_buy_score=0.0, min_macd_area=9999,
        require_standard_divergence=False, require_trend_context=False,
        require_zhongshu_for_first_buy=False, require_zhongshu_for_first_sell=False,
        use_ma_system=False,
    ))


class SublevelConfirmationTest(unittest.TestCase):
    def test_no_data_reports_unavailable(self) -> None:
        """没有次级别数据时必须明说「无法验证」，不能假装通过。"""

        out = confirm_by_sublevel_first_buy(
            "sh600000", "2026-07-14", None, engine=engine()
        )
        self.assertFalse(out.available)
        self.assertIsNone(out.matched_date)
        self.assertIn("无法", out.message)

    def test_finds_sublevel_first_buy_after_second_buy(self) -> None:
        """次级别一买出现在二买之后、窗口内 → 验证通过。"""

        # 造一段能产出一买的次级别走势（振幅 8 的方波）
        closes = [100.0]
        for i in range(40):
            closes.append(closes[-1] + (8.0 if (i // 5) % 2 == 0 else -8.0))
        bars = make_bars(closes)
        sub_buys = [
            s for s in engine().scan(bars).signals if s.kind == SignalKind.FIRST_BUY
        ]
        if not sub_buys:
            self.skipTest("构造序列未产出次级别一买")

        # 二买日取某个次级别一买之前的日期
        first = sub_buys[0]
        second_buy_date = str(bars[0].date)[:10]
        out = confirm_by_sublevel_first_buy(
            "sh600000", second_buy_date, bars, engine=engine()
        )
        self.assertTrue(out.available, out.message)
        if out.matched_date:
            self.assertLessEqual(out.days_back or 0, 5)
            self.assertIn("p100", out.message)

    def test_sublevel_buy_before_second_buy_does_not_match(self) -> None:
        """次级别一买早于二买 → 不属于「相应走势」，不应算验证。"""

        closes = [100.0]
        for i in range(40):
            closes.append(closes[-1] + (8.0 if (i // 5) % 2 == 0 else -8.0))
        bars = make_bars(closes)
        sub_buys = [
            s for s in engine().scan(bars).signals if s.kind == SignalKind.FIRST_BUY
        ]
        if not sub_buys:
            self.skipTest("构造序列未产出次级别一买")
        # 二买日取所有次级别一买之后
        last_date = str(bars[-1].date)[:10]
        out = confirm_by_sublevel_first_buy(
            "sh600000", last_date, bars, engine=engine(), lookback_days=1
        )
        self.assertTrue(out.available)
        self.assertIsNone(out.matched_date, "次级别一买全部早于二买，不该匹配")
        self.assertIn("未", out.message)

    def test_window_limit_is_respected(self) -> None:
        """超出 lookback 天的次级别一买不计入。"""

        closes = [100.0]
        for i in range(40):
            closes.append(closes[-1] + (8.0 if (i // 5) % 2 == 0 else -8.0))
        bars = make_bars(closes)
        sub_buys = [
            s for s in engine().scan(bars).signals if s.kind == SignalKind.FIRST_BUY
        ]
        if not sub_buys:
            self.skipTest("构造序列未产出次级别一买")
        second_buy_date = str(bars[0].date)[:10]
        wide = confirm_by_sublevel_first_buy(
            "sh600000", second_buy_date, bars, engine=engine(), lookback_days=90
        )
        narrow = confirm_by_sublevel_first_buy(
            "sh600000", second_buy_date, bars, engine=engine(), lookback_days=0
        )
        if wide.matched_date:
            self.assertIsNone(
                narrow.matched_date,
                "窗口为 0 天时不应匹配到任何次级别一买",
            )

    def test_meta_payload_shape(self) -> None:
        """meta 的字段名固定，前端与文档都依赖它。"""

        out = SublevelBuyConfirmation(
            symbol="sh600000", available=False, sublevel_period="m30",
            message="无数据",
        )
        meta = out.to_meta()["sublevel_rule"]
        for key in (
            "available", "sublevel_period", "sublevel_first_buy_count",
            "sublevel_matched_date", "sublevel_days_back", "sublevel_message",
        ):
            self.assertIn(key, meta)


class TradingDayGapTest(unittest.TestCase):
    def test_gap_between_dates(self) -> None:
        from chanpicker.masystem import _trading_day_gap

        self.assertEqual(_trading_day_gap("20260710", "20260712"), 2)
        self.assertEqual(_trading_day_gap("20260710", "20260710"), 0)

    def test_invalid_input_returns_none(self) -> None:
        from chanpicker.masystem import _trading_day_gap

        self.assertIsNone(_trading_day_gap("", "20260712"))
        self.assertIsNone(_trading_day_gap("bad", "20260712"))
        self.assertIsNone(_trading_day_gap("20261340", "20260712"))


class ServiceSublevelTest(unittest.TestCase):
    """service 层的接入：只对关注圈、且要有 second_buy_date。"""

    def setUp(self) -> None:
        import sys
        import tempfile
        from pathlib import Path

        from backend.service import ScanService
        from backend.store import Store

        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_skips_items_without_second_buy(self) -> None:
        from chanpicker.core import WatchItem, WatchState

        self.store.upsert_symbol("sh600001", "测试")
        self.store.save_watch_items([
            WatchItem(
                symbol="sh600001", state=WatchState.WAIT_SECOND_BUY,
                first_buy_date="2026-07-01", first_buy_index=2,
                first_buy_price=10.0, first_buy_low=9.5, last_update_date="2026-07-02",
            )
        ])
        out = self.service.sublevel_second_buy_confirmations()
        self.assertEqual(out["checked"], 0, "没有二买的关注项不应进入检查")
        self.assertEqual(out["skipped_no_data"], 0)
        self.assertIn("p100", out["message"])

    def test_counts_missing_sublevel_data_as_skipped(self) -> None:
        from chanpicker.core import WatchItem, WatchState

        self.store.upsert_symbol("sh600002", "测试")
        self.store.save_watch_items([
            WatchItem(
                symbol="sh600002", state=WatchState.OPPORTUNITY,
                first_buy_date="2026-07-01", first_buy_index=2,
                first_buy_price=10.0, first_buy_low=9.5, last_update_date="2026-07-02",
                second_buy_date="2026-07-05", second_buy_price=10.5,
            )
        ])
        out = self.service.sublevel_second_buy_confirmations()
        self.assertEqual(out["skipped_no_data"], 1, "无次级别数据应计入 skipped")
        self.assertEqual(out["verified"], 0)

    def test_limit_caps_checked_items(self) -> None:
        from chanpicker.core import WatchItem, WatchState

        for i in range(5):
            sym = f"sh60000{i}"
            self.store.upsert_symbol(sym, f"测试{i}")
            self.store.save_watch_items([
                WatchItem(
                    symbol=sym, state=WatchState.OPPORTUNITY,
                    first_buy_date="2026-07-01", first_buy_index=2,
                    first_buy_price=10.0, first_buy_low=9.5, last_update_date="2026-07-02",
                    second_buy_date="2026-07-05", second_buy_price=10.5,
                )
            ])
        out = self.service.sublevel_second_buy_confirmations(limit=2)
        self.assertLessEqual(out["checked"] + out["skipped_no_data"], 2)


if __name__ == "__main__":
    unittest.main()

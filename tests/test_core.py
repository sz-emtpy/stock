from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chanpicker import (
    Bar,
    ChanConfig,
    ChanEngine,
    SignalKind,
    WatchItem,
    WatchState,
    WatchlistManager,
    load_bars_csv,
)


def make_bars(prices: list[float], symbol: str = "sh600000") -> list[Bar]:
    bars: list[Bar] = []
    for offset, price in enumerate(prices, start=1):
        bars.append(
            Bar(
                symbol=symbol,
                date=f"2024-01-{offset:02d}",
                open=price,
                high=price + 0.4,
                low=price - 0.4,
                close=price,
                volume=1_000_000,
                amount=price * 1_000_000,
            )
        )
    return bars


def loose_test_config() -> ChanConfig:
    return ChanConfig(
        min_stroke_gap=1,
        min_divergence_score=0.05,
        min_second_buy_score=0.0,
        min_macd_area=9999,
        require_zhongshu_for_first_buy=False,
        max_wait_bars=30,
        cooldown_bars=60,
    )


class ChanEngineTest(unittest.TestCase):
    def test_scan_detects_first_buy_and_second_buy(self) -> None:
        prices = [100, 80, 95, 72, 92, 66, 88, 64, 84, 70, 92, 78, 96]
        result = ChanEngine(loose_test_config()).scan(make_bars(prices))

        kinds = [signal.kind for signal in result.signals]
        self.assertIn(SignalKind.FIRST_BUY, kinds)
        self.assertIn(SignalKind.SECOND_BUY, kinds)

        first_buy = next(signal for signal in result.signals if signal.kind == SignalKind.FIRST_BUY)
        second_buy = next(signal for signal in result.signals if signal.kind == SignalKind.SECOND_BUY)
        self.assertGreater(second_buy.bar_index, first_buy.bar_index)
        self.assertGreater(second_buy.meta["second_buy_score"], 0)
        self.assertEqual(first_buy.message, "背驰后一买成立，纳入关注圈并等待二买确认")

    def test_scan_detects_third_buy_after_center_breakout(self) -> None:
        # 100/120/110/125/112 形成中枢 [112,120]；140 向上离开；130 回调不破 ZG
        prices = [100, 120, 110, 125, 112, 140, 130, 145]
        result = ChanEngine(loose_test_config()).scan(make_bars(prices))

        third = [signal for signal in result.signals if signal.kind == SignalKind.THIRD_BUY]
        self.assertTrue(third, "应识别出三买信号")
        signal = third[-1]
        self.assertGreater(signal.meta["third_buy_score"], 0)
        self.assertAlmostEqual(signal.anchor_price, 129.6, places=6)
        self.assertGreater(signal.anchor_price, signal.meta["zhongshu_zg"])
        # 确认日在回调底分型之后，避免未来函数
        self.assertGreater(signal.bar_index, 6)

    def test_third_buy_rejected_when_pullback_reenters_center(self) -> None:
        # 回调到 118，跌回中枢上沿 120 之内 → 三买不成立
        prices = [100, 120, 110, 125, 112, 140, 118, 145]
        result = ChanEngine(loose_test_config()).scan(make_bars(prices))

        kinds = [signal.kind for signal in result.signals]
        self.assertNotIn(SignalKind.THIRD_BUY, kinds)

    def test_default_requires_confirmation_after_bottom_fractal(self) -> None:
        prices = [100, 80, 95, 72, 92, 66, 88, 64]
        config = loose_test_config()
        config = ChanConfig(**{**config.__dict__, "require_zhongshu_for_first_buy": False})
        result = ChanEngine(config).scan(make_bars(prices))
        self.assertEqual(result.signals, [])

    def test_watchlist_moves_to_opportunity_when_second_buy_exists(self) -> None:
        prices = [100, 80, 95, 72, 92, 66, 88, 64, 84, 70, 92, 78, 96]
        bars = make_bars(prices)
        engine = ChanEngine(loose_test_config())
        result = engine.scan(bars)

        items = WatchlistManager(loose_test_config()).update(bars, result.signals)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, WatchState.OPPORTUNITY)
        self.assertIsNotNone(items[0].second_buy_date)
        self.assertEqual(items[0].exit_reason, "二买确认，机会触发")

    def test_watchlist_fails_when_close_breaks_first_buy_low(self) -> None:
        bars = make_bars([12, 11, 12, 11, 12, 11, 12, 9])
        item = WatchItem(
            symbol="sh600000",
            state=WatchState.WAIT_SECOND_BUY,
            first_buy_date="2024-01-03",
            first_buy_index=2,
            first_buy_price=12,
            first_buy_low=10,
            last_update_date="2024-01-07",
        )

        items = WatchlistManager(loose_test_config()).update(bars, [], existing=[item])

        self.assertEqual(items[0].state, WatchState.FAILED)
        self.assertEqual(items[0].exit_reason, "跌破一买低点，关注失效")

    def test_load_bars_csv_validates_required_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            path.write_text("date,open,high,low,close,volume\n2024-01-01,1,2,0.5,1.5,100\n", encoding="utf-8")
            bars = load_bars_csv(path, symbol="sz000001")

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "sz000001")
        self.assertEqual(bars[0].close, 1.5)


if __name__ == "__main__":
    unittest.main()

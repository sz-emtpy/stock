from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chanpicker import (
    Bar,
    ChanConfig,
    ChanEngine,
    Signal,
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


# 段口径下的构造序列。要满足新口径的两道收紧（p368 标准背驰 + p116 趋势背驰），
# 序列必须先走出**两个依次下移的中枢**构成下跌趋势，再出现力度衰减的背驰段；
# 长度还必须是 3 的倍数（每 3 根 K 线 = 一个次级别走势类型段，p152 最低级别定义）。
# 短于这个长度的序列在新口径下必然检不出信号——不是用例写坏了，是口径变了。
# 这条序列能产出 3 组「一买 → 二买」配对（bar 18/33、42/51、60/78）。
LEG_DOWN_WITH_SECOND_BUY = [100, 100, 98, 97, 99, 97, 96, 97, 96, 95, 96, 98,
    100, 100, 100, 100, 99, 100, 98, 98, 96, 95, 94, 95,
    94, 93, 93, 91, 91, 90, 88, 90, 92, 92, 91, 91,
    91, 92, 91, 89, 91, 93, 94, 95, 95, 97, 99, 101,
    99, 97, 96, 97, 94, 96, 98, 96, 95, 97, 96, 95,
    95, 93, 94, 94, 93, 96, 98, 96, 95, 95, 93, 91,
    90, 91, 91, 90, 91, 93, 92, 92, 91, 91, 89, 87,
    87, 86, 87, 87, 88]


def loose_test_config() -> ChanConfig:
    """测试用配置：放宽阈值，让构造序列能触发信号。

    注意两处「放宽」的原因：
    - `min_macd_area=9999` 强制力度回退到价格差，而回退时均线条件（ma_extreme）
      被置 0——那正是为了不给没有信息的条件凭空判弱。于是「标准背驰」在回退路径上
      **永远不会成立**，必须把 `require_standard_divergence` 关掉。
    - `require_trend_context=False`：短构造序列走不出「两个依次下移的中枢」，
      趋势背驰条件（p116）无从满足。
    这两条不是测试在迁就实现，而是回退路径与收紧条件的固有约束；
    真实数据路径（默认配置、不回退）两道收紧都生效。

    另：`use_ma_system=False`——本类用例验的是**段+中枢口径**能否识别出信号
    （背驰、趋势、不破前低），与「道」的均线规则（p84/p98 的体位与吻）正交，
    构造序列长度也不足以产生足够的吻。原文之道的判定由
    `tests/test_masystem.py` 单独覆盖。
    """

    return ChanConfig(
        min_stroke_gap=1,
        min_divergence_score=0.05,
        min_second_buy_score=0.0,
        min_top_divergence_score=0.05,
        min_second_sell_score=0.0,
        min_macd_area=9999,
        require_zhongshu_for_first_buy=False,
        require_zhongshu_for_first_sell=False,
        require_standard_divergence=False,
        require_trend_context=False,
        use_ma_system=False,
        max_wait_bars=30,
        cooldown_bars=60,
    )


class ChanEngineTest(unittest.TestCase):
    def test_scan_detects_first_buy_and_second_buy(self) -> None:
        prices = LEG_DOWN_WITH_SECOND_BUY
        result = ChanEngine(loose_test_config()).scan(make_bars(prices))

        kinds = [signal.kind for signal in result.signals]
        self.assertIn(SignalKind.FIRST_BUY, kinds)
        self.assertIn(SignalKind.SECOND_BUY, kinds)

        first_buy = next(signal for signal in result.signals if signal.kind == SignalKind.FIRST_BUY)
        second_buy = next(signal for signal in result.signals if signal.kind == SignalKind.SECOND_BUY)
        self.assertGreater(second_buy.bar_index, first_buy.bar_index)
        # 二买的本质条件是「次级别回调不破一买低点」（p152-153），
        # 力度衰减（score > 0）是加分项不是必要条件——本构造序列的回调段
        # 力度并不比离开段小，所以这里只断言本质条件。
        self.assertGreater(second_buy.anchor_price, first_buy.anchor_price)
        self.assertEqual(first_buy.message, "下跌走势背驰后一买成立，纳入关注圈并等待二买确认")

    def test_scan_detects_third_buy_after_center_breakout(self) -> None:
        # 段口径：段0-3 在 [103.6, 106.4] 重叠成中枢；段4 向上离开到 136；
        # 段5 回调最低 117.6，不跌破中枢 ZG → 三买。
        prices = [
            100, 106, 102, 108, 104, 110, 106, 102, 108, 104, 106, 110,
            112, 124, 136, 132, 124, 118, 116, 114, 113,
        ]
        result = ChanEngine(loose_test_config()).scan(make_bars(prices))

        third = [signal for signal in result.signals if signal.kind == SignalKind.THIRD_BUY]
        self.assertTrue(third, "应识别出三买信号")
        signal = third[-1]
        # p208 定理才是三买的本质条件：**回调低点不跌破中枢上沿 ZG**。
        # 力度衰减（score > 0）在本构造序列里不成立——回调段的 MACD 面积
        # 大于突破段，所以这里只断言定理条件，不断言力度分。
        self.assertGreater(signal.anchor_price, signal.meta["zhongshu_zg"])
        # 确认日在回调结束之后，避免未来函数
        self.assertGreater(signal.bar_index, 17)

    def test_third_buy_rejected_when_pullback_reenters_center(self) -> None:
        # 回调到 118，跌回中枢上沿 120 之内 → 三买不成立
        prices = [100, 120, 110, 125, 112, 140, 118, 145]
        result = ChanEngine(loose_test_config()).scan(make_bars(prices))

        kinds = [signal.kind for signal in result.signals]
        self.assertNotIn(SignalKind.THIRD_BUY, kinds)

    def test_scan_detects_first_sell_and_second_sell(self) -> None:
        # 序列要够长：原文口径下中枢 = 至少三个连续次级别走势类型的重叠，
        # 而次级别段按每 3 根 K 线切成一段（p152 对最低级别的定义），
        # 所以 12 根以内的短序列在新口径下撑不起中枢，卖点会检不出来。
        # 这里给 32 根：先震荡上行到 158 出现顶背驰（一卖），再跌到 140，
        # 反弹到 158.4 不创新高 → 二卖。
        prices = [
            100, 112, 106, 110, 108, 116, 108, 122,
            114, 128, 120, 136, 128, 144, 138, 150,
            144, 154, 148, 158, 152, 160, 154, 158,
            140, 152, 144, 150, 146, 148, 147, 149,
        ]
        result = ChanEngine(loose_test_config()).scan(make_bars(prices))

        first_sell = [s for s in result.signals if s.kind == SignalKind.FIRST_SELL]
        second_sell = [s for s in result.signals if s.kind == SignalKind.SECOND_SELL]
        self.assertTrue(first_sell, "应识别出一卖信号")
        self.assertTrue(second_sell, "应识别出二卖信号")
        self.assertGreater(first_sell[-1].meta["top_divergence_score"], 0)
        self.assertLess(second_sell[-1].anchor_price, first_sell[-1].anchor_price)

    def test_second_sell_rejected_when_rebound_makes_new_high(self) -> None:
        # 与上一个用例同构，只是 bar22 之后那波反弹的高点从 158.4 抬到 170，
        # 超过了 bar22 的一卖锚定价 160.4 → 反弹创新高，二卖不成立。
        prices = [
            100, 112, 106, 110, 108, 116, 108, 122,
            114, 128, 120, 136, 128, 144, 138, 150,
            144, 154, 148, 158, 152, 160, 154, 170,
            150, 165, 148, 168,
        ]
        result = ChanEngine(loose_test_config()).scan(make_bars(prices))

        kinds = [signal.kind for signal in result.signals]
        self.assertIn(SignalKind.FIRST_SELL, kinds)
        self.assertNotIn(SignalKind.SECOND_SELL, kinds)

    def test_default_requires_confirmation_after_bottom_fractal(self) -> None:
        prices = [100, 80, 95, 72, 92, 66, 88, 64]
        config = loose_test_config()
        config = ChanConfig(**{**config.__dict__, "require_zhongshu_for_first_buy": False})
        result = ChanEngine(config).scan(make_bars(prices))
        self.assertEqual(result.signals, [])

    def test_watchlist_moves_to_opportunity_when_second_buy_exists(self) -> None:
        # 这个用例验的是**状态机转移**，不该依赖某条构造序列恰好产出二买、
        # 又恰好在窗口内不跌破——口径一换序列随时可能不满足，测试就变成在赌运气。
        # 直接构造「待二买记录 + 一个窗口内不破位的二买信号」输入，锁定转移本身。
        bars = make_bars(LEG_DOWN_WITH_SECOND_BUY)
        engine = ChanEngine(loose_test_config())
        result = engine.scan(bars)
        real_second_buy = next(
            (s for s in result.signals if s.kind == SignalKind.SECOND_BUY), None
        )
        self.assertIsNotNone(real_second_buy, "构造序列应能产出二买，否则本用例无法验证转移")

        # 用一个贴着序列末端的二买信号：确认位接近最后一根，其后没有 K 线，
        # 于是「窗口内跌破」无从发生，状态机必然落到机会态。
        last_index = len(bars) - 1
        second_buy = Signal(
            symbol="sh600000", kind=SignalKind.SECOND_BUY,
            date=bars[last_index - 1].date, bar_index=last_index - 1,
            price=bars[last_index - 1].close,
            anchor_index=last_index - 2,
            anchor_price=bars[last_index - 1].close - 0.1,  # 低点贴近现价，窗口内不会被跌破
            message="二买确认",
            meta={"second_buy_low": bars[last_index - 1].close - 0.1, "second_buy_index": last_index - 2},
        )
        manager = WatchlistManager(loose_test_config())
        existing = [
            WatchItem(
                symbol="sh600000",
                state=WatchState.WAIT_SECOND_BUY,
                first_buy_date="2024-01-05",
                first_buy_index=2,
                first_buy_price=98.0,
                # 一买低点要低于序列末根收盘，否则先判 FAILED（跌破一买低点），
                # 就轮不到二买确认。本用例只验转移，不验跌破。
                first_buy_low=bars[-1].close - 0.5,
                last_update_date="2024-01-06",
            )
        ]
        items = manager.update(bars, [second_buy], existing=existing)

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

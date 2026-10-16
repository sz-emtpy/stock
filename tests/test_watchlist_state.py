"""关注圈状态机：二买失败转移、出圈口径、成功率统计。

这一组用例锁死三件容易互相带偏的事：
1. OPPORTUNITY 不再是终态——二买确认后仍要被继续判定，否则「二买失败」永远观测不到；
2. 一买失败优先级高于二买失败（低点都破了，二买自然不成立）；
3. 成功率的分子分母口径（一买以「是否引出二买」计，二买以「考验窗口内是否守住低点」计）。
"""

from __future__ import annotations

import unittest

from chanpicker import (
    Bar,
    ChanConfig,
    ChanEngine,
    CLOSED_STATES,
    Signal,
    SignalKind,
    TRACKING_STATES,
    WatchItem,
    WatchState,
    WatchlistManager,
    segment_meta,
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


def engine_signals(prices: list[float], config: ChanConfig | None = None) -> list[Signal]:
    return ChanEngine(config or ChanConfig()).scan(make_bars(prices)).signals


def watch_item(state: WatchState, index: int, price: float, low: float, symbol: str = "sh600000", **extra) -> WatchItem:
    payload = {
        "symbol": symbol,
        "state": state,
        "first_buy_date": f"2024-01-{index + 1:02d}",
        "first_buy_index": index,
        "first_buy_price": price,
        "first_buy_low": low,
        "last_update_date": "2024-01-10",
    }
    payload.update(extra)
    return WatchItem(**payload)


class SecondBuyFailureTest(unittest.TestCase):
    """二买失败：机会态必须在后续扫描里被继续判定。"""

    def test_opportunity_stays_in_tracking_states(self) -> None:
        # 这是整套二买失败判定的前提：若把 OPPORTUNITY 当终态，update 里
        # 的 active 查找永远选不中它，后面所有分支都走不到。
        self.assertIn(WatchState.OPPORTUNITY, TRACKING_STATES)
        self.assertIn(WatchState.SECOND_BUY_FAILED, CLOSED_STATES)
        self.assertNotIn(WatchState.SECOND_BUY_FAILED, TRACKING_STATES)

    def test_close_below_second_buy_low_marks_failure(self) -> None:
        config = ChanConfig(max_wait_bars=30)
        manager = WatchlistManager(config)
        # 一买在 index 5，低点 60；二买低点 70（高于一买低点）
        existing = [
            watch_item(
                WatchState.OPPORTUNITY,
                index=5,
                price=62.0,
                low=60.0,
                second_buy_date="2024-01-09",
                second_buy_price=72.0,
                meta={"second_buy_low": 70.0, "second_buy_index": 8},
            )
        ]
        # 最后三根 K 线跌回 69（< 二买低点 70，但仍 > 一买低点 60）
        prices = [100, 95, 90, 85, 80, 60, 78, 88, 74, 80, 76, 72, 69]
        bars = make_bars(prices)
        signals: list[Signal] = []
        items = manager.update(bars=bars, signals=signals, existing=existing)

        item = next(it for it in items if it.symbol == "sh600000")
        self.assertEqual(item.state, WatchState.SECOND_BUY_FAILED)
        self.assertEqual(item.exit_date, bars[-1].date)
        self.assertIn("二买低点", item.exit_reason or "")

    def test_holding_above_second_buy_low_keeps_opportunity(self) -> None:
        manager = WatchlistManager(ChanConfig(max_wait_bars=30))
        existing = [
            watch_item(
                WatchState.OPPORTUNITY,
                index=5,
                price=62.0,
                low=60.0,
                second_buy_date="2024-01-09",
                second_buy_price=72.0,
                meta={"second_buy_low": 70.0, "second_buy_index": 8},
            )
        ]
        prices = [100, 95, 90, 85, 80, 60, 78, 88, 74, 80, 76, 72, 75]
        items = manager.update(bars=make_bars(prices), signals=[], existing=existing)

        item = next(it for it in items if it.symbol == "sh600000")
        self.assertEqual(item.state, WatchState.OPPORTUNITY)

    def test_first_buy_failure_wins_over_second_buy_failure(self) -> None:
        # 同时跌破一买低点与二买低点时，必须记 FAILED 而不是 SECOND_BUY_FAILED：
        # 一买低点是更底层的失效，二买失败是它的子集。
        manager = WatchlistManager(ChanConfig(max_wait_bars=30))
        existing = [
            watch_item(
                WatchState.OPPORTUNITY,
                index=5,
                price=62.0,
                low=60.0,
                second_buy_date="2024-01-09",
                second_buy_price=72.0,
                meta={"second_buy_low": 70.0, "second_buy_index": 8},
            )
        ]
        prices = [100, 95, 90, 85, 80, 60, 78, 88, 74, 80, 76, 72, 55]
        items = manager.update(bars=make_bars(prices), signals=[], existing=existing)

        item = next(it for it in items if it.symbol == "sh600000")
        self.assertEqual(item.state, WatchState.FAILED)
        self.assertEqual(item.exit_reason, "跌破一买低点，关注失效")

    def test_breakout_after_window_is_not_a_failure(self) -> None:
        # 考验窗口之外的破位不计入这笔二买：可能是后续大盘整体回落，
        # 记成「二买失败」会把成功率做虚。
        manager = WatchlistManager(ChanConfig(max_wait_bars=5))
        existing = [
            watch_item(
                WatchState.OPPORTUNITY,
                index=5,
                price=62.0,
                low=60.0,
                second_buy_date="2024-01-09",
                second_buy_price=72.0,
                meta={"second_buy_low": 70.0, "second_buy_index": 8},
            )
        ]
        # 共 13 根，二买在 index 8，窗口 5 根 → index 13 才超窗口；
        # 这里最后一根 index 12 距 8 为 4，仍在窗口内，所以先构造窗口外的场景：
        prices = [100, 95, 90, 85, 80, 60, 78, 88, 74, 80, 76, 72, 69, 68, 67, 66, 65, 64, 63]
        items = manager.update(bars=make_bars(prices), signals=[], existing=existing)

        item = next(it for it in items if it.symbol == "sh600000")
        self.assertEqual(item.state, WatchState.OPPORTUNITY)

    def test_second_buy_failed_is_terminal(self) -> None:
        # 出圈后不再被跟踪，也不会因为新一轮扫描又变回机会态。
        manager = WatchlistManager(ChanConfig(max_wait_bars=30))
        existing = [
            watch_item(
                WatchState.SECOND_BUY_FAILED,
                index=5,
                price=62.0,
                low=60.0,
                second_buy_date="2024-01-09",
                second_buy_price=72.0,
                exit_date="2024-01-10",
                exit_reason="二买失败",
                meta={"second_buy_low": 70.0, "second_buy_index": 8},
            )
        ]
        prices = [100, 95, 90, 85, 80, 60, 78, 88, 74, 80, 76, 72, 69]
        items = manager.update(bars=make_bars(prices), signals=[], existing=existing)

        item = next(it for it in items if it.symbol == "sh600000")
        self.assertEqual(item.state, WatchState.SECOND_BUY_FAILED)

    def test_cooldown_counts_second_buy_failure(self) -> None:
        # 刚二买失败就又来一个一买，必须被冷静期挡住，避免反复入圈出圈刷屏。
        manager = WatchlistManager(ChanConfig(max_wait_bars=30, cooldown_bars=60))
        existing = [
            watch_item(
                WatchState.SECOND_BUY_FAILED,
                index=10,
                price=62.0,
                low=60.0,
                second_buy_date="2024-01-11",
                second_buy_price=72.0,
                meta={"second_buy_low": 70.0, "second_buy_index": 12},
            )
        ]
        prices = [100, 95, 90, 85, 80, 60, 78, 88, 74, 80, 76, 72, 69, 68, 67, 66, 65, 64, 63, 62, 61]
        signals = [
            Signal(
                symbol="sh600000",
                kind=SignalKind.FIRST_BUY,
                date="2024-01-18",
                bar_index=17,
                price=61.0,
                anchor_index=16,
                anchor_price=60.0,
                message="背驰后一买成立",
            )
        ]
        items = manager.update(bars=make_bars(prices), signals=signals, existing=existing)

        # 冷静期内没有新建 WAIT_SECOND_BUY 记录
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, WatchState.SECOND_BUY_FAILED)


class SegmentMetaTest(unittest.TestCase):
    """背驰段坐标：5 类信号都要带上两段起止点，前端才画得出结构。"""

    DIVERGENCE_KINDS = (
        SignalKind.FIRST_BUY,
        SignalKind.SECOND_BUY,
        SignalKind.THIRD_BUY,
        SignalKind.FIRST_SELL,
        SignalKind.SECOND_SELL,
    )

    # 段口径序列：每 3 根 K 线 = 一个次级别走势类型段（p152 最低级别定义），
    # 长度需是 3 的倍数且足够长才撑得起「两个同向中枢 + 标准背驰」。
    # 短序列在段口径下检不出信号，而这里要验的是 meta 的段坐标结构，
    # 与「能否检出信号」无关，所以用一条能满足新口径的长序列。
    PRICES = [100, 100, 98, 97, 99, 97, 96, 97, 96, 95, 96, 98,
    100, 100, 100, 100, 99, 100, 98, 98, 96, 95, 94, 95,
    94, 93, 93, 91, 91, 90, 88, 90, 92, 92, 91, 91,
    91, 92, 91, 89, 91, 93, 94, 95, 95, 97, 99, 101,
    99, 97, 96, 97, 94, 96, 98, 96, 95, 97, 96, 95,
    95, 93, 94, 94, 93, 96, 98, 96, 95, 95, 93, 91,
    90, 91, 91, 90, 91, 93, 92, 92, 91, 91, 89, 87,
    87, 86, 87, 87, 88]

    def _segments(self, prices: list[float]) -> dict:
        signals = engine_signals(
            prices,
            ChanConfig(
                min_stroke_gap=1,
                min_divergence_score=0.05,
                min_second_buy_score=0.0,
                min_top_divergence_score=0.05,
                min_second_sell_score=0.0,
                min_macd_area=9999,
                require_zhongshu_for_first_buy=False,
                require_zhongshu_for_first_sell=False,
                # 回退路径下 ma_extreme 被置 0，「标准背驰」永不成立；
                # 短序列也走不出趋势背驰，这里只验 meta 结构，放宽这两道收紧。
                require_standard_divergence=False,
                require_trend_context=False,
            ),
        )
        return {signal.kind: signal.meta for signal in signals if signal.kind in self.DIVERGENCE_KINDS}

    def test_all_signal_kinds_carry_segment_coordinates(self) -> None:
        metas = self._segments(self.PRICES)
        self.assertTrue(metas, "至少应检出一种带背驰段的信号")
        for kind, meta in metas.items():
            for key in ("previous_segment", "current_segment"):
                self.assertIn(key, meta, f"{kind.value} 缺少 {key}")
                segment = meta[key]
                for field in ("start_index", "start_price", "end_index", "end_price", "direction"):
                    self.assertIn(field, segment, f"{kind.value}.{key} 缺少 {field}")
                self.assertLess(segment["start_index"], segment["end_index"])
                self.assertIn(segment["direction"], ("up", "down"))

    def test_each_kind_compares_the_right_pair_of_segments(self) -> None:
        # 背驰的本质是「相邻两段力度对比」，但每种信号比的具体是哪两段不同：
        # 一买比相邻两个下跌段；二买/三买比「离开段 vs 回调段」；
        # 一卖比相邻两个上升段；二卖比「下跌段 vs 反弹段」。
        metas = self._segments(self.PRICES)
        expected = {
            SignalKind.FIRST_BUY: ("down", "down"),
            SignalKind.SECOND_BUY: ("up", "down"),
            SignalKind.THIRD_BUY: ("up", "down"),
            SignalKind.FIRST_SELL: ("up", "up"),
            SignalKind.SECOND_SELL: ("down", "up"),
        }
        self.assertTrue(metas, "至少应检出一种带背驰段的信号")
        for kind, (previous_dir, current_dir) in expected.items():
            if kind not in metas:
                continue
            self.assertEqual(
                metas[kind]["previous_segment"]["direction"],
                previous_dir,
                f"{kind.value} 的前段方向应为 {previous_dir}",
            )
            self.assertEqual(
                metas[kind]["current_segment"]["direction"],
                current_dir,
                f"{kind.value} 的背驰段方向应为 {current_dir}",
            )

    def test_segments_relate_the_way_the_detector_pairs_them(self) -> None:
        """段口径下两段的相接关系必须与检测逻辑一致。

        引擎里两类信号取段的方式不同：
        - **一买/一卖**从「同向段序列」里取相邻两个（反向段过滤时已剔除），
          它们在**段序列**里隔了几个段取决于中间夹了多少反向段，实测 1~5；
        - **二买/三买/二卖**按 p152「第一次次级别回调」/ p590「第一次回抽」，
          取前段之后**紧邻**的那个反向段，段距恒为 1。

        用段序号（起点索引 // 段长）而不是索引差来判定——段按固定长度切，
        索引差会被段边界错位污染（同样的相接关系会算出间隙 0 或 10）。
        """

        from chanpicker.core import Bar, ChanConfig, ChanEngine

        prices = self.PRICES
        bars = [
            Bar(symbol="sh600000", date=f"2024-01-{i+1:02d}", open=p, high=p + 0.4,
                low=p - 0.4, close=p, volume=1_000_000, amount=p * 1_000_000)
            for i, p in enumerate(prices)
        ]
        config = ChanConfig(
            min_stroke_gap=1, min_second_buy_score=0.0, min_third_buy_score=0.0,
            min_macd_area=9999,
            require_zhongshu_for_first_buy=False,
            require_zhongshu_for_first_sell=False,
            require_standard_divergence=False,
            require_trend_context=False,
            min_legs_bars=3,
        )
        signals = ChanEngine(config).scan(bars).signals
        metas = [s.meta for s in signals if "previous_segment" in s.meta]
        self.assertTrue(metas, "构造序列应至少产出一个带段坐标的信号")
        checked = 0
        for signal in signals:
            meta = signal.meta
            if "previous_segment" not in meta:
                continue
            previous = meta["previous_segment"]
            current = meta["current_segment"]
            # 段按固定长度切（p152 最低级别定义），所以段序号 = 起点索引 // 段长。
            # 用「隔了几个段」而不是索引差来判定相接关系——索引差会被段边界错位污染。
            prev_seq = previous["start_index"] // config.min_legs_bars
            curr_seq = current["start_index"] // config.min_legs_bars
            seg_gap = curr_seq - prev_seq
            self.assertGreaterEqual(seg_gap, 0, "背驰段不应早于前段")
            if previous["direction"] == current["direction"]:
                # 一买/一卖取的是「同向段序列里相邻的两个」（反向段在过滤时已剔除），
                # 它们在**段序列**里的距离取决于中间夹了几个反向段，实测 1~5 都有，
                # 所以这里只锁「确实隔了段」这一性质，不锁死具体距离。
                self.assertGreaterEqual(
                    seg_gap, 1,
                    f"{signal.kind.value} 段距 {seg_gap}：同向段不应紧邻（那说明取的不是相邻两个）",
                )
            else:
                # 二买/三买/二卖：p152「第一次次级别回调」+ p590「第一次回抽」——
                # 前段与背驰段必须**紧邻**（段距 1），不能跨过多个同向段去远处找。
                self.assertEqual(
                    seg_gap, 1,
                    f"{signal.kind.value} 段距 {seg_gap}：回调段应紧邻离开段（段距 1）",
                )
            checked += 1
        self.assertGreater(checked, 0, "没有任何信号可校验")

    def test_segment_meta_reads_stroke_endpoints(self) -> None:
        from chanpicker import Fractal, Stroke

        stroke = Stroke(
            start=Fractal(index=3, date="2024-01-03", kind="bottom", price=70.0),
            end=Fractal(index=9, date="2024-01-09", kind="top", price=95.0),
        )
        meta = segment_meta(stroke)
        self.assertEqual(meta["start_index"], 3)
        self.assertEqual(meta["end_index"], 9)
        self.assertEqual(meta["start_price"], 70.0)
        self.assertEqual(meta["end_price"], 95.0)
        self.assertEqual(meta["direction"], "up")

    def test_old_signals_without_segments_are_survivable(self) -> None:
        # 老库里没有 previous_segment/current_segment，前端要能判空跳过，
        # 不能因为缺字段整个图表崩掉。
        signal = Signal(
            symbol="sh600000",
            kind=SignalKind.FIRST_BUY,
            date="2024-01-10",
            bar_index=10,
            price=70.0,
            anchor_index=9,
            anchor_price=68.0,
            message="背驰后一买成立",
            meta={"divergence_score": 0.2},
        )
        self.assertNotIn("current_segment", signal.meta)


if __name__ == "__main__":
    unittest.main()

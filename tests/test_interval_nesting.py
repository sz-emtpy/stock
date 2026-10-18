"""30 分钟区间套（interval nesting）单元测试。

区间套 = 用 30 分钟（次级别）同位置结构确认日线（大级别）买点。本组用例不触网：
m30 K 线直接通过 `m30_bars` 参数注入；日线买点用 `anchor_date` 直接指定。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker import Bar, ChanConfig, ChanEngine, SignalKind  # noqa: E402

from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402

# 段口径下能产出「一买 → 二买」配对的构造序列（取自 tests/test_core.py 的同名 fixture）。
# 这里只用它保证 m30 扫描能检出买点；区间套测试不关心配对本身。
LEG_DOWN_WITH_BUYS = [100, 100, 98, 97, 99, 97, 96, 97, 96, 95, 96, 98,
    100, 100, 100, 100, 99, 100, 98, 98, 96, 95, 94, 95,
    94, 93, 93, 91, 91, 90, 88, 90, 92, 92, 91, 91,
    91, 92, 91, 89, 91, 93, 94, 95, 95, 97, 99, 101,
    99, 97, 96, 97, 94, 96, 98, 96, 95, 97, 96, 95,
    95, 93, 94, 94, 93, 96, 98, 96, 95, 95, 93, 91,
    90, 91, 91, 90, 91, 93, 92, 92, 91, 91, 89, 87,
    87, 86, 87, 87, 88]


def loose_test_config() -> ChanConfig:
    """与 tests/test_core.py 同义的放宽配置：让构造序列能触发信号。"""

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


def make_m30_bars(prices, symbol="sh600519", start_day="2026-10-14", period="m30") -> list[Bar]:
    """把价格序列摊成 30 分钟时间戳的 K 线（仅用于测试，时间不要求真实连续）。"""

    bars: list[Bar] = []
    start = datetime.strptime(start_day + " 09:30", "%Y-%m-%d %H:%M")
    for i, price in enumerate(prices):
        ts = (start + timedelta(minutes=30 * i)).strftime("%Y-%m-%d %H:%M")
        bars.append(
            Bar(
                symbol=symbol,
                date=ts,
                open=price,
                high=price + 0.4,
                low=price - 0.4,
                close=price,
                volume=1_000_000,
                amount=price * 1_000_000,
                period=period,
            )
        )
    return bars


def first_buy_day(bars: list[Bar], config: ChanConfig) -> str:
    """用同样的配置跑一遍引擎，取出 m30 一买落在的日历日（[:10]）。"""

    fb = next(
        s for s in ChanEngine(config).scan(bars).signals if s.kind == SignalKind.FIRST_BUY
    )
    return str(fb.date)[:10]


class IntervalNestingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.config = loose_test_config()
        self.service = ScanService(self.store, config=self.config)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def test_no_day_buy_in_store_returns_unavailable(self) -> None:
        """anchor_date 省略且本地无任何日线买点 → available=False，不走网络。"""
        out = self.service.interval_nesting("sh600519")
        self.assertFalse(out["available"])
        self.assertIn("日线买点", out["message"])

    def test_no_m30_data_returns_unavailable(self) -> None:
        """注入了空 m30 数据（< MIN_BARS）→ available=False，不触发远端拉取。"""
        out = self.service.interval_nesting("sh600519", anchor_date="2026-10-14", m30_bars=[])
        self.assertFalse(out["available"])
        self.assertIn("30 分钟数据", out["message"])

    def test_anchor_outside_m30_window_is_unavailable_but_honest(self) -> None:
        """日线买点日早于 m30 数据窗口 → available=True、confirmed=False，说明做不了。"""
        # m30 覆盖 10-10 ~ 10-14，日线买点在 09-01（远早于窗口）
        m30 = make_m30_bars(LEG_DOWN_WITH_BUYS, start_day="2026-10-10")
        out = self.service.interval_nesting(
            "sh600519", anchor_date="2026-09-01", anchor_kind="FIRST_BUY", m30_bars=m30
        )
        self.assertTrue(out["available"])
        self.assertFalse(out["confirmed"])
        self.assertIsNone(out["matched"])
        self.assertEqual(out["m30_window"]["start"], "2026-10-10")
        self.assertIn("不在 30 分钟数据窗口", out["message"])

    def test_confirmed_when_m30_buy_at_same_position(self) -> None:
        """日线买点日与 m30 一买落在同一天 → 区间套确认，matched 指向该 m30 一买。"""
        m30 = make_m30_bars(LEG_DOWN_WITH_BUYS, start_day="2026-10-14")
        buy_day = first_buy_day(m30, self.config)
        out = self.service.interval_nesting(
            "sh600519", anchor_date=buy_day, anchor_kind="FIRST_BUY", m30_bars=m30
        )
        self.assertTrue(out["available"])
        self.assertTrue(out["confirmed"], msg=out["message"])
        self.assertIsNotNone(out["matched"])
        self.assertEqual(out["matched"]["kind"], "FIRST_BUY")
        self.assertEqual(str(out["matched"]["date"])[:10], buy_day)
        # 同位置买点必然落在窗口内
        self.assertIn(buy_day, {out["m30_window"]["start"], out["m30_window"]["end"]})

    def test_unconfirmed_when_m30_has_no_buy_near_anchor(self) -> None:
        """m30 全部为单调上涨（无任何分型/买点）且 anchor 在窗口内 → confirmed=False。"""
        rising = list(range(100, 140))  # 严格单调，无局部极值 → 无分型 → 无买点
        m30 = make_m30_bars(rising, start_day="2026-10-14")
        out = self.service.interval_nesting(
            "sh600519", anchor_date="2026-10-14", anchor_kind="FIRST_BUY", m30_bars=m30
        )
        self.assertTrue(out["available"])
        self.assertFalse(out["confirmed"])
        self.assertEqual(out["m30_buys"], [])

    def test_m30_structure_reports_counts(self) -> None:
        """m30 结构统计：bars/zhongshu/signals/buys 字段齐全且合理。"""
        m30 = make_m30_bars(LEG_DOWN_WITH_BUYS, start_day="2026-10-14")
        buy_day = first_buy_day(m30, self.config)
        out = self.service.interval_nesting(
            "sh600519", anchor_date=buy_day, anchor_kind="FIRST_BUY", m30_bars=m30
        )
        struct = out["m30_structure"]
        self.assertEqual(struct["bars"], len(m30))
        self.assertGreaterEqual(struct["buys"], 1)
        self.assertGreaterEqual(struct["signals"], 1)


if __name__ == "__main__":
    unittest.main()

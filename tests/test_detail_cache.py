"""详情页取数缓存回归：本地优先，没有才抓，抓一次就记住。

背景（实测）：详情页曾要 6.8 秒才出来，两个根因——
1. `_load_bars` 的日线新鲜度判断是 `cached[-1].date == today`，周末/节假日
   永远不成立 → **每次进详情都联网重抓 400 根**（514ms）；
2. `_sectors_for` 每次都对没有板块缓存的股票联网反查（1 次产业链 + 最多 3 次
   板块搜索 × 3 秒间隔 = **5.7 秒**），且失败后每次都重试。

改造后首屏 43ms（约 158 倍），板块/对比改为独立懒加载接口。这组用例把
「不许退回每次联网」钉死：所有网络函数都被替换成会抛错的桩。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import Bar, Signal, SignalKind  # noqa: E402

from backend import service as service_module  # noqa: E402
from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402


def make_bars(symbol: str, count: int = 60, start_day: str = "2026-09-01") -> list[Bar]:
    """造一段日线。日期按自然日连续推进，保证 (symbol, trade_date) 唯一。"""

    from datetime import date, timedelta

    base = date.fromisoformat(start_day)
    out: list[Bar] = []
    price = 100.0
    for i in range(count):
        price += 0.5 if i % 2 == 0 else -0.3
        out.append(
            Bar(
                symbol=symbol,
                date=(base + timedelta(days=i)).isoformat(),
                open=price,
                high=price + 0.4,
                low=price - 0.4,
                close=price,
                volume=1_000_000,
                amount=price * 1_000_000,
                period="day",
            )
        )
    return out


class DetailCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)
        self.symbol = "sh600519"

    def _seed_bars(self, count: int = 60) -> None:
        self.store.save_bars(make_bars(self.symbol, count))

    # ---------------- _load_bars：本地优先 ----------------

    def test_fresh_cache_hits_without_network(self) -> None:
        """缓存已覆盖本地已知最新交易日 → 完全不联网。

        这是最关键的一条：原实现的 `== today` 判断在周末必然失败，导致每次
        进详情都重抓。现在只有真正过期（缓存落后于全库最新交易日）才联网。
        """

        self._seed_bars()
        latest_local = self.store.recent_trading_dates(1)[0]

        boom = mock.Mock(side_effect=AssertionError("缓存新鲜时不该联网取数"))
        with mock.patch.object(service_module, "fetch_bars", boom):
            bars = self.service._load_bars(self.symbol, 400, True, "day")
        boom.assert_not_called()
        self.assertEqual(bars[-1].date, latest_local)

    def _make_stale(self) -> None:
        """让主股缓存"变旧"：库里另一只票有更新的交易日，主股就落后了。

        `_bars_fresh` 比的是「全库最新交易日」，这正是真实场景：全市场扫描
        每天都在推进这个日期，某只票没跟上就说明它该更新了。
        """

        # 主股 2026-09-01 起 60 天 → 到 10-30；参照票更晚，到 11-20
        self.store.save_bars(make_bars("sh000001", 5, start_day="2026-11-16"))

    def test_stale_cache_fetches_once_then_cools_down(self) -> None:
        """缓存落后于最新交易日 → 抓一次；紧接着再进来必须命中冷却戳、不再抓。"""

        self._seed_bars()
        self._make_stale()
        fresh_bars = make_bars(self.symbol, 60, start_day="2026-11-16")

        calls: list[int] = []

        def fake_fetch(symbol, period="day", days=400, fq="qfq", retries=3):
            calls.append(1)
            return fresh_bars

        with mock.patch.object(service_module, "fetch_bars", fake_fetch):
            self.service._load_bars(self.symbol, 400, True, "day")
            self.service._load_bars(self.symbol, 400, True, "day")
        self.assertEqual(len(calls), 1, "冷却期内第二次不该再联网")

    def test_fetch_failure_is_remembered(self) -> None:
        """抓取失败也记戳：否则每次进详情都要白等一次超时。"""

        self._seed_bars()
        self._make_stale()

        calls: list[int] = []

        def failing_fetch(*args, **kwargs):
            calls.append(1)
            raise service_module.MarketDataError("boom")

        with mock.patch.object(service_module, "fetch_bars", failing_fetch):
            first = self.service._load_bars(self.symbol, 400, True, "day")
            second = self.service._load_bars(self.symbol, 400, True, "day")
        self.assertEqual(len(calls), 1, "失败后冷却期内不该反复重试")
        self.assertTrue(first and second, "失败时应退回缓存而不是抛错")

    # ---------------- 分钟级别：短冷却，不能拿旧盘中数据 ----------------

    def test_minute_period_uses_short_cooldown(self) -> None:
        """分钟级别必须用 5 分钟冷却，而不是日线的 6 小时。

        否则「日内择时」会拿着上午的 K 线做下午的判断——原实现每次都抓，
        正是为了这个。这条用例锁住两个常量不许被合并。
        """

        self.assertLess(
            service_module.MINUTE_BARS_COOLDOWN_SECONDS,
            service_module.BARS_COOLDOWN_SECONDS,
            "分钟冷却必须显著短于日线冷却",
        )
        self.assertLessEqual(service_module.MINUTE_BARS_COOLDOWN_SECONDS, 600)

    def test_minute_cache_never_counts_as_fresh(self) -> None:
        """分钟 K 一律不按「交易日」判新鲜，只能靠短冷却节流。"""

        minute_bars = [
            Bar(
                symbol=self.symbol,
                date=f"2026-10-16 {9 + i // 2:02d}:{30 * (i % 2):02d}",
                open=10.0,
                high=10.4,
                low=9.6,
                close=10.0,
                period="m30",
            )
            for i in range(10)
        ]
        self.assertFalse(self.service._bars_fresh(minute_bars, "m30"))

    def test_minute_still_refetches_after_cooldown(self) -> None:
        """冷却到期后必须重新抓，而不是一直吃旧缓存。"""

        self.store.save_bars(make_bars(self.symbol, 40), period="day")
        # 分钟缓存故意存一份，并把戳设成"很久以前"
        minute = [
            Bar(
                symbol=self.symbol,
                date=f"2026-10-16 {9 + i // 2:02d}:{30 * (i % 2):02d}",
                open=10.0,
                high=10.4,
                low=9.6,
                close=10.0,
                period="m30",
            )
            for i in range(40)
        ]
        self.store.save_bars(minute, period="m30")
        self.store.mark_fetch("bars", f"{self.symbol}:m30", at=1_000_000.0)

        calls: list[int] = []

        def fake_fetch(symbol, period="day", days=400, fq="qfq", retries=3):
            calls.append(1)
            return minute

        with mock.patch.object(service_module, "fetch_bars", fake_fetch):
            self.service._load_bars(self.symbol, 80, True, "m30")
        self.assertEqual(len(calls), 1, "冷却到期后分钟数据必须重抓")

    # ---------------- _sectors_for：命中缓存 / 失败冷却 ----------------

    def test_sectors_cache_hit_skips_network(self) -> None:
        self.store.save_stock_sectors(self.symbol, [{"code": "pt01801193", "name": "证券Ⅱ", "level": "sw2"}])
        boom = mock.Mock(side_effect=AssertionError("有板块缓存时不该联网反查"))
        with mock.patch.object(service_module, "fetch_stock_sectors", boom):
            got = self.service._sectors_for(self.symbol)
        boom.assert_not_called()
        self.assertEqual([s["name"] for s in got], ["证券Ⅱ"])

    def test_sectors_miss_cools_down(self) -> None:
        """反查返回空（无板块映射）也要记戳，冷却期内不再尝试。"""

        calls: list[int] = []

        def empty_fetch(symbol, max_themes=3, pause=3.0):
            calls.append(1)
            return []

        with mock.patch.object(service_module, "fetch_stock_sectors", empty_fetch):
            self.assertEqual(self.service._sectors_for(self.symbol), [])
            self.assertEqual(self.service._sectors_for(self.symbol), [])
        self.assertEqual(len(calls), 1, "抓空之后冷却期内不该重试")

    def test_sectors_failure_cools_down(self) -> None:
        calls: list[int] = []

        def failing_fetch(*args, **kwargs):
            calls.append(1)
            raise service_module.MarketDataError("boom")

        with mock.patch.object(service_module, "fetch_stock_sectors", failing_fetch):
            self.assertEqual(self.service._sectors_for(self.symbol), [])
            self.assertEqual(self.service._sectors_for(self.symbol), [])
        self.assertEqual(len(calls), 1)

    # ---------------- analyze_symbol：板块/对比不阻塞主链路 ----------------

    def test_analyze_is_lazy_when_sectors_not_cached(self) -> None:
        """板块无缓存时 analyze 必须立刻返回，并把对比标记为 lazy。

        关键断言：`fetch_stock_sectors`（5.7 秒）**一次都不该被调用**——
        调用即说明又回到阻塞主链路的老路。
        """

        self._seed_bars()
        boom = mock.Mock(side_effect=AssertionError("analyze 主链路不该反查板块"))

        with mock.patch.object(service_module, "fetch_bars", lambda *a, **k: make_bars(self.symbol)):
            with mock.patch.object(service_module, "fetch_stock_sectors", boom):
                payload = self.service.analyze_symbol(self.symbol, use_cache=True, period="day")

        boom.assert_not_called()
        self.assertTrue(payload["comparison"].get("lazy"), "对比应为懒加载标记")
        self.assertEqual(payload["sectors"], [])
        self.assertGreater(payload["bars_count"], 0, "K 线与买卖点必须照常返回")

    def test_analyze_includes_comparison_when_sectors_cached(self) -> None:
        """板块已有缓存时（纯本地、无网络）允许直接带上，省掉一次二次请求。"""

        self._seed_bars()
        self.store.save_stock_sectors(self.symbol, [{"code": "pt01801193", "name": "证券Ⅱ", "level": "sw2"}])
        with mock.patch.object(service_module, "fetch_bars", lambda *a, **k: make_bars(self.symbol)):
            with mock.patch.object(service_module, "fetch_bars", lambda *a, **k: make_bars(self.symbol)):
                payload = self.service.analyze_symbol(self.symbol, use_cache=True, period="day")
        self.assertFalse((payload["comparison"] or {}).get("lazy"))
        self.assertEqual([s["name"] for s in payload["sectors"]], ["证券Ⅱ"])

    def test_comparison_payload_standalone(self) -> None:
        """独立接口可单独调用，并且板块结果会被落库（第二次走本地）。"""

        self._seed_bars()
        fetched = [{"code": "pt01801193", "name": "证券Ⅱ", "level": "sw2"}]
        calls: list[int] = []

        def fake_sectors(symbol, max_themes=3, pause=3.0):
            calls.append(1)
            return fetched

        with mock.patch.object(service_module, "fetch_stock_sectors", fake_sectors):
            first = self.service.comparison_payload(self.symbol)
            second = self.service.comparison_payload(self.symbol)

        self.assertEqual([s["name"] for s in first["sectors"]], ["证券Ⅱ"])
        self.assertEqual([s["name"] for s in second["sectors"]], ["证券Ⅱ"])
        self.assertEqual(len(calls), 1, "第一次抓到后必须落库，第二次不该再联网")


class FetchStampTest(unittest.TestCase):
    """冷却戳本身的行为（Store 层）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def test_never_fetched_returns_none(self) -> None:
        self.assertIsNone(self.store.fetch_stamp("bars", "sh600519:day"))
        self.assertFalse(self.store.fetched_within("bars", "sh600519:day", 3600))

    def test_mark_and_read_back(self) -> None:
        self.store.mark_fetch("bars", "sh600519:day")
        self.assertIsNotNone(self.store.fetch_stamp("bars", "sh600519:day"))
        self.assertTrue(self.store.fetched_within("bars", "sh600519:day", 60))

    def test_cooldown_expires(self) -> None:
        self.store.mark_fetch("bars", "sh600519:day", at=1_000_000.0)
        self.assertFalse(self.store.fetched_within("bars", "sh600519:day", 60))

    def test_stamp_is_upsert_not_duplicate(self) -> None:
        self.store.mark_fetch("bars", "sh600519:day", at=1_000_000.0)
        self.store.mark_fetch("bars", "sh600519:day", at=2_000_000.0)
        self.assertEqual(self.store.fetch_stamp("bars", "sh600519:day"), 2_000_000.0)


if __name__ == "__main__":
    unittest.main()

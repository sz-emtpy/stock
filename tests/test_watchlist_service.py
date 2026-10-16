"""关注圈出圈、成功率统计与本地重评的服务层测试。

三个必须锁死的口径：
1. 出圈 = FAILED / SECOND_BUY_FAILED 不再出现在 /api/watchlist，但记录仍留在库里
   （否则成功率统计会失去分母）；
2. 成功率的分子分母：一买以「是否引出二买」计，二买以「考验窗口内是否守住二买低点」计；
3. refresh_watchlist 全程走本地缓存，不能触发任何网络取数。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import (  # noqa: E402
    Bar,
    Signal,
    SignalKind,
    WatchItem,
    WatchState,
)

from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402


def make_bars(symbol: str, count: int = 60, start_price: float = 100.0) -> list[Bar]:
    bars: list[Bar] = []
    price = start_price
    for offset in range(count):
        price = price + (0.7 if offset % 2 == 0 else -0.35)
        bars.append(
            Bar(
                symbol=symbol,
                date=f"2026-01-{offset + 1:02d}",
                open=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=1_000_000,
                amount=price * 1_000_000,
            )
        )
    return bars


def watch_item(symbol: str, state: WatchState, index: int = 5, low: float = 90.0) -> WatchItem:
    return WatchItem(
        symbol=symbol,
        state=state,
        first_buy_date="2026-01-06",
        first_buy_index=index,
        first_buy_price=95.0,
        first_buy_low=low,
        last_update_date="2026-01-20",
        second_buy_date="2026-01-09" if state == WatchState.OPPORTUNITY else None,
        second_buy_price=97.0 if state == WatchState.OPPORTUNITY else None,
        meta={"second_buy_low": 96.0, "second_buy_index": 8} if state == WatchState.OPPORTUNITY else {},
    )


class WatchlistFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_failed_items_are_removed_from_watchlist_but_kept_in_store(self) -> None:
        self.store.save_watch_items(
            [
                watch_item("sh600000", WatchState.WAIT_SECOND_BUY),
                watch_item("sh600001", WatchState.WEAK_TRACK),
                watch_item("sh600002", WatchState.OPPORTUNITY),
                watch_item("sh600003", WatchState.FAILED),
                watch_item("sh600004", WatchState.SECOND_BUY_FAILED),
            ]
        )
        listed = [item["symbol"] for item in self.service.watchlist()]
        self.assertEqual(listed, ["sh600000", "sh600001", "sh600002"])

        # 库里必须仍是 5 条：出圈是「软剔除」，不是删库
        stored = {item.symbol for item in self.store.load_watch_items()}
        self.assertEqual(len(stored), 5)
        self.assertIn("sh600004", stored)

    def test_opportunity_still_qualifies(self) -> None:
        # 机会态仍在圈内，个股页的「符合买入条件」判定要继续认它
        self.store.save_watch_items([watch_item("sh600000", WatchState.OPPORTUNITY)])
        bars = make_bars("sh600000")
        payload = self.service._build_analysis_payload(
            "sh600000", "测试股", bars, {"signals": []}, self.store.load_watch_items("sh600000"), "day"
        )
        self.assertTrue(payload["qualifies"])

    def test_second_buy_failed_does_not_qualify(self) -> None:
        self.store.save_watch_items([watch_item("sh600000", WatchState.SECOND_BUY_FAILED)])
        bars = make_bars("sh600000")
        payload = self.service._build_analysis_payload(
            "sh600000", "测试股", bars, {"signals": []}, self.store.load_watch_items("sh600000"), "day"
        )
        self.assertFalse(payload["qualifies"])


class SuccessRateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_empty_history_reports_no_sample(self) -> None:
        stats = self.service.watchlist_stats()
        self.assertIsNone(stats["first_buy"]["rate"])
        self.assertEqual(stats["first_buy"]["rate_label"], "—")
        self.assertEqual(stats["second_buy"]["rate_label"], "—")
        self.assertIn("没有已终结", stats["message"])

    def test_first_buy_rate_counts_reaching_second_buy_as_success(self) -> None:
        # 8 笔一买：3 笔引出二买（其中 1 笔二买后来失败，仍算一买成功），
        # 2 笔跌破一买低点，2 笔超时弱跟踪，1 笔还在待二买。
        states = [
            WatchState.OPPORTUNITY,
            WatchState.OPPORTUNITY,
            WatchState.SECOND_BUY_FAILED,
            WatchState.FAILED,
            WatchState.FAILED,
            WatchState.WEAK_TRACK,
            WatchState.WEAK_TRACK,
            WatchState.WAIT_SECOND_BUY,
        ]
        self.store.save_watch_items(
            [watch_item(f"sh60000{i}", state) for i, state in enumerate(states)]
        )
        stats = self.service.watchlist_stats()
        first = stats["first_buy"]
        self.assertEqual(first["success"], 3)
        self.assertEqual(first["failed"], 2)
        self.assertEqual(first["weak"], 2)
        self.assertEqual(first["pending"], 1)
        self.assertEqual(first["closed"], 7)
        self.assertEqual(first["rate_label"], "42.9%")
        self.assertAlmostEqual(first["rate"], 3 / 7, places=4)

    def test_second_buy_rate_uses_the_test_window(self) -> None:
        self.store.save_watch_items(
            [
                watch_item("sh600000", WatchState.OPPORTUNITY),
                watch_item("sh600001", WatchState.OPPORTUNITY),
                watch_item("sh600002", WatchState.SECOND_BUY_FAILED),
                watch_item("sh600003", WatchState.FAILED),  # 没走到二买，不进二买分母
            ]
        )
        stats = self.service.watchlist_stats()
        second = stats["second_buy"]
        self.assertEqual(second["confirmed"], 3)
        self.assertEqual(second["success"], 2)
        self.assertEqual(second["failed"], 1)
        self.assertEqual(second["rate_label"], "66.7%")

    def test_watchlist_counts_split_active_and_removed(self) -> None:
        self.store.save_watch_items(
            [
                watch_item("sh600000", WatchState.WAIT_SECOND_BUY),
                watch_item("sh600001", WatchState.OPPORTUNITY),
                watch_item("sh600002", WatchState.WEAK_TRACK),
                watch_item("sh600003", WatchState.FAILED),
                watch_item("sh600004", WatchState.SECOND_BUY_FAILED),
            ]
        )
        stats = self.service.watchlist_stats()
        watch = stats["watchlist"]
        self.assertEqual(watch["active"], 3)
        self.assertEqual(watch["removed_total"], 2)
        self.assertEqual(watch["removed"]["FAILED"], 1)
        self.assertEqual(watch["removed"]["SECOND_BUY_FAILED"], 1)
        self.assertEqual(watch["labels"]["SECOND_BUY_FAILED"], "二买失败")
        self.assertEqual(stats["wait_bars"], 30)


class RefreshWatchlistTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_refresh_uses_local_cache_only(self) -> None:
        # 本地有 K 线的会被重评；没有 K 线的记为跳过而不是去联网拉
        self.store.save_bars(make_bars("sh600000"))
        self.store.save_watch_items(
            [
                watch_item("sh600000", WatchState.WAIT_SECOND_BUY),
                watch_item("sh600009", WatchState.WAIT_SECOND_BUY),  # 无本地K线
            ]
        )
        result = self.service.refresh_watchlist()
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertIn("已重评 1 只", result["message"])

    def test_refresh_ignores_closed_items(self) -> None:
        # 已出圈的是终态，没必要也没理由重评
        self.store.save_bars(make_bars("sh600000"))
        self.store.save_watch_items([watch_item("sh600000", WatchState.SECOND_BUY_FAILED)])
        result = self.service.refresh_watchlist()
        self.assertEqual(result["requested"], 0)
        self.assertEqual(result["evaluated"], 0)

    def test_refresh_flips_state_when_second_buy_low_breaks(self) -> None:
        # 造一条「机会态 + 最新收盘价已跌破二买低点」的记录，重评后应翻成二买失败。
        # 二买确认位记在 index 8，考验窗口 30 根 → 最后一根必须落在 index 38 以内。
        bars = make_bars("sh600000", count=38, start_price=100.0)
        # 让最后一根收盘价低于二买低点 96
        bars[-1] = Bar(
            symbol="sh600000",
            date=bars[-1].date,
            open=95.0,
            high=95.5,
            low=94.5,
            close=95.0,
            volume=1_000_000,
            amount=95.0 * 1_000_000,
        )
        self.store.save_bars(bars)
        self.store.save_watch_items([watch_item("sh600000", WatchState.OPPORTUNITY)])

        result = self.service.refresh_watchlist()
        item = self.store.load_watch_items("sh600000")[0]
        self.assertEqual(item.state, WatchState.SECOND_BUY_FAILED)
        self.assertEqual(result["changed"], 1)


class StaleRunRecoveryTest(unittest.TestCase):
    def test_running_rows_are_marked_interrupted_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.create_run("run-stale", scope="all", total=100)
            store.update_run("run-stale", processed=30, hits=2)
            self.assertEqual(store.get_run("run-stale")["status"], "running")

            # 新建 service 会触发一次残留修复（模拟服务重启）
            ScanService(store)
            run = store.get_run("run-stale")
            self.assertEqual(run["status"], "interrupted")
            self.assertIn("重启", run["message"])

            # 幂等：再建一次不会把已终结的行改坏
            ScanService(store)
            self.assertEqual(store.get_run("run-stale")["status"], "interrupted")
            store.finish_run("run-stale", status="finished", message="正常完成")
            ScanService(store)
            self.assertEqual(store.get_run("run-stale")["status"], "finished")
            store.close()


if __name__ == "__main__":
    unittest.main()

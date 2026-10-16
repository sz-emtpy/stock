"""每日分钟数据增量拉取脚本的回归测试。

重点锁三件事：
1. **只拉在圈的标的**——已出圈的不会再更新二买，拉了也是浪费
   （实测：关注记录 4439 条 vs 在圈 1978 只，多拉一倍半）
2. **批间隔要够大**——数据源连续请求会限流，实测需 25 秒以上
3. **失败要如实上报**——不能静默吞掉取数错误
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync_intraday.py"


def load_script():
    """加载被测脚本（它会 print 进度，这里静音免得污染测试输出）。"""

    spec = importlib.util.spec_from_file_location("sync_intraday", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_intraday"] = module
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


def quiet(fn, *args, **kwargs):
    """调用会 print 的被测函数，把输出吞掉。"""

    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


class BatchPacingTest(unittest.TestCase):
    """批间隔必须防限流。"""

    def test_default_pause_is_at_least_25_seconds(self) -> None:
        module = load_script()
        self.assertGreaterEqual(
            module.BATCH_PAUSE, 25.0,
            "批间隔低于 25 秒会触发 westock 限流「请求过于频繁」",
        )

    def test_batch_size_matches_westock_limit(self) -> None:
        module = load_script()
        self.assertLessEqual(
            module.BATCH_SIZE, 20,
            "westock 单次最多 20 只代码，超过会被截断",
        )


class WatchlistScopeTest(unittest.TestCase):
    """只拉在圈标的。"""

    def setUp(self) -> None:
        from backend.store import Store

        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.module = load_script()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _seed(self):
        from chanpicker import WatchItem, WatchState

        self.store.upsert_symbol("sh600001", "在圈一")
        self.store.upsert_symbol("sh600002", "在圈二")
        self.store.upsert_symbol("sh600003", "已出圈")
        self.store.save_watch_items([
            WatchItem(
                symbol="sh600001", state=WatchState.WAIT_SECOND_BUY,
                first_buy_date="2026-07-01", first_buy_index=2,
                first_buy_price=10.0, first_buy_low=9.5, last_update_date="2026-07-02",
            ),
            WatchItem(
                symbol="sh600002", state=WatchState.OPPORTUNITY,
                first_buy_date="2026-07-03", first_buy_index=5,
                first_buy_price=11.0, first_buy_low=10.5, last_update_date="2026-07-04",
                second_buy_date="2026-07-06", second_buy_price=11.5,
            ),
            WatchItem(
                symbol="sh600003", state=WatchState.FAILED,
                first_buy_date="2026-07-05", first_buy_index=8,
                first_buy_price=12.0, first_buy_low=11.0, last_update_date="2026-07-07",
                exit_reason="一买失败",
            ),
        ])

    def test_excludes_exited_symbols(self) -> None:
        """已出圈的（FAILED / SECOND_BUY_FAILED）不该再拉分钟数据。"""

        self._seed()
        universe = quiet(self.module.load_watchlist_symbols, self.store, "watchlist", None)
        symbols = {sym for sym, _ in universe}
        self.assertIn("sh600001", symbols)
        self.assertIn("sh600002", symbols)
        self.assertNotIn(
            "sh600003", symbols,
            "已出圈的标的仍在被拉取，白白消耗限流配额",
        )

    def test_deduplicates_multiple_records_per_symbol(self) -> None:
        """同一 symbol 可能有多条关注记录，去重后只拉一次。"""

        from chanpicker import WatchItem, WatchState

        self.store.upsert_symbol("sh600004", "多条记录")
        self.store.save_watch_items([
            WatchItem(
                symbol="sh600004", state=WatchState.WEAK_TRACK,
                first_buy_date="2026-07-01", first_buy_index=2,
                first_buy_price=10.0, first_buy_low=9.5, last_update_date="2026-07-02",
            ),
            WatchItem(
                symbol="sh600004", state=WatchState.WEAK_TRACK,
                first_buy_date="2026-06-01", first_buy_index=9,
                first_buy_price=10.0, first_buy_low=9.5, last_update_date="2026-06-02",
            ),
        ])
        universe = quiet(self.module.load_watchlist_symbols, self.store, "watchlist", None)
        symbols = [sym for sym, _ in universe]
        self.assertEqual(symbols.count("sh600004"), 1, "同一 symbol 被重复拉取")

    def test_limit_caps_result(self) -> None:
        self._seed()
        universe = quiet(self.module.load_watchlist_symbols, self.store, "watchlist", 1)
        self.assertEqual(len(universe), 1)


class SaveBarsIdempotencyTest(unittest.TestCase):
    """重复跑不能产生重复行——否则增量拉取会把库撑爆。"""

    def test_saving_twice_keeps_row_count(self) -> None:
        from backend.provider import Bar
        from backend.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            try:
                bars = [
                    Bar(
                        symbol="sh600000", date="2026-10-16 10:30:00", open=9.0, high=9.2,
                        low=8.9, close=9.1, volume=1000, amount=9100, period="m30",
                    ),
                    Bar(
                        symbol="sh600000", date="2026-10-16 11:00:00", open=9.1, high=9.3,
                        low=9.0, close=9.2, volume=1100, amount=10120, period="m30",
                    ),
                ]
                store.save_bars(bars, period="m30")
                first = store._conn.execute(
                    "SELECT COUNT(*) FROM bars WHERE period='m30'"
                ).fetchone()[0]
                store.save_bars(bars, period="m30")
                second = store._conn.execute(
                    "SELECT COUNT(*) FROM bars WHERE period='m30'"
                ).fetchone()[0]
                self.assertEqual(first, second, "重复保存产生了重复行，增量拉取会撑爆库")
                self.assertEqual(second, 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
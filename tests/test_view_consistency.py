"""四个视图的买点数量口径一致性（2026-10-17 用户报「同一买点数对不上」）。

四个视图原本各走各的链路，同一个买点在不同页签出现/消失、数量差 10 倍：

| 视图 | 原数据源 | 原窗口 | 原来的问题 |
|---|---|---|---|
| 市场扫描 | scan_hits 快照 | 30 自然日 | 唯一键 (run_id,symbol,kind) 让每股票每类只剩一条 |
| 三日买卖点池 | signals 表 | 3 交易日 | 去重口径与市场扫描不同 |
| 关注圈 | watchlist 状态机 | 全历史 | 僵尸记录永不终结，新一买开不了新一轮 |
| 体系选股 | signal_outcomes | 3 交易日 | 不去重，比池子多出同 key 的旧信号 |

本文件锁死修复后的关系：
1. `candidates(days=N)` 与 `pools(days=N)` **完全同源同数**（只差窗口参数）；
2. 三日池每行都挂体系分，且与 `system_pick` 同 key 同分；
3. `scan_hits` 唯一键含 trade_date，同一股票同类的多个历史买点都要留住；
4. 关注圈二买考验期满守住 → 归档为 SECOND_BUY_OK，并允许新一买开新一轮。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import (  # noqa: E402
    Bar,
    ChanConfig,
    Signal,
    SignalKind,
    TRACKING_STATES,
    WatchItem,
    WatchState,
    WatchlistManager,
)

from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402


def bar(symbol: str, day: str, close: float = 10.0) -> Bar:
    return Bar(
        symbol=symbol,
        date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000_000,
        amount=0.0,
        period="day",
    )


def make_signal(
    symbol: str,
    kind: SignalKind,
    trade_date: str,
    price: float = 10.0,
    bar_index: int = 10,
) -> Signal:
    return Signal(
        symbol=symbol,
        kind=kind,
        date=trade_date,
        bar_index=bar_index,
        price=price,
        anchor_index=bar_index - 1,
        anchor_price=price * 0.97,
        message="x",
        meta={"divergence_score": 0.2},
        period="day",
    )


class CandidatePoolSameSourceTest(unittest.TestCase):
    """市场扫描与三日池必须同源同数——这是用户「对不上」的直接诉求。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def _seed(self) -> list[str]:
        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(40)]
        self.store.save_bars([bar("sh600000", d) for d in days])
        self.store.save_bars([bar("sz000001", d) for d in days])
        self.store.save_signals(
            [
                make_signal("sh600000", SignalKind.FIRST_BUY, days[39]),
                make_signal("sz000001", SignalKind.SECOND_BUY, days[38]),
                make_signal("sz000001", SignalKind.THIRD_BUY, days[39]),
            ]
        )
        return days

    def test_candidates_and_pools_have_identical_counts(self) -> None:
        self._seed()
        candidates = self.service.candidates(recent_days=3)
        pools = self.service.pools(days=3)

        for key in ("first_buy", "second_buy", "third_buy"):
            self.assertEqual(
                len(candidates[key]),
                len(pools[key]),
                f"{key} 数量不一致：candidates={len(candidates[key])} pools={len(pools[key])}",
            )

    def test_candidates_and_pools_have_identical_keys(self) -> None:
        self._seed()
        candidates = self.service.candidates(recent_days=3)
        pools = self.service.pools(days=3)

        for key in ("first_buy", "second_buy", "third_buy"):
            lhs = {(r["symbol"], r["kind"], r["date"]) for r in candidates[key]}
            rhs = {(r["symbol"], r["kind"], r["date"]) for r in pools[key]}
            self.assertEqual(lhs, rhs, f"{key} 集合不一致：仅候选 {lhs - rhs}，仅池 {rhs - lhs}")

    def test_window_is_trading_days_not_calendar_days(self) -> None:
        """窗口必须是交易日。自然日窗口在周末/长假会漂，用户无法跨页签对照。"""

        self._seed()
        out = self.service.candidates(recent_days=5)
        self.assertIn("trading_dates", out)
        self.assertLessEqual(len(out["trading_dates"]), 5)
        self.assertEqual(out["source"], "signals")
        self.assertEqual(out["dedup"], "same_symbol_kind_latest")

    def test_same_symbol_kind_keeps_latest_only(self) -> None:
        """同股票同类型只留最近一条——与 pools 的去重规则一致。"""

        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(40)]
        self.store.save_bars([bar("sh600000", d) for d in days])
        self.store.save_signals(
            [
                make_signal("sh600000", SignalKind.FIRST_BUY, days[37], price=9.0),
                make_signal("sh600000", SignalKind.FIRST_BUY, days[39], price=10.0),
            ]
        )
        out = self.service.candidates(recent_days=5)
        self.assertEqual(len(out["first_buy"]), 1)
        self.assertEqual(out["first_buy"][0]["date"], days[39])


class PoolSystemScoreTest(unittest.TestCase):
    """三日池的胜算列要挂体系分，且与体系选股同 key 同分。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def _seed(self, with_rebuild: bool = True) -> list[str]:
        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(40)]
        for sym, close in (("sh600000", 10.0), ("sz000001", 12.0)):
            self.store.save_bars([bar(sym, d, close) for d in days])
        self.store.save_signals(
            [
                make_signal("sh600000", SignalKind.FIRST_BUY, days[39]),
                make_signal("sz000001", SignalKind.THIRD_BUY, days[39]),
            ]
        )
        if with_rebuild:
            self.service.rebuild_outcomes("day")
        return days

    def test_pool_rows_carry_system_score(self) -> None:
        self._seed()
        out = self.service.pools(days=3)
        rows = out["first_buy"] + out["second_buy"] + out["third_buy"]
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertIn("system_score", row)
            self.assertIn("system_hits", row)
            self.assertIn("system_available", row)
            self.assertTrue(row["system_available"], f'{row["symbol"]} 没挂上体系分')
            self.assertIsInstance(row["system_score"], float)

    def test_pool_has_rate_badge_too(self) -> None:
        """胜算列是「体系分 / 胜率」两段，胜率徽章不能因为加了体系分就丢掉。"""

        self._seed()
        out = self.service.pools(days=3)
        for row in out["first_buy"]:
            self.assertIn("rate", row)
            self.assertIn("rate_value", row)

    def test_pool_score_matches_system_pick(self) -> None:
        """同 key 同分——池子和选股页用的是同一套引擎 + 同一个快照。"""

        self._seed()
        pools = self.service.pools(days=3)
        pick = self.service.system_pick(days=3, min_score=-999.0)
        pool_scores = {
            (r["symbol"], r["kind"], r["date"]): r["system_score"]
            for key in ("first_buy", "second_buy", "third_buy")
            for r in pools[key]
            if r["system_available"]
        }
        self.assertGreater(len(pool_scores), 0)
        checked = 0
        for item in pick["items"]:
            key = (item["symbol"], item["kind"], item["signal_date"])
            if key in pool_scores:
                checked += 1
                self.assertAlmostEqual(pool_scores[key], item["score"], places=9)
        self.assertGreater(checked, 0, "池子与选股页没有任何共同 key，口径已漂移")

    def test_pool_score_absent_without_rebuild(self) -> None:
        """没 rebuild 时不能伪造 0 分——0 分有明确含义（等价于基准）。"""

        self._seed(with_rebuild=False)
        out = self.service.pools(days=3)
        rows = out["first_buy"] + out["second_buy"] + out["third_buy"]
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertFalse(row["system_available"])
            self.assertIsNone(row["system_score"])

    def test_system_pick_dedups_by_symbol_kind(self) -> None:
        """体系选股与池子同样去重，否则同股票两个买点会让条数多一条。"""

        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(40)]
        self.store.save_bars([bar("sh600000", d) for d in days])
        self.store.save_signals(
            [
                make_signal("sh600000", SignalKind.FIRST_BUY, days[38]),
                make_signal("sh600000", SignalKind.FIRST_BUY, days[39]),
            ]
        )
        self.service.rebuild_outcomes("day")
        pick = self.service.system_pick(days=3, min_score=-999.0)
        firsts = [it for it in pick["items"] if it["kind"] == SignalKind.FIRST_BUY.value]
        self.assertEqual(len(firsts), 1, "同股票同类型应只留最新一条")
        self.assertEqual(firsts[0]["signal_date"], days[39])


class ScanHitsIdentityTest(unittest.TestCase):
    """scan_hits 唯一键必须含 trade_date，否则历史买点被覆盖。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "t.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def test_unique_key_includes_trade_date(self) -> None:
        cols = Store._unique_index_columns(self.store._conn, "scan_hits")
        self.assertIn(("run_id", "symbol", "kind", "trade_date"), cols)

    def test_multiple_dates_per_symbol_kind_are_kept(self) -> None:
        self.store.create_run("run-1", scope="all", total=1)
        self.store.save_hits(
            "run-1",
            [
                {"symbol": "sh600000", "kind": "FIRST_BUY", "date": "2026-01-05",
                 "price": 9.0, "score": 0.2, "name": "浦发"},
                {"symbol": "sh600000", "kind": "FIRST_BUY", "date": "2026-03-10",
                 "price": 10.0, "score": 0.3, "name": "浦发"},
            ],
        )
        hits = self.store.hits_for_run("run-1")
        self.assertEqual(len(hits), 2, "同一股票同类的两个历史买点都被覆盖了")
        self.assertEqual({h["date"] for h in hits}, {"2026-01-05", "2026-03-10"})

    def test_migration_is_idempotent(self) -> None:
        cols_before = Store._unique_index_columns(self.store._conn, "scan_hits")
        self.store.close()
        again = Store(self.path)
        self.addCleanup(again.close)
        cols_after = Store._unique_index_columns(again._conn, "scan_hits")
        self.assertEqual(cols_before, cols_after)


class ZombieRecordTest(unittest.TestCase):
    """二买考验期满守住 → 归档 SECOND_BUY_OK，并允许新一买开新一轮。"""

    @staticmethod
    def make_bars(prices: list[float]) -> list[Bar]:
        base = date(2024, 1, 1)
        return [
            Bar(
                symbol="sh600000",
                date=(base + timedelta(i)).isoformat(),
                open=p,
                high=p,
                low=p,
                close=p,
                volume=1_000_000,
                amount=0.0,
                period="day",
            )
            for i, p in enumerate(prices)
        ]

    def test_second_buy_ok_is_not_tracking(self) -> None:
        self.assertNotIn(WatchState.SECOND_BUY_OK, TRACKING_STATES)

    def test_second_buy_ok_archives_after_window(self) -> None:
        manager = WatchlistManager(ChanConfig(max_wait_bars=5))
        existing = [
            WatchItem(
                symbol="sh600000",
                state=WatchState.OPPORTUNITY,
                first_buy_date="2024-01-03",
                first_buy_index=2,
                first_buy_price=90.0,
                first_buy_low=85.0,
                last_update_date="2024-01-10",
                second_buy_date="2024-01-09",
                second_buy_price=80.0,
                meta={"second_buy_low": 76.0, "second_buy_index": 8},
            )
        ]
        # 二买在 index 8，窗口 5 → index 14 起超窗口，且全程守住 76
        prices = [100, 95, 90, 88, 86, 85, 84, 83, 80, 82, 84, 86, 88, 90, 92, 94]
        items = manager.update(bars=self.make_bars(prices), signals=[], existing=existing)

        item = next(it for it in items if it.symbol == "sh600000")
        self.assertEqual(item.state, WatchState.SECOND_BUY_OK)
        self.assertIn("守住二买低点", item.exit_reason or "")

    def test_newer_first_buy_opens_new_round(self) -> None:
        """僵尸记录归档后，同一批 signals 里更新的一买要立刻开新一轮。"""

        manager = WatchlistManager(ChanConfig(max_wait_bars=5))
        existing = [
            WatchItem(
                symbol="sh600000",
                state=WatchState.OPPORTUNITY,
                first_buy_date="2024-01-03",
                first_buy_index=2,
                first_buy_price=90.0,
                first_buy_low=85.0,
                last_update_date="2024-01-10",
                second_buy_date="2024-01-09",
                second_buy_price=80.0,
                meta={"second_buy_low": 76.0, "second_buy_index": 8},
            )
        ]
        prices = [100, 95, 90, 88, 86, 85, 84, 83, 80, 82, 84, 86, 88, 90, 92, 94]
        bars = self.make_bars(prices)
        newer = Signal(
            symbol="sh600000",
            kind=SignalKind.FIRST_BUY,
            date=bars[15].date,
            bar_index=15,
            price=bars[15].close,
            anchor_index=14,
            anchor_price=bars[14].low,
            message="new round",
            meta={},
            period="day",
        )
        items = manager.update(bars=bars, signals=[newer], existing=existing)

        states = {it.state for it in items if it.symbol == "sh600000"}
        self.assertIn(WatchState.SECOND_BUY_OK, states, "旧轮次没有归档")
        self.assertIn(WatchState.WAIT_SECOND_BUY, states, "新一买没有开启新一轮")

        active = [it for it in items if it.symbol == "sh600000" and it.state in TRACKING_STATES]
        self.assertEqual(len(active), 1, "跟踪中的记录必须只有一条")
        self.assertEqual(active[0].first_buy_date, bars[15].date)


class WatchlistStatsSecondOkTest(unittest.TestCase):
    """二买守住要计入二买成功，不能因为归档了就从成功率里消失。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def test_second_buy_ok_counts_as_success(self) -> None:
        self.store.save_watch_items(
            [
                WatchItem(
                    symbol="sh600000",
                    state=WatchState.SECOND_BUY_OK,
                    first_buy_date="2024-01-03",
                    first_buy_index=2,
                    first_buy_price=90.0,
                    first_buy_low=85.0,
                    last_update_date="2024-02-01",
                    second_buy_date="2024-01-09",
                    second_buy_price=80.0,
                ),
                WatchItem(
                    symbol="sz000001",
                    state=WatchState.SECOND_BUY_FAILED,
                    first_buy_date="2024-01-03",
                    first_buy_index=2,
                    first_buy_price=90.0,
                    first_buy_low=85.0,
                    last_update_date="2024-02-01",
                    second_buy_date="2024-01-09",
                    second_buy_price=80.0,
                ),
            ]
        )
        stats = self.service.watchlist_stats()
        self.assertEqual(stats["second_buy"]["success"], 1)
        self.assertEqual(stats["second_buy"]["ok"], 1)
        self.assertEqual(stats["second_buy"]["confirmed"], 2)
        self.assertAlmostEqual(stats["second_buy"]["rate"], 0.5)
        # 一买成功 = 走到二买确认（守住或失败都算）
        self.assertEqual(stats["first_buy"]["success"], 2)


if __name__ == "__main__":
    unittest.main()

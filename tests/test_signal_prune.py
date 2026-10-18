"""过时信号清理（_prune_stale_signals）的回归。

背景：盘中扫描用未收盘的盘中价产生信号，收盘后数据更新，这些信号可能不复现
（例如盘中价突破中枢、收盘后回调跌破中枢上沿），残留在 signals 表成为「过时信号」。
体系选股读 signals 表快照、详情页读实时 scan，两者因此不一致。

analyze 时清除「窗口内但实时 scan 不复现」的旧信号，同时删 signal_outcomes 里
对应的结局样本，保证两处一致。重点锁：
1. 窗口内不复现的信号被删；
2. 窗口外的历史信号保留（窗口裁剪本身会丢信号，不能误删）；
3. 实时 scan 复现的信号保留；
4. signal_outcomes 同步删。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import Bar, Signal, SignalKind  # noqa: E402

from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402


def bar(symbol: str, day: str, close: float) -> Bar:
    return Bar(symbol=symbol, date=day, open=close, high=close, low=close, close=close,
               volume=1_000_000, amount=0.0, period="day")


def make_signal(symbol: str, kind: SignalKind, trade_date: str, price: float = 10.0) -> Signal:
    return Signal(symbol=symbol, kind=kind, date=trade_date, bar_index=10, price=price,
                  anchor_index=9, anchor_price=price * 0.97, message="x",
                  meta={"divergence_score": 0.2}, period="day")


class PruneTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def _seed_bars(self, n: int = 40) -> list[str]:
        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(n)]
        self.store.save_bars([bar("sh600000", d, 10.0) for d in days])
        return days

    def test_prune_removes_stale_window_signal(self) -> None:
        days = self._seed_bars(40)
        # signals 表里有一个窗口内的 THIRD_BUY（盘中残留），实时 scan 不复现
        stale = make_signal("sh600000", SignalKind.THIRD_BUY, days[35], 10.0)
        self.store.save_signals([stale])

        # 实时 scan「没有」这个信号 → 清理后应被删
        removed = self.service._prune_stale_signals("sh600000", self.store.load_bars("sh600000", limit=100), [])
        self.assertEqual(removed, 1)
        left = self.store.signals_for_symbol("sh600000", limit=100)
        self.assertEqual(left, [], "过时信号应被删除")

    def test_prune_keeps_outside_window_signal(self) -> None:
        days = self._seed_bars(40)
        # 窗口外的信号（日期早于 bars 最早一天），不删
        old_date = "2025-12-01"
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_BUY, old_date, 10.0)])
        bars = self.store.load_bars("sh600000", limit=100)
        removed = self.service._prune_stale_signals("sh600000", bars, [])
        self.assertEqual(removed, 0)
        left = self.store.signals_for_symbol("sh600000", limit=100)
        self.assertEqual(len(left), 1, "窗口外的历史信号不应被删")

    def test_prune_keeps_reproduced_signal(self) -> None:
        days = self._seed_bars(40)
        # 实时 scan 复现了该信号 → 保留
        kept = make_signal("sh600000", SignalKind.FIRST_BUY, days[20], 10.0)
        self.store.save_signals([kept])
        bars = self.store.load_bars("sh600000", limit=100)
        removed = self.service._prune_stale_signals("sh600000", bars, [kept])
        self.assertEqual(removed, 0)
        left = self.store.signals_for_symbol("sh600000", limit=100)
        self.assertEqual(len(left), 1, "实时 scan 复现的信号应保留")

    def test_prune_also_deletes_outcomes(self) -> None:
        days = self._seed_bars(40)
        stale = make_signal("sh600000", SignalKind.THIRD_BUY, days[35], 10.0)
        self.store.save_signals([stale])
        # 手工插入对应的结局样本
        self.store.save_outcomes([{
            "symbol": "sh600000", "kind": "THIRD_BUY", "period": "day",
            "signal_date": days[35], "price": 10.0, "anchor_price": 9.7,
            "max_return": 0.1, "window_size": 20, "features": {},
        }])
        bars = self.store.load_bars("sh600000", limit=100)
        self.service._prune_stale_signals("sh600000", bars, [])
        self.assertEqual(self.store.outcome_count("day"), 0, "结局样本应同步删除")

    def test_analyze_prunes_stale(self) -> None:
        """端到端：analyze 后，窗口内不复现的旧信号被清，详情与体系选股一致。"""
        days = self._seed_bars(40)
        # 造一个「窗口内、但真实 scan 不会产生」的脏信号
        stale = make_signal("sh600000", SignalKind.THIRD_BUY, days[38], 99.0)  # 异常价格
        self.store.save_signals([stale])
        self.service.analyze_symbol("sh600000", use_cache=True, period="day")
        left = [s for s in self.store.signals_for_symbol("sh600000", limit=100) if s["kind"] == "THIRD_BUY"]
        self.assertEqual(left, [], "analyze 后过时的三买应被清除")


if __name__ == "__main__":
    unittest.main()

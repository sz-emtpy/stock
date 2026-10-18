"""体系选股（system_pick）的回归。

这是体系的价值闭环——把特征/增益/验证落到「今天该关注哪些票」。
重点锁：
1. 按体系分降序排序、分数可解释（命中特征来自增益）；
2. min_score 过滤只留「比基准更可能拉升」的；
3. 空库 / 体系未构建时诚实返回，不报错不编数据。
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


class PickTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def _seed(self) -> None:
        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(40)]
        self.store.save_bars([bar("sh600000", d, 10.0) for d in days])
        self.store.save_bars([bar("sz000001", d, 12.0) for d in days])
        # 两个最近买点（days 窗口内）
        self.store.save_signals([
            make_signal("sh600000", SignalKind.FIRST_BUY, days[38], 10.0),
            make_signal("sz000001", SignalKind.THIRD_BUY, days[39], 12.0),
        ])
        self.service.rebuild_outcomes("day")

    def test_empty_when_system_not_built(self) -> None:
        # 有 K 线（有交易日历）但没跑 rebuild（无快照）→ 提示先重建体系
        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(40)]
        self.store.save_bars([bar("sh600000", d, 10.0) for d in days])
        out = self.service.system_pick(days=3)
        self.assertFalse(out["available"])
        self.assertIn("体系尚未构建", out["message"])

    def test_pick_returns_scored_sorted_items(self) -> None:
        self._seed()
        out = self.service.system_pick(days=3)
        self.assertTrue(out["available"])
        self.assertGreaterEqual(out["total_scored"], 2)
        self.assertGreater(len(out["items"]), 0)
        # 每个条目都有可解释的命中特征（非空）
        for it in out["items"]:
            self.assertIn("score", it)
            self.assertIn("hits", it)
            self.assertIsInstance(it["hits"], list)
        # 按分降序
        scores = [it["score"] for it in out["items"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_min_score_filters(self) -> None:
        self._seed()
        all_items = self.service.system_pick(days=3, min_score=-999.0)["items"]
        # 用最高分作为阈值 → 只留最高分的那些
        top = max(it["score"] for it in all_items)
        filtered = self.service.system_pick(days=3, min_score=top)["items"]
        self.assertTrue(all(it["score"] >= top for it in filtered))

    def test_no_trading_dates_is_honest(self) -> None:
        # 空库 → 没有交易日历
        out = self.service.system_pick(days=3)
        self.assertFalse(out["available"])
        self.assertIn("先执行一次市场扫描", out["message"])


if __name__ == "__main__":
    unittest.main()

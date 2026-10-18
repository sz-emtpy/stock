"""买点结局样本库（自成长体系 M1）的回归测试。

核心口径（不可动摇）：
1. 「赚钱」用三重结局量化：max_return（最大涨幅）/ hold_return（期末收益）
   / max_drawdown（最大回撤），全部相对信号价，不是二元赚/亏。
2. **防未来函数**：特征一律只用「信号日及之前」的数据。结局只用「信号日之后」
   的 K 线。两者方向相反、互不污染。
3. 无后续 K 线的信号（最新交易日附近的）结局留 NULL，不能当成 0 或失败。
4. rebuild 幂等：重复重建不产生重复行。
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


def make_signal(symbol: str, kind: SignalKind, trade_date: str, price: float = 10.0) -> Signal:
    return Signal(
        symbol=symbol,
        kind=kind,
        date=trade_date,
        bar_index=10,
        price=price,
        anchor_index=9,
        anchor_price=price * 0.97,
        message="x",
        meta={"divergence_score": 0.3},
        period="day",
    )


def bar(symbol: str, day: str, close: float, volume: float = 1_000_000) -> Bar:
    """简化 K 线：open=high=low=close=close（一字线），方便精确验证结局。"""

    return Bar(symbol=symbol, date=day, open=close, high=close, low=close, close=close,
               volume=volume, amount=0.0, period="day")


class OutcomeTest(unittest.TestCase):
    """结局计算：构造已知后续走势，验证三重结局精确值。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def _seed_exact(self) -> None:
        base = date(2026, 1, 1)
        bars = [bar("sh600000", (base + timedelta(i)).isoformat(), 10.0) for i in range(10)]
        future = [11.0, 12.0, 13.0, 9.0, 8.0, 9.0, 10.0, 10.0, 11.0, 11.0,
                  10.0, 10.0, 9.0, 10.0, 11.0, 11.0, 12.0, 12.0, 12.0, 11.0]
        for i, close in enumerate(future):
            bars.append(bar("sh600000", (base + timedelta(10 + i)).isoformat(), close))
        self.store.save_bars(bars)
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_BUY, "2026-01-10", 10.0)])

    def test_triple_outcome_exact(self) -> None:
        self._seed_exact()
        self.service.rebuild_outcomes("day")
        row = self.store.load_outcomes(kind="FIRST_BUY")[0]
        # 信号价 10；后续 20 根 close 最大 13、最小 8、最后一根 11
        self.assertAlmostEqual(row["max_return"], 0.3, places=4)
        self.assertAlmostEqual(row["hold_return"], 0.1, places=4)
        self.assertAlmostEqual(row["max_drawdown"], -0.2, places=4)
        self.assertEqual(row["days_to_peak"], 3)   # 13.0 出现在第 3 根
        self.assertEqual(row["window_size"], 20)

    def test_no_future_bars_leaves_outcome_null(self) -> None:
        """信号是最新一根（无后续 K 线）→ 结局留 NULL，不是 0 也不是失败。"""

        base = date(2026, 1, 1)
        bars = [bar("sh600000", (base + timedelta(i)).isoformat(), 10.0) for i in range(10)]
        self.store.save_bars(bars)
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_BUY, "2026-01-10", 10.0)])
        self.service.rebuild_outcomes("day")
        row = self.store.load_outcomes(kind="FIRST_BUY")[0]
        self.assertIsNone(row["max_return"])
        self.assertIsNone(row["hold_return"])
        self.assertIsNone(row["max_drawdown"])
        self.assertEqual(row["window_size"], 0)

    def test_partial_window_uses_what_exists(self) -> None:
        """信号后只有 3 根 → window_size=3，结局基于这 3 根算。"""

        base = date(2026, 1, 1)
        bars = [bar("sh600000", (base + timedelta(i)).isoformat(), 10.0) for i in range(10)]
        for i, close in enumerate([12.0, 11.0, 10.5]):
            bars.append(bar("sh600000", (base + timedelta(10 + i)).isoformat(), close))
        self.store.save_bars(bars)
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_BUY, "2026-01-10", 10.0)])
        self.service.rebuild_outcomes("day")
        row = self.store.load_outcomes(kind="FIRST_BUY")[0]
        self.assertEqual(row["window_size"], 3)
        self.assertAlmostEqual(row["max_return"], 0.2, places=4)   # 12 / 10 - 1


class FeatureTest(unittest.TestCase):
    """特征提取：量比 / 回撤 / 均线位置 / 结构特征，且防未来函数。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def test_vol_ratio_computed(self) -> None:
        """信号日量 300 万，前 20 根均量 100 万 → vol_ratio = 3.0。"""

        base = date(2026, 1, 1)
        bars = [bar("sh600000", (base + timedelta(i)).isoformat(), 10.0, volume=1_000_000) for i in range(30)]
        # 信号日在第 25 根（01-25），把它的量改成 300 万
        bars[24] = bar("sh600000", "2026-01-25", 10.0, volume=3_000_000)
        self.store.save_bars(bars)
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_BUY, "2026-01-25", 10.0)])
        self.service.rebuild_outcomes("day")
        row = self.store.load_outcomes(kind="FIRST_BUY")[0]
        self.assertAlmostEqual(row["features"]["vol_ratio"], 3.0, places=4)

    def test_retrace_from_high(self) -> None:
        """前 60 日最高 20，信号价 10 → retrace_from_high = 0.5。"""

        base = date(2026, 1, 1)
        bars = [bar("sh600000", (base + timedelta(i)).isoformat(), 10.0) for i in range(30)]
        bars[5] = bar("sh600000", "2026-01-06", 20.0)   # 历史高点
        self.store.save_bars(bars)
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_BUY, "2026-01-25", 10.0)])
        self.service.rebuild_outcomes("day")
        row = self.store.load_outcomes(kind="FIRST_BUY")[0]
        self.assertAlmostEqual(row["features"]["retrace_from_high"], 0.5, places=4)

    def test_above_ma20_flag(self) -> None:
        """信号价站上前 20 日 MA20 → above_ma20 = 1。"""

        base = date(2026, 1, 1)
        # 前 20 根 close=9，信号日 close=10 → MA20≈9.05 < 10 → above_ma20=1
        bars = [bar("sh600000", (base + timedelta(i)).isoformat(), 9.0) for i in range(20)]
        bars.append(bar("sh600000", "2026-01-21", 10.0))
        self.store.save_bars(bars)
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_BUY, "2026-01-21", 10.0)])
        self.service.rebuild_outcomes("day")
        row = self.store.load_outcomes(kind="FIRST_BUY")[0]
        self.assertEqual(row["features"]["above_ma20"], 1)

    def test_structural_feature_from_meta(self) -> None:
        base = date(2026, 1, 1)
        bars = [bar("sh600000", (base + timedelta(i)).isoformat(), 10.0) for i in range(30)]
        self.store.save_bars(bars)
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_BUY, "2026-01-25", 10.0)])
        self.service.rebuild_outcomes("day")
        row = self.store.load_outcomes(kind="FIRST_BUY")[0]
        self.assertAlmostEqual(row["features"]["divergence_score"], 0.3, places=4)

    def test_features_ignore_future_bars(self) -> None:
        """防未来函数：未来 K 线完全不同，特征必须一模一样。"""

        base = date(2026, 1, 1)
        head = [bar("sh600000", (base + timedelta(i)).isoformat(), 10.0) for i in range(10)]
        future_a = [bar("sh600000", (base + timedelta(10 + i)).isoformat(), 12.0) for i in range(10)]
        future_b = [bar("sh600000", (base + timedelta(10 + i)).isoformat(), 5.0) for i in range(10)]
        self.store.save_bars(head + future_a)

        sig = make_signal("sh600000", SignalKind.FIRST_BUY, "2026-01-10", 10.0)
        dates_a = [b.date for b in head + future_a]
        dates_b = [b.date for b in head + future_b]
        feats_a = self.service._extract_features(
            {"date": "2026-01-10", "price": 10.0, "meta": {"divergence_score": 0.3}},
            head + future_a, dates_a, set(dates_a), self.service._indicator_cache(head + future_a),
        )
        feats_b = self.service._extract_features(
            {"date": "2026-01-10", "price": 10.0, "meta": {"divergence_score": 0.3}},
            head + future_b, dates_b, set(dates_b), self.service._indicator_cache(head + future_b),
        )
        self.assertEqual(feats_a, feats_b, "特征不能被未来 K 线影响")


class RebuildTest(unittest.TestCase):
    """幂等与聚合统计。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def _seed(self) -> None:
        base = date(2026, 1, 1)
        bars = [bar("sh600000", (base + timedelta(i)).isoformat(), 10.0) for i in range(30)]
        self.store.save_bars(bars)
        self.store.save_signals([
            make_signal("sh600000", SignalKind.FIRST_BUY, "2026-01-05", 10.0),
            make_signal("sh600000", SignalKind.FIRST_BUY, "2026-01-10", 10.0),
            make_signal("sh600000", SignalKind.SECOND_BUY, "2026-01-15", 10.0),
        ])

    def test_rebuild_is_idempotent(self) -> None:
        self._seed()
        first = self.service.rebuild_outcomes("day")
        second = self.service.rebuild_outcomes("day")
        self.assertEqual(first["outcomes"], second["outcomes"])
        self.assertEqual(self.store.outcome_count("day"), first["outcomes"])
        # 重复重建不产生重复行（主键 upsert）
        self.assertEqual(self.store.outcome_count("day"), 3)

    def test_stats_grouped_by_kind(self) -> None:
        self._seed()
        self.service.rebuild_outcomes("day")
        stats = self.store.outcome_stats("day")
        self.assertIn("FIRST_BUY", stats)
        self.assertIn("SECOND_BUY", stats)
        self.assertEqual(stats["FIRST_BUY"]["count"], 2)
        self.assertEqual(stats["SECOND_BUY"]["count"], 1)
        # 每类都有 closed（有后续 K 线的样本数）与 win（max_return>0 数）
        self.assertGreater(stats["FIRST_BUY"]["closed"], 0)

    def test_empty_library_is_honest(self) -> None:
        out = self.service.rebuild_outcomes("day")
        self.assertEqual(out["outcomes"], 0)
        self.assertIn("还没有", out["message"])

    def test_non_day_period_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.rebuild_outcomes("m30")


if __name__ == "__main__":
    unittest.main()

"""市场情绪（跨符号）+ 个股历史成功率（跨信号）特征的回归。

市场情绪从全市场 bars 用 SQL 窗口函数算，是本体系「情绪市场」特征的底座。
重点锁：
1. 宽度 / 涨停 / 平均涨跌幅 / 指数涨跌幅的数值正确性；
2. 个股历史成功率是**时序**的——只用信号日之前已终结的一买，防未来函数；
3. 情绪特征用信号日当天数据（信号在收盘后才确认，当天数据已可得，不算未来）。
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


def bar(symbol: str, day: str, close: float, volume: float = 1_000_000) -> Bar:
    return Bar(symbol=symbol, date=day, open=close, high=close, low=close, close=close,
               volume=volume, amount=0.0, period="day")


def make_signal(symbol: str, kind: SignalKind, trade_date: str, price: float = 10.0) -> Signal:
    return Signal(symbol=symbol, kind=kind, date=trade_date, bar_index=10, price=price,
                  anchor_index=9, anchor_price=price * 0.97, message="x",
                  meta={"divergence_score": 0.2}, period="day")


class MarketBreadthTest(unittest.TestCase):
    """Store.compute_market_breadth 的数值正确性。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def test_breadth_limit_up_and_index(self) -> None:
        d1 = "2026-10-13"
        d2 = "2026-10-14"
        # 三只票：day1 都是 10，day2 一只涨 10%（涨停）、一只跌 10%、一只平盘
        for sym, c2 in (("sh600000", 11.0), ("sh600001", 9.0), ("sz000001", 10.0)):
            self.store.save_bars([bar(sym, d1, 10.0), bar(sym, d2, c2)])
        # 指数：day1=3000, day2=3030 → +1%（指数存 fq='ref'，与个股 qfq 隔离）
        self.store.save_bars(
            [bar("sh000001", d1, 3000.0), bar("sh000001", d2, 3030.0)], fq="ref"
        )

        self.store.compute_market_breadth()
        row = self.store.load_market_breadth(d2)
        self.assertIsNotNone(row)
        self.assertEqual(row["total"], 3)
        self.assertEqual(row["up_count"], 1)
        self.assertEqual(row["down_count"], 1)
        self.assertEqual(row["flat_count"], 1)
        self.assertEqual(row["limit_up"], 1)   # 只有 sh600000 涨 10% ≥ 9.5%
        # 平均涨跌幅 = (0.1 + (-0.1) + 0) / 3 = 0
        self.assertAlmostEqual(row["avg_change"], 0.0, places=4)
        self.assertAlmostEqual(row["index_change"], 0.01, places=4)

    def test_idempotent_recompute(self) -> None:
        self.store.save_bars([bar("sh600000", "2026-10-13", 10.0), bar("sh600000", "2026-10-14", 11.0)])
        self.store.compute_market_breadth()
        n1 = self.store.market_breadth_count()
        self.store.compute_market_breadth()
        self.assertEqual(self.store.market_breadth_count(), n1, "重算不应产生重复行")


class SentimentFeatureTest(unittest.TestCase):
    """情绪特征与个股历史成功率特征写入 signal_outcomes。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def _seed(self) -> None:
        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(30)]
        # 一只票 30 根日线（信号在中间，前后都有 K 线）
        bars = [bar("sh600000", d, 10.0) for d in days]
        # 另两只票提供市场宽度（day2 那天一只涨一只跌，让情绪非 50%）
        self.store.save_bars(bars)
        self.store.save_bars([bar("sh600001", d, 10.0 if i < 1 else (11.0 if i == 1 else 10.0))
                              for i, d in enumerate(days)])
        self.store.save_bars([bar("sz000001", d, 10.0 if i < 1 else (9.0 if i == 1 else 10.0))
                              for i, d in enumerate(days)])
        # 指数
        self.store.save_bars(
            [bar("sh000001", d, 3000.0 if i < 1 else 3030.0) for i, d in enumerate(days)],
            fq="ref",
        )
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_BUY, days[10], 10.0)])

    def test_sentiment_features_present(self) -> None:
        self._seed()
        self.store.compute_market_breadth()
        self.service.rebuild_outcomes("day")
        row = self.store.load_outcomes(kind="FIRST_BUY")[0]
        feats = row["features"]
        self.assertIn("market_breadth", feats)
        self.assertIn("market_limit_up", feats)
        self.assertIn("market_avg_change", feats)
        self.assertIn("market_index_change", feats)
        # 指数涨跌幅数值正确性由 MarketBreadthTest 覆盖，这里只确认特征已挂上


class PriorWinRateTest(unittest.TestCase):
    """个股历史成功率：时序累计，防未来函数（只用信号日之前的一买）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def test_prior_win_rate_is_sequential(self) -> None:
        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(40)]
        self.store.save_bars([bar("sh600000", d, 10.0) for d in days])
        # 三个一买：day5（成功，day8 有二买）、day15（失败）、day25（失败）
        self.store.save_signals([
            make_signal("sh600000", SignalKind.FIRST_BUY, days[5], 10.0),
            make_signal("sh600000", SignalKind.SECOND_BUY, days[8], 10.0),
            make_signal("sh600000", SignalKind.FIRST_BUY, days[15], 10.0),
            make_signal("sh600000", SignalKind.FIRST_BUY, days[25], 10.0),
        ])
        self.service.rebuild_outcomes("day")

        rows = sorted(
            self.store.load_outcomes(kind="FIRST_BUY"), key=lambda r: r["signal_date"]
        )
        # day5 是第一个一买：还没有历史 → prior 特征缺失
        self.assertNotIn("symbol_prior_win_rate", rows[0]["features"])
        # day15 一买：之前只有 day5 那个（成功）→ 成功率 1.0
        self.assertEqual(rows[1]["features"]["symbol_prior_win_rate"], 1.0)
        # day25 一买：之前 day5 成功 + day15 失败 → 0.5
        self.assertEqual(rows[2]["features"]["symbol_prior_win_rate"], 0.5)

    def test_prior_win_rate_reflects_failure(self) -> None:
        """一买失败也要正确计入历史成功率（不能只看成功）。"""

        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(45)]
        self.store.save_bars([bar("sh600000", d, 10.0) for d in days])
        # day5 一买：day36 才出二买（间隔 31 交易日 > 30 窗口）→ 失败
        # day20 一买：day36 二买（间隔 16 交易日）→ 成功
        self.store.save_signals([
            make_signal("sh600000", SignalKind.FIRST_BUY, days[5], 10.0),
            make_signal("sh600000", SignalKind.FIRST_BUY, days[20], 10.0),
            make_signal("sh600000", SignalKind.SECOND_BUY, days[36], 10.0),
        ])
        self.service.rebuild_outcomes("day")
        rows = sorted(self.store.load_outcomes(kind="FIRST_BUY"), key=lambda r: r["signal_date"])
        # day20 一买的 prior = day5 一买的结果 = 失败 → 0.0
        self.assertEqual(rows[1]["features"]["symbol_prior_win_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()

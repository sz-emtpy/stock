"""体系 v2（2026-10-18 体检报告改造）的回归。

体检报告的三个致命盲区对应的修复：
1. 标签错位 → 规则化出场净收益（net_exit_return）做标签，出场 = 窗口内首个卖点，
   否则强平；扣往返成本。
2. 小样本噪声 → 每箱增益带 Wilson 95% 下界（gain_lb），打分用下界。
3. 牛市 beta → validate_by_regime 按市场状态分段验证，全状态有 edge 才算 robust。

另外锁死：出场候选只看 EXIT_WINDOW_BARS 窗口内（窗口外卖点与本笔无关），
这是冒烟测试抓到的真实 bug（窗口外卖点把强平覆盖成 +47% 的卖点出场）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import Bar, SignalKind  # noqa: E402

from backend.service import (  # noqa: E402
    EXIT_WINDOW_BARS,
    TRADE_COST_ROUNDTRIP,
    ScanService,
)
from backend.store import Store  # noqa: E402
from backend.trading_system import (  # noqa: E402
    DEFAULT_WIN_THRESHOLD,
    feature_gains,
    label_win,
    market_regime,
    score_signal,
    validate_by_regime,
    validate_time_split,
    wilson_lower_bound,
)


def bar(symbol: str, day: str, close: float, volume: float = 1_000_000) -> Bar:
    return Bar(
        symbol=symbol,
        date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        amount=0.0,
        period="day",
    )


class WilsonLBTest(unittest.TestCase):
    def test_small_samples_get_discounted(self) -> None:
        # 同样 50% 胜率，样本越大下界越接近真实值
        lb30 = wilson_lower_bound(15, 30)
        lb1000 = wilson_lower_bound(500, 1000)
        self.assertLess(lb30, 0.40)
        self.assertGreater(lb1000, 0.46)
        self.assertLess(lb1000, 0.53)

    def test_perfect_small_sample_still_below_one(self) -> None:
        # 3/3 全胜的下界必须显著低于 1——这正是小样本箱不许拿满分的原因
        self.assertLess(wilson_lower_bound(3, 3), 0.8)

    def test_zero_samples(self) -> None:
        self.assertEqual(wilson_lower_bound(0, 0), 0.0)


class LabelWinV2Test(unittest.TestCase):
    def test_uses_net_exit_return_when_present(self) -> None:
        self.assertTrue(label_win({"net_exit_return": 0.06}))
        self.assertFalse(label_win({"net_exit_return": 0.04}))
        self.assertFalse(label_win({"net_exit_return": -0.02}))

    def test_threshold_is_five_percent(self) -> None:
        self.assertEqual(DEFAULT_WIN_THRESHOLD, 0.05)

    def test_fallback_to_v1_label_for_legacy_rows(self) -> None:
        # 老样本没有出场字段 → 退回 max_return（10% 语义）
        self.assertTrue(label_win({"max_return": 0.12}))
        self.assertFalse(label_win({"max_return": 0.08}))

    def test_unterminated_row_is_not_win(self) -> None:
        self.assertFalse(label_win({"net_exit_return": None, "max_return": None}))


class FeatureGainsLBTest(unittest.TestCase):
    def test_buckets_carry_lower_bound_gain(self) -> None:
        rows = []
        for i in range(60):
            rows.append(
                {
                    "signal_date": f"2026-01-{i % 28 + 1:02d}",
                    "kind": "FIRST_BUY",
                    "max_return": 0.2 if i % 3 else -0.05,
                    "net_exit_return": 0.08 if i % 3 else -0.02,
                    "features": {"above_ma20": 1 if i % 2 else 0},
                }
            )
        gains = feature_gains(rows, ("above_ma20",))
        info = gains["above_ma20"]
        self.assertIn("base_rate", info)
        for b in info["buckets"]:
            self.assertIn("gain_lb", b)
            self.assertIn("lb_win_rate", b)
            # 下界不超过朴素胜率
            self.assertLessEqual(b["lb_win_rate"], b["win_rate"] + 1e-9)

    def test_score_uses_lower_bound(self) -> None:
        rows = [
            {
                "signal_date": "2026-01-01",
                "kind": "FIRST_BUY",
                "net_exit_return": 0.08,
                "features": {"above_ma20": 1},
            }
        ] * 40 + [
            {
                "signal_date": "2026-01-02",
                "kind": "FIRST_BUY",
                "net_exit_return": -0.01,
                "features": {"above_ma20": 0},
            }
        ] * 40
        gains = feature_gains(rows, ("above_ma20",))
        score, hits = score_signal({"above_ma20": 1}, gains)
        self.assertTrue(hits)
        h = hits[0]
        self.assertIn("gain_lb", h)
        self.assertIn("n", h)
        # 总分 = 下界增益之和（不是朴素增益）
        self.assertAlmostEqual(score, h["gain_lb"], places=6)


class MarketRegimeTest(unittest.TestCase):
    def test_up_down_flat(self) -> None:
        self.assertEqual(market_regime({"features": {"market_index_change": 0.08}}), "上行")
        self.assertEqual(market_regime({"features": {"market_index_change": -0.08}}), "下行")
        self.assertEqual(market_regime({"features": {"market_index_change": 0.01}}), "震荡")

    def test_weak_breadth_overrides_to_weak(self) -> None:
        self.assertEqual(
            market_regime({"features": {"market_index_change": 0.01, "market_breadth": 0.2}}),
            "弱市",
        )

    def test_missing_index_is_none(self) -> None:
        self.assertIsNone(market_regime({"features": {}}))


class ValidateByRegimeTest(unittest.TestCase):
    def _rows(self, regime_chg: float, n: int = 100, start_day: int = 1) -> list:
        rows = []
        for i in range(n):
            rows.append(
                {
                    "signal_date": f"2026-{(start_day + i) % 12 + 1:02d}-{i % 28 + 1:02d}",
                    "kind": "FIRST_BUY",
                    "net_exit_return": 0.08 if i % 2 else -0.01,
                    "features": {"market_index_change": regime_chg},
                }
            )
        return rows

    def test_segments_by_regime(self) -> None:
        rows = self._rows(0.08, 80) + self._rows(-0.08, 80)
        out = validate_by_regime(rows)
        self.assertIsNotNone(out)
        regimes = set(out["regimes"])
        self.assertIn("上行", regimes)
        self.assertIn("下行", regimes)
        self.assertTrue("verifiable" in out["segments"][0])

    def test_overall_edge_requires_all_regimes(self) -> None:
        # 一段有效一段无效 → overall 必须是 False
        good = self._rows(0.08, 120)  # 完美的可分样本
        for i, r in enumerate(good):
            r["features"]["above_ma20"] = 1 if i % 2 else 0
            # 上行段：站上 MA20 的全赢、没站上的全输
            r["net_exit_return"] = 0.09 if r["features"]["above_ma20"] else -0.05
        bad = self._rows(-0.08, 120)  # 完全随机的样本（下一段覆盖）
        for i, r in enumerate(bad):
            r["features"]["above_ma20"] = 1 if i % 2 else 0
            r["net_exit_return"] = 0.02 if i % 3 else -0.02  # 与特征无关
        out = validate_by_regime(good + bad)
        self.assertIsNotNone(out)
        self.assertFalse(out["overall_has_edge"])


class ExitComputationTest(unittest.TestCase):
    """规则化出场的核心逻辑：窗口内首个卖点 / 强平 / 成本 / alpha。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def _bars(self, closes: list[float], start: date = date(2026, 1, 1)) -> list:
        return [
            bar("sh600000", (start + timedelta(i)).isoformat(), c) for i, c in enumerate(closes)
        ]

    def _sig(self, day: str, price: float) -> dict:
        return {
            "date": day,
            "price": price,
            "anchor_price": price * 0.95,
            "kind": "FIRST_BUY",
            "period": "day",
        }

    def _run(self, closes: list[float], sig: dict, sells: list, bench: list) -> dict:
        bars = self._bars(closes)
        dates = [b.date for b in bars]
        ind = self.service._indicator_cache(bars)
        return self.service._compute_outcome(
            "sh600000", sig, bars, dates, set(dates), ind,
            sell_dates=sells, benchmark_seq=bench,
        )

    def test_sell_within_window_exits_at_sell_price(self) -> None:
        # 70 根缓慢上涨 + 中间一个明确卖点日
        closes = [10 + i * 0.05 for i in range(70)]
        dates = [b.date for b in self._bars(closes)]
        sell_day = dates[10]
        sell_price = closes[10] * 1.1  # 卖点信号价比当日收盘高 10%（模拟确认价口径）
        outcome = self._run(
            closes, self._sig(dates[0], closes[0]),
            [(sell_day, sell_price)], [],
        )
        self.assertEqual(outcome["exit_kind"], "SELL")
        self.assertEqual(outcome["exit_date"], sell_day)
        self.assertAlmostEqual(outcome["exit_return"], sell_price / closes[0] - 1.0, places=4)

    def test_sell_outside_window_becomes_flat_close(self) -> None:
        """冒烟测试抓到的 bug：窗口外的卖点绝不能覆盖强平。"""

        closes = [10 + i * 0.02 for i in range(70)]
        dates = [b.date for b in self._bars(closes)]
        # 卖点在窗口外（第 65 天，EXIT_WINDOW_BARS=60）
        out_day = dates[65]
        outcome = self._run(
            closes, self._sig(dates[0], closes[0]),
            [(out_day, closes[65])], [],
        )
        self.assertEqual(outcome["exit_kind"], "FLAT_CLOSE")
        # future = 信号日之后（不含当天），exit_window = future[:60] → 末根是 dates[60]
        self.assertEqual(outcome["exit_date"], dates[EXIT_WINDOW_BARS])

    def test_net_return_subtracts_cost(self) -> None:
        closes = [10.0] * 30
        dates = [b.date for b in self._bars(closes)]
        outcome = self._run(closes, self._sig(dates[0], 10.0), [], [])
        self.assertAlmostEqual(outcome["exit_return"], 0.0, places=6)
        self.assertAlmostEqual(outcome["net_exit_return"], -TRADE_COST_ROUNDTRIP, places=6)

    def test_alpha_against_benchmark(self) -> None:
        closes = [10 + i * 0.1 for i in range(30)]
        dates = [b.date for b in self._bars(closes)]
        bench = [(dates[0], 3000.0), (dates[-1], 3060.0)]  # 指数涨 2%
        outcome = self._run(closes, self._sig(dates[0], 10.0), [], bench)
        self.assertIsNotNone(outcome["alpha_exit"])
        expected = outcome["net_exit_return"] - 0.02
        self.assertAlmostEqual(outcome["alpha_exit"], expected, places=4)

    def test_no_future_bars_leaves_exit_empty(self) -> None:
        closes = [10.0, 10.0, 10.0]
        dates = [b.date for b in self._bars(closes)]
        # 信号日 = 最后一根 → 无后续 K 线
        outcome = self._run(closes, self._sig(dates[-1], 10.0), [], [])
        self.assertIsNone(outcome.get("exit_return"))


class PoolExitGuideTest(unittest.TestCase):
    """池行挂止损价与期望出场。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def test_stop_price_from_anchor(self) -> None:
        base = date(2026, 1, 1)
        days = [(base + timedelta(i)).isoformat() for i in range(40)]
        self.store.save_bars([bar("sh600000", d, 10.0) for d in days])
        self.store.save_signals(
            [
                type(
                    "S", (),
                    {
                        "symbol": "sh600000",
                        "kind": SignalKind.FIRST_BUY,
                        "date": days[39],
                        "bar_index": 39,
                        "price": 10.0,
                        "anchor_index": 38,
                        "anchor_price": 9.5,
                        "message": "x",
                        "meta": {},
                        "period": "day",
                    },
                )()
            ]
        )
        pools = self.service.pools(days=3)
        rows = pools["first_buy"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stop_price"], 9.5)


if __name__ == "__main__":
    unittest.main()

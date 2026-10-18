"""Service 层测试：级别联立背景、买卖点字段语义、周期隔离。

覆盖一次真实回归：`daily_context()` 曾按 `row["bar_index"]` 取最新信号，
但 `Store._signal_row_to_dict()` 当时没吐这个字段，导致分钟级别接口 500。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import Bar, Signal, SignalKind  # noqa: E402

# service.py 内部用相对导入，因此必须按包路径导入 backend.service
from backend.service import BUY_KIND_VALUES, SELL_KIND_VALUES, ScanService  # noqa: E402
from backend.store import Store  # noqa: E402


def make_signal(
    symbol: str,
    kind: SignalKind,
    bar_index: int,
    trade_date: str,
    meta: dict,
    period: str = "day",
) -> Signal:
    return Signal(
        symbol=symbol,
        kind=kind,
        date=trade_date,
        bar_index=bar_index,
        price=10.0 + bar_index,
        anchor_index=bar_index - 1,
        anchor_price=9.5 + bar_index,
        message=f"{kind.value}@{bar_index}",
        meta=meta,
        period=period,
    )


class DailyContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_missing_cache_reports_unavailable(self) -> None:
        ctx = self.service.daily_context("sh600519")
        self.assertFalse(ctx["available"])
        self.assertIn("日线", ctx["message"])

    def test_picks_latest_daily_signal_and_ignores_minute(self) -> None:
        self.store.save_signals(
            [
                make_signal("sh600519", SignalKind.FIRST_BUY, 100, "2026-05-01", {"divergence_score": 0.3}),
                make_signal("sh600519", SignalKind.FIRST_SELL, 250, "2026-09-08", {"top_divergence_score": 0.42}),
            ]
        )
        # 分钟周期里 bar_index 更大，但绝不能影响日线背景的选取
        self.store.save_signals(
            [make_signal("sh600519", SignalKind.FIRST_BUY, 999, "2026-10-15 10:15:00", {"divergence_score": 0.9}, period="m15")]
        )

        ctx = self.service.daily_context("sh600519")
        self.assertTrue(ctx["available"])
        self.assertEqual(ctx["kind"], "FIRST_SELL")
        self.assertEqual(ctx["date"], "2026-09-08")
        self.assertTrue(ctx["is_sell"])
        self.assertFalse(ctx["is_buy"])
        self.assertAlmostEqual(ctx["score"], 0.42)
        self.assertIn("一卖", ctx["kind_label"])


class SignalSemanticsTest(unittest.TestCase):
    def test_buy_and_sell_kind_sets_are_disjoint(self) -> None:
        """买点与卖点集合必须互斥，否则 qualifies 的语义会被污染。"""

        self.assertEqual(BUY_KIND_VALUES & SELL_KIND_VALUES, set())
        self.assertIn(SignalKind.FIRST_SELL.value, SELL_KIND_VALUES)
        self.assertIn(SignalKind.SECOND_SELL.value, SELL_KIND_VALUES)
        for kind in ("FIRST_BUY", "SECOND_BUY", "THIRD_BUY"):
            self.assertIn(kind, BUY_KIND_VALUES)

    def test_analysis_payload_separates_buy_and_risk_signals(self) -> None:
        """构造一份含买点与卖点的分析结果，确认 qualifies 只由买点驱动。"""

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            service = ScanService(store)
            bars = [
                Bar(
                    symbol="sh600519",
                    date=f"2026-01-{i + 1:02d}",
                    open=10.0,
                    high=11.0,
                    low=9.0,
                    close=10.5,
                    volume=1.0,
                    amount=1.0,
                )
                for i in range(10)
            ]
            sell_only = {
                "signals": [
                    {
                        "symbol": "sh600519",
                        "kind": "FIRST_SELL",
                        "date": "2026-01-10",
                        "bar_index": 9,
                        "price": 10.5,
                        "anchor_index": 8,
                        "anchor_price": 11.0,
                        "message": "顶背驰",
                        "meta": {"top_divergence_score": 0.5},
                        "score": 0.5,
                        "is_sell": True,
                    }
                ]
            }
            payload = service._build_analysis_payload("sh600519", "测试股", bars, sell_only, [], "day")
            self.assertFalse(payload["qualifies"], "只有卖点时不应判定为符合买入条件")
            self.assertTrue(payload["risk_active"])
            self.assertEqual(len(payload["risk_signals"]), 1)
            self.assertEqual(payload["buy_signals"], [])

            buy_payload = dict(sell_only)
            buy_payload["signals"] = [
                {
                    "symbol": "sh600519",
                    "kind": "FIRST_BUY",
                    "date": "2026-01-09",
                    "bar_index": 8,
                    "price": 10.0,
                    "anchor_index": 7,
                    "anchor_price": 9.5,
                    "message": "背驰后一买成立",
                    "meta": {"divergence_score": 0.3},
                    "score": 0.3,
                    "is_sell": False,
                }
            ]
            payload2 = service._build_analysis_payload("sh600519", "测试股", bars, buy_payload, [], "day")
            self.assertTrue(payload2["qualifies"])
            self.assertFalse(payload2["risk_active"])
            store.close()


class RiskPoolTest(unittest.TestCase):
    """风险池回归：批次8 曾把买点键混进 risk_pool 响应导致必 500（服务器实爆）。

    grouped 按 SELL_KINDS 初始化，只含 first_sell/second_sell 两个键；
    响应里再取 grouped["FIRST_BUY"] 等买点键就是 KeyError——且是**每次调用都炸**，
    不是偶发。这里锁死：接口正常返回、只含卖点键、空窗口同样不炸。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def _seed(self) -> None:
        # bars 提供 recent_trading_dates 的日线日期集合；信号给卖点与买点各一条。
        days = [f"2026-10-{d:02d}" for d in range(1, 13)]
        bars = [
            # 注意第 9 个位置参数是 Bar.period 不是 fq——存成 "qfq" 会让
            # recent_trading_dates(period="day") 查空（本用例曾真实踩过）
            Bar("sh600000", d, c, c + 1, c - 1, c, 0.0, 0.0, "day")
            for j, (d, c) in enumerate([(d, 100.0 + j) for j, d in enumerate(days)])
        ]
        self.store.save_bars(bars)
        self.store.save_signals(
            [
                # recent_trading_dates(3) = 10-12/10-11/10-10，cutoff=10-10，
                # 两条卖点都要落在窗口内；买点也放窗口内验证 risk 池不吞它
                make_signal("sh600000", SignalKind.FIRST_SELL, 8, days[9], {}, "day"),
                make_signal("sh600000", SignalKind.SECOND_SELL, 10, days[11], {}, "day"),
                # 买点也在库中，但 risk_pool 只应吐卖点——买点归买点池管
                make_signal("sh600000", SignalKind.FIRST_BUY, 9, days[10], {}, "day"),
            ]
        )

    def test_risk_pool_returns_sell_keys_without_keyerror(self) -> None:
        self._seed()
        out = self.service.risk_pool(days=3)
        self.assertEqual(out["days"], 3)
        self.assertEqual(len(out["first_sell"]), 1)
        self.assertEqual(len(out["second_sell"]), 1)
        self.assertNotIn("first_buy", out)
        self.assertNotIn("second_buy", out)
        self.assertNotIn("third_buy", out)

    def test_risk_pool_empty_window_still_ok(self) -> None:
        """窗口内没有任何信号（库为空）时也必须正常返回空池，而不是 500。"""

        out = self.service.risk_pool(days=3)
        self.assertEqual(out["first_sell"], [])
        self.assertEqual(out["second_sell"], [])

    def test_pools_still_return_buy_keys(self) -> None:
        """对照：买点池的响应键不受本次修复影响。"""

        self._seed()
        out = self.service.pools(days=3)
        for key in ("first_buy", "second_buy", "third_buy"):
            self.assertIn(key, out)


if __name__ == "__main__":
    unittest.main()


class BulkSuccessRateTest(unittest.TestCase):
    """批量胜率：必须与逐只 symbol_success_rate 口径完全一致，且混合徽章正确。

    背景（实测）：逐只查询 241 只要 6206ms，批量 IN 只要 ~160ms。
    但批量绝不能「算得更快却算错」——两套实现会漂移，同一只票在两个
    接口里给出不同百分比。这里锁死逐字段相等。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    @staticmethod
    def _dates(start="2026-02-01", n=45):
        from datetime import date, timedelta

        base = date.fromisoformat(start)
        return [(base + timedelta(days=j)).isoformat() for j in range(n)]

    def _seed_symbol(self, symbol, days):
        bars = [
            Bar(symbol, d, c, c + 1, c - 1, c, 0.0, 0.0, "qfq")
            for j, d in enumerate(days)
            for c in [100.0 + j]
        ]
        self.store.save_bars(bars)
        # 一买先于二买、二买后不破低 → 各 1 次成功（样本不足，但结构一致）
        self.store.save_signals(
            [
                make_signal(symbol, SignalKind.FIRST_BUY, 10, days[10], {}, "day"),
                make_signal(symbol, SignalKind.SECOND_BUY, 20, days[20], {}, "day"),
            ]
        )

    def test_bulk_matches_single_field_by_field(self) -> None:
        symbols = [f"sh{a:06d}" for a in range(5)]
        days = self._dates()
        for sym in symbols:
            self._seed_symbol(sym, days)
        bulk = self.service.symbol_success_rates(symbols)
        self.assertEqual(set(bulk), set(symbols))
        for sym in symbols:
            single = self.service.symbol_success_rate(sym)
            b = bulk[sym]
            self.assertEqual(b["available"], single["available"])
            for key in ("first_buy", "second_buy"):
                for field in ("closed", "success", "rate", "insufficient", "failed", "total"):
                    self.assertEqual(
                        b[key][field], single[key][field], msg=f"{sym}.{key}.{field}"
                    )

    def test_attach_pool_rates_uses_mixed_badge(self) -> None:
        sym = "sh999001"
        days = self._dates()
        self._seed_symbol(sym, days)
        grouped = {
            SignalKind.FIRST_BUY.value: [
                make_signal(sym, SignalKind.FIRST_BUY, 10, days[10], {}, "day").__dict__
            ],
            SignalKind.SECOND_BUY.value: [],
            SignalKind.THIRD_BUY.value: [],
        }
        # make_signal 返回 Signal dataclass，转成 store 吐出的 dict 形状
        grouped[SignalKind.FIRST_BUY.value] = [
            self.store._signal_row_to_dict(
                self.store._conn.execute(
                    "SELECT * FROM signals WHERE symbol=? AND kind=?", (sym, SignalKind.FIRST_BUY.value)
                ).fetchone()
            )
        ]
        self.service._attach_pool_rates(grouped, "day")
        row = grouped[SignalKind.FIRST_BUY.value][0]
        self.assertIn("rate", row)
        self.assertIn("rate_value", row)
        self.assertEqual(row["rate_value"], row["rate"]["value"])
        # 三买没有成功率口径 → source='none'
        none_row = [{"symbol": sym}]
        self.service._attach_pool_rates(
            {SignalKind.THIRD_BUY.value: none_row}, "day"
        )
        self.assertEqual(none_row[0]["rate"]["source"], "none")
        self.assertEqual(none_row[0]["rate_value"], -1)

    def test_rate_badge_symbol_source(self) -> None:
        payload = {
            "available": True,
            "first_buy": {"closed": 3, "success": 2, "rate": 0.6667, "insufficient": False},
        }
        badge = ScanService._rate_badge(
            payload, "first_buy", {"first_buy": {"rate": 0.1, "closed": 100}}
        )
        self.assertEqual(badge["source"], "symbol")
        self.assertEqual(badge["text"], "66.7%")
        self.assertAlmostEqual(badge["value"], 0.6667)

    def test_rate_badge_falls_back_to_market(self) -> None:
        # 个股样本不足（1 次）→ 退回全市场基准
        payload = {
            "available": True,
            "first_buy": {"closed": 1, "success": 1, "rate": 1.0, "insufficient": True},
        }
        baseline = {"first_buy": {"rate": 0.1444, "closed": 3983}}
        badge = ScanService._rate_badge(payload, "first_buy", baseline)
        self.assertEqual(badge["source"], "market")
        self.assertEqual(badge["text"], "14.4%")

    def test_rate_badge_none_without_kind(self) -> None:
        badge = ScanService._rate_badge({}, None, {})
        self.assertEqual(badge["source"], "none")
        self.assertEqual(badge["text"], "—")
        self.assertEqual(badge["value"], -1)

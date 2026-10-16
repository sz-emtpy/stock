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


if __name__ == "__main__":
    unittest.main()

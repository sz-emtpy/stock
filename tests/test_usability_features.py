"""自选持仓 / 板块热度 / 扫描历史 / 增量扫描 / 参数敏感性 的后端回归。

这些能力都是「易用性补强」，但都有容易踩错的口径：
- 自选的风险提示必须**只关联自选股自身的卖点**，不能把全市场卖点混进来；
- 板块热度只能统计**已缓存板块**的个股（反查单只 5~7 秒），并且要如实报覆盖率；
- 增量扫描必须只挑「本地日线落后于最新交易日」的，不能把已最新的又扫一遍；
- 参数敏感性回测**绝不能联网**（回测带来取数副作用是不可接受的）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import Bar, Signal, SignalKind  # noqa: E402

from backend import service as service_module  # noqa: E402
from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402


def make_bars(symbol: str, count: int = 60, start_day: str = "2026-09-01") -> list[Bar]:
    base = date.fromisoformat(start_day)
    out: list[Bar] = []
    price = 100.0
    for i in range(count):
        price += 0.5 if i % 2 == 0 else -0.3
        out.append(
            Bar(
                symbol=symbol,
                date=(base + timedelta(days=i)).isoformat(),
                open=price,
                high=price + 0.4,
                low=price - 0.4,
                close=price,
                volume=1_000_000,
                amount=price * 1_000_000,
                period="day",
            )
        )
    return out


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
        meta={"divergence_score": 0.2},
        period="day",
    )


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)


class FavoritesTest(BaseCase):
    def test_empty_favorites_is_honest(self) -> None:
        out = self.service.favorites()
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["items"], [])
        self.assertIn("自选为空", out["message"])

    def test_add_list_remove_roundtrip(self) -> None:
        self.service.add_favorite("sh600519", note="底仓", cost_price=1500.0)
        self.service.add_favorite("sz000063")
        out = self.service.favorites()
        self.assertEqual(out["count"], 2)
        by_symbol = {it["symbol"]: it for it in out["items"]}
        self.assertEqual(by_symbol["sh600519"]["note"], "底仓")
        self.assertEqual(by_symbol["sh600519"]["cost_price"], 1500.0)
        self.assertIsNone(by_symbol["sz000063"]["cost_price"])

        removed = self.service.remove_favorite("sh600519")
        self.assertTrue(removed["removed"])
        self.assertEqual(self.service.favorites()["count"], 1)

    def test_remove_missing_is_not_error(self) -> None:
        out = self.service.remove_favorite("sh999999")
        self.assertFalse(out["removed"])
        self.assertIn("本来就不在自选", out["message"])

    def test_blank_symbol_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.add_favorite("   ")

    def test_pnl_computed_from_cost_and_last_close(self) -> None:
        bars = make_bars("sh600519", 10)
        self.store.save_bars(bars)
        last_close = bars[-1].close
        self.service.add_favorite("sh600519", cost_price=round(last_close / 1.1, 2))
        item = self.service.favorites()["items"][0]
        self.assertAlmostEqual(item["last_close"], last_close, places=2)
        self.assertIsNotNone(item["pnl_pct"])
        self.assertGreater(item["pnl_pct"], 0)   # 现价高于成本 → 浮盈为正

    def test_pnl_none_without_cost_price(self) -> None:
        self.store.save_bars(make_bars("sh600519", 10))
        self.service.add_favorite("sh600519")
        item = self.service.favorites()["items"][0]
        self.assertIsNone(item["pnl_pct"], "没填成本价就该是 None，不能算成 0%")

    def test_risk_only_attaches_own_sell_signals(self) -> None:
        """关键口径：自选 A 的卖点不能挂到自选 B 头上。"""

        days = [f"2026-10-{d:02d}" for d in range(1, 13)]
        self.store.save_bars([Bar("sh600000", d, 100, 101, 99, 100, 0, 0, "day") for d in days])
        # 只有 sh600000 有卖点；sh600519 没有
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_SELL, days[-1])])
        self.service.add_favorite("sh600000")
        self.service.add_favorite("sh600519")

        items = {it["symbol"]: it for it in self.service.favorites()["items"]}
        self.assertEqual(items["sh600000"]["risk_count"], 1)
        self.assertEqual(items["sh600519"]["risk_count"], 0)
        self.assertEqual(items["sh600519"]["risk_signals"], [])


class SectorHeatTest(BaseCase):
    def _seed(self, symbols_with_sector: list[str], symbols_without: list[str]) -> None:
        days = [f"2026-10-{d:02d}" for d in range(1, 13)]
        bars = []
        signals = []
        for sym in symbols_with_sector + symbols_without:
            bars.extend(Bar(sym, d, 100, 101, 99, 100, 0, 0, "day") for d in days)
            signals.append(make_signal(sym, SignalKind.FIRST_BUY, days[-1]))
        self.store.save_bars(bars)
        self.store.save_signals(signals)
        for i, sym in enumerate(symbols_with_sector):
            self.store.save_stock_sectors(
                sym, [{"code": f"pt{i:04d}", "name": f"板块{i}", "level": "sw2"}]
            )

    def test_groups_by_sector_and_reports_coverage(self) -> None:
        self._seed(["sh600000", "sh600519"], ["sz000001"])
        out = self.service.sector_heat(days=3)
        self.assertEqual(out["signals"], 3)
        self.assertEqual(out["covered"], 2, "只有两只缓存了板块")
        self.assertEqual(out["unknown"]["count"], 1, "未缓存板块的那只应计入板块未知")
        names = {s["name"] for s in out["sectors"]}
        self.assertEqual(len(out["sectors"]), 2)
        self.assertTrue(all(s["count"] == 1 for s in out["sectors"]))

    def test_empty_when_no_signals(self) -> None:
        out = self.service.sector_heat(days=3)
        self.assertEqual(out["sectors"], [])
        self.assertEqual(out["signals"], 0)

    def test_ranked_by_count_desc(self) -> None:
        days = [f"2026-10-{d:02d}" for d in range(1, 13)]
        self.store.save_bars([Bar("sh600000", d, 100, 101, 99, 100, 0, 0, "day") for d in days])
        self.store.save_signals(
            [
                make_signal("sh600000", SignalKind.FIRST_BUY, days[-1]),
                make_signal("sh600000", SignalKind.SECOND_BUY, days[-1] + "T"),
            ]
        )
        # 同一板块命中两次（一买 + 二买），排序应把它排在只有 1 次的板块前
        self.store.save_stock_sectors("sh600000", [{"code": "pt1", "name": "热门", "level": "sw2"}])
        out = self.service.sector_heat(days=3)
        hot = next(s for s in out["sectors"] if s["name"] == "热门")
        self.assertEqual(hot["count"], 2)
        self.assertEqual(out["sectors"][0]["name"], "热门")


class ScanHistoryTest(BaseCase):
    def test_empty_history(self) -> None:
        out = self.service.scan_history()
        self.assertEqual(out["count"], 0)
        self.assertIn("还没有扫描记录", out["message"])

    def test_history_with_hits_by_kind(self) -> None:
        self.store.create_run("run-1", scope="all", total=10)
        self.store.update_run("run-1", processed=10, hits=3)
        self.store.finish_run("run-1", "finished", "完成")
        self.store.save_hits(
            "run-1",
            [
                {"symbol": "sh600000", "kind": "FIRST_BUY", "date": "2026-10-16", "price": 10.0},
                {"symbol": "sh600519", "kind": "FIRST_BUY", "date": "2026-10-16", "price": 20.0},
                {"symbol": "sz000001", "kind": "FIRST_SELL", "date": "2026-10-16", "price": 30.0},
            ],
        )
        out = self.service.scan_history()
        self.assertEqual(out["count"], 1)
        row = out["items"][0]
        self.assertEqual(row["scope_label"], "全部A股")
        self.assertEqual(row["by_kind"].get("FIRST_BUY"), 2)
        self.assertEqual(row["by_kind"].get("FIRST_SELL"), 1)
        self.assertIsNotNone(row["elapsed"])

    def test_newest_first(self) -> None:
        self.store.create_run("run-old", scope="all", total=5)
        self.store.finish_run("run-old", "finished")
        self.store.create_run("run-new", scope="hs300", total=5)
        out = self.service.scan_history()
        self.assertEqual(out["items"][0]["run_id"], "run-new")


class IncrementalScanTest(BaseCase):
    def test_targets_are_only_lagging_symbols(self) -> None:
        """本地已是最新的标的不该进增量清单——否则「增量」等于全量。"""

        # 三只票：两只停在 10-10，一只已到 10-16
        for sym in ("sh600000", "sh600519"):
            self.store.save_bars(make_bars(sym, 3, start_day="2026-10-08"))
        self.store.save_bars(make_bars("sz000001", 3, start_day="2026-10-14"))
        self.store.upsert_symbol("sh600000", "浦发银行")
        self.store.upsert_symbol("sh600519", "贵州茅台")
        self.store.upsert_symbol("sz000001", "平安银行")

        with mock.patch.object(
            service_module,
            "fetch_universe_symbols",
            lambda scope: (
                [("sh600000", "浦发银行"), ("sh600519", "贵州茅台"), ("sz000001", "平安银行")],
                {},
            ),
        ):
            plan = self.service.incremental_scan_targets("all")

        self.assertEqual(plan["latest_trade_date"], "2026-10-16")
        self.assertEqual(plan["count"], 2)
        self.assertEqual(plan["skipped"], 1)
        self.assertEqual(
            {sym for sym, _ in plan["targets"]}, {"sh600000", "sh600519"}
        )

    def test_nothing_to_do_starts_nothing(self) -> None:
        """全部已是最新时必须直接返回提示，不能空跑一个 30 分钟的扫描。"""

        self.store.save_bars(make_bars("sh600000", 3, start_day="2026-10-14"))
        self.store.upsert_symbol("sh600000", "浦发银行")
        with mock.patch.object(
            service_module,
            "fetch_universe_symbols",
            lambda scope: ([("sh600000", "浦发银行")], {}),
        ):
            out = self.service.start_incremental_scan("all")
        self.assertFalse(out["started"])
        self.assertIsNone(out["run_id"])
        self.assertIn("无需增量", out["message"])


class ParamSensitivityTest(BaseCase):
    def test_insufficient_bars_returns_unavailable(self) -> None:
        out = self.service.param_sensitivity("sh600519")
        self.assertFalse(out["available"])
        self.assertIn("不足", out["message"])

    def test_reports_baseline_and_sweeps(self) -> None:
        self.store.save_bars(make_bars("sh600519", 80))
        out = self.service.param_sensitivity("sh600519")
        self.assertTrue(out["available"])
        self.assertEqual(out["bars"], 80)
        self.assertIn("baseline", out)
        params = {row["param"] for row in out["sweeps"]}
        self.assertEqual(
            params, {"min_divergence_score", "max_wait_bars", "ma_min_male_kisses"}
        )
        # 每个参数网格里必须恰好有一档标为当前值
        for param in params:
            current = [r for r in out["sweeps"] if r["param"] == param and r["current"]]
            self.assertEqual(len(current), 1, f"{param} 应恰好一档标为当前")

    def test_never_touches_network(self) -> None:
        """回测必须纯本地。取数函数被调用即说明回测带来了网络副作用。"""

        self.store.save_bars(make_bars("sh600519", 80))
        boom = mock.Mock(side_effect=AssertionError("回测不该联网取数"))
        with mock.patch.object(service_module, "fetch_bars", boom):
            out = self.service.param_sensitivity("sh600519")
        boom.assert_not_called()
        self.assertTrue(out["available"])

    def test_blank_symbol_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.param_sensitivity("  ")


class BatchNestingTest(BaseCase):
    def test_no_candidates_is_honest(self) -> None:
        out = self.service.batch_interval_nesting(limit=5)
        self.assertEqual(out["checked"], 0)
        self.assertIn("没有可检查的标的", out["message"])

    def test_summary_counts_are_consistent(self) -> None:
        """checked 必须等于各类状态之和——否则汇总数字会骗人。"""

        days = [f"2026-10-{d:02d}" for d in range(10, 17)]
        for sym in ("sh600000", "sh600519"):
            self.store.save_bars([Bar(sym, d, 100, 101, 99, 100, 0, 0, "day") for d in days])
        self.store.save_signals([make_signal("sh600000", SignalKind.FIRST_BUY, days[-1])])
        self.service.add_favorite("sh600000")

        out = self.service.batch_interval_nesting(limit=5)
        statuses = [it["status"] for it in out["items"]]
        self.assertEqual(out["checked"], len(statuses))
        counted = out["confirmed"] + out["unconfirmed"] + out["no_data"]
        self.assertEqual(
            counted, len([s for s in statuses if s != "error"]),
            "confirmed+unconfirmed+no_data 必须等于非 error 条目数",
        )


class UniverseSearchTest(BaseCase):
    """模糊搜索的排序口径（放在 store/service 之外，直接测 app 里的逻辑函数）。"""

    def test_search_ranking(self) -> None:
        from backend.app import universe

        self.store.upsert_symbol("sh600519", "贵州茅台")
        self.store.upsert_symbol("sh600500", "中化国际")
        self.store.upsert_symbol("sz000063", "中兴通讯")

        with mock.patch("backend.app.store", self.store):
            rows = universe(q="600519", limit=10)
            self.assertEqual(rows[0]["symbol"], "sh600519")

            rows = universe(q="6005", limit=10)
            symbols = [r["symbol"] for r in rows]
            # 代码前缀命中的应排在前面
            self.assertEqual(symbols[0], "sh600500")

            rows = universe(q="茅台", limit=10)
            self.assertEqual([r["symbol"] for r in rows], ["sh600519"])

            rows = universe(q="不存在的名字", limit=10)
            self.assertEqual(rows, [])

    def test_empty_query_returns_list(self) -> None:
        from backend.app import universe

        self.store.upsert_symbol("sh600519", "贵州茅台")
        with mock.patch("backend.app.store", self.store):
            rows = universe(q=None, limit=10)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()

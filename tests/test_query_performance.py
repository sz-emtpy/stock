"""慢查询回归：索引必须存在，且必须能把 period 用在前导列。

背景（实测）：全市场扫描后 bars 涨到 218 万行，`recent_trading_dates` 变成
8990ms、`period_counts` 变成 7809ms，页面首屏要等 12–20 秒。根因是原索引
`(symbol, period, trade_date)` 的**前导列是 symbol**，而这两个查询只按
period 过滤，用不上索引 → 全表扫。加 `(period, trade_date)` 覆盖索引后
降到 0.24ms / 61ms。

这组用例的作用是把「索引被谁误删」立刻暴露出来——删索引不会报错，
只会在数据涨到百万级后才突然变慢，那时已经很难联想到是索引问题。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import Bar  # noqa: E402

from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402

# 必须存在的覆盖索引：period 必须在最前面，否则跨股查询退化成全表扫
REQUIRED_INDEXES = {
    "bars": ["idx_bars_period_date"],
    "signals": ["idx_signals_period_kind_date"],
}


def make_bars(symbol: str, count: int, start: str = "2026-01-01") -> list[Bar]:
    from datetime import date, timedelta

    day = date.fromisoformat(start)
    out: list[Bar] = []
    price = 100.0
    for i in range(count):
        price += 0.5 if i % 2 == 0 else -0.3
        d = (day + timedelta(days=i)).isoformat()
        out.append(
            Bar(
                symbol=symbol,
                date=d,
                open=price,
                high=price + 0.4,
                low=price - 0.4,
                close=price,
                volume=1_000_000,
                amount=price * 1_000_000,
            )
        )
    return out


class IndexSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def index_sql(self, table: str) -> dict[str, str]:
        rows = self.store._conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name = ?", (table,)
        ).fetchall()
        return {row["name"]: (row["sql"] or "") for row in rows}

    def test_required_indexes_exist(self) -> None:
        for table, names in REQUIRED_INDEXES.items():
            existing = self.index_sql(table)
            for name in names:
                self.assertIn(name, existing, f"{table} 缺少索引 {name}")

    def test_period_is_the_leading_column(self) -> None:
        """这是关键：period 放到后面就等于没建，查询照样全扫。"""

        for table, names in REQUIRED_INDEXES.items():
            existing = self.index_sql(table)
            for name in names:
                sql = existing[name].replace("\n", " ")
                self.assertIn("(period,", sql.replace(" ", " "), f"{name} 的首列必须是 period")

    def test_indexes_are_idempotent_across_opens(self) -> None:
        """重复打开库不应重复建索引报错（CREATE INDEX IF NOT EXISTS 的意义）。"""

        path = Path(self._tmp.name) / "t.db"
        self.store.close()
        again = Store(path)
        # 换掉 self.store，好让 addCleanup 关的是这个还活着的连接
        self.store = again
        self.assertIn("idx_bars_period_date", self.index_sql("bars"))
        self.assertIn("idx_signals_period_kind_date", self.index_sql("signals"))


class QueryPlanTest(unittest.TestCase):
    """确认查询计划真的走索引，而不是碰巧数据量小看不出来。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def plan(self, sql: str, params: tuple = ()) -> str:
        rows = self.store._conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        return " | ".join(str(row["detail"]) for row in rows)

    def test_recent_trading_dates_uses_index_not_scan(self) -> None:
        for i in range(30):
            self.store.save_bars(make_bars(f"sh60000{i}", 40))
        detail = self.plan(
            "SELECT DISTINCT trade_date FROM bars WHERE period = ? ORDER BY trade_date DESC LIMIT 3",
            ("day",),
        )
        self.assertIn("idx_bars_period_date", detail, f"未走覆盖索引，实际计划: {detail}")
        self.assertNotIn("SCAN bars", detail, f"退化成全表扫: {detail}")

    def test_signals_since_uses_kind_index(self) -> None:
        from chanpicker.core import Signal, SignalKind

        for i in range(30):
            self.store.save_signals(
                [
                    Signal(
                        symbol=f"sh60000{i}",
                        kind=kind,
                        date="2026-02-01",
                        bar_index=10,
                        price=10.0,
                        anchor_index=9,
                        anchor_price=9.0,
                        message="x",
                        meta={"divergence_score": 0.2},
                    )
                    for kind in (SignalKind.FIRST_BUY, SignalKind.SECOND_SELL)
                ]
            )
        detail = self.plan(
            "SELECT * FROM signals WHERE kind IN ('FIRST_BUY') AND trade_date >= ? AND period = ?",
            ("2026-01-01", "day"),
        )
        self.assertIn("idx_signals_period_kind_date", detail, f"未走 kind 复合索引: {detail}")


class SymbolNamesBatchTest(unittest.TestCase):
    """symbol_names 是消 N+1 的关键，必须与单查语义一致。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def test_batch_matches_single_lookup(self) -> None:
        self.store.upsert_symbol("sh600519", "贵州茅台")
        self.store.upsert_symbol("sz000063", "中兴通讯")
        got = self.store.symbol_names(["sh600519", "sz000063"])
        self.assertEqual(got, {"sh600519": "贵州茅台", "sz000063": "中兴通讯"})

    def test_missing_symbol_falls_back_to_itself(self) -> None:
        # 与 symbol_name() 的语义保持一致：查不到就返回代码本身
        self.store.upsert_symbol("sh600519", "贵州茅台")
        got = self.store.symbol_names(["sh600519", "sh999999"])
        self.assertEqual(got["sh600519"], "贵州茅台")
        self.assertEqual(got["sh999999"], "sh999999")

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(self.store.symbol_names([]), {})
        self.assertEqual(self.store.symbol_names(["", ""]), {})

    def test_duplicates_collapse(self) -> None:
        self.store.upsert_symbol("sh600519", "贵州茅台")
        got = self.store.symbol_names(["sh600519", "sh600519", "sh600519"])
        self.assertEqual(got, {"sh600519": "贵州茅台"})

    def test_large_batch_crosses_chunk_boundary(self) -> None:
        # 恰好跨越 500 分批边界：499/500/501/1000/1001 都要正确
        for total in (499, 500, 501, 1000, 1001):
            with self.subTest(total=total):
                symbols = [f"sz{i:05d}" for i in range(total)]
                for i, sym in enumerate(symbols):
                    self.store.upsert_symbol(sym, f"名称{i}")
                got = self.store.symbol_names(symbols)
                self.assertEqual(len(got), total)
                self.assertEqual(got[symbols[0]], "名称0")
                self.assertEqual(got[symbols[-1]], f"名称{total - 1}")

    def test_watchlist_uses_batch_not_n_plus_one(self) -> None:
        from chanpicker.core import WatchItem, WatchState

        for i in range(5):
            sym = f"sh60000{i}"
            self.store.upsert_symbol(sym, f"股票{i}")
            self.store.save_watch_items(
                [
                    WatchItem(
                        symbol=sym,
                        state=WatchState.WAIT_SECOND_BUY,
                        first_buy_date="2026-01-02",
                        first_buy_index=1,
                        first_buy_price=10.0,
                        first_buy_low=9.5,
                        last_update_date="2026-01-03",
                    )
                ]
            )
        items = self.service.watchlist()
        self.assertEqual([it["name"] for it in items], [f"股票{i}" for i in range(5)])

    def test_batch_issues_single_query_regardless_of_row_count(self) -> None:
        """批量查询的 SQL 次数必须与行数无关 —— 这是 N+1 被真正消掉的证据。

        注意 sqlite3.Connection 的 execute 是只读属性，不能 monkeypatch；
        用官方提供的 set_trace_callback 抓实际下发的 SQL。
        """

        symbols = [f"sh6{i:05d}" for i in range(1200)]
        for i, sym in enumerate(symbols):
            self.store.upsert_symbol(sym, f"名称{i}")

        statements: list[str] = []
        self.store._conn.set_trace_callback(statements.append)
        try:
            self.store.symbol_names(symbols)
        finally:
            self.store._conn.set_trace_callback(None)
        selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        # 1200 行按 500 分批 → 3 次查询，而不是 1200 次
        self.assertLessEqual(len(selects), 5, f"批量查询次数过多: {len(selects)}")
        self.assertGreaterEqual(len(selects), 1)

    def test_single_lookup_loop_would_issue_one_query_per_row(self) -> None:
        """反向对照：确认上面的断言真的能区分「批量」和「逐个查」。"""

        symbols = [f"sh6{i:05d}" for i in range(50)]
        for i, sym in enumerate(symbols):
            self.store.upsert_symbol(sym, f"名称{i}")

        statements: list[str] = []
        self.store._conn.set_trace_callback(statements.append)
        try:
            for sym in symbols:
                self.store.symbol_name(sym)
        finally:
            self.store._conn.set_trace_callback(None)
        selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), len(symbols), "逐个查应当一次一条，两条断言才能互为对照")

    def test_large_batch_returns_same_result_as_loop(self) -> None:
        """规模下的正确性对照：批量结果必须与逐个查完全一致。

        这里刻意**不用**计时断言做性能保证——套件整体负载下计时会抖动，
        曾经真的误报过一次。性能保证交给下面的「查询次数与行数无关」，
        那条是确定性的。
        """

        symbols = [f"sh6{i:05d}" for i in range(2000)]
        for i, sym in enumerate(symbols):
            self.store.upsert_symbol(sym, f"名称{i}")

        batch = self.store.symbol_names(symbols)
        expected = {sym: self.store.symbol_name(sym) for sym in symbols}
        self.assertEqual(batch, expected)
        self.assertEqual(len(batch), 2000)

    @staticmethod
    def _count_selects(store: Store, action) -> int:
        """抓一段逻辑实际下发了多少次 SELECT。"""

        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        try:
            action()
        finally:
            store._conn.set_trace_callback(None)
        return len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")])


class ServiceBatchUsageTest(unittest.TestCase):
    """service 层的三个补名入口必须走批量。

    上一组只验证了 Store 有批量方法，漏了一件事：有人可能根本不用它。
    这里对 service 的三个调用点直接数 SQL 次数——改回逐行单查会立刻被抓住。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def _seed(self, total: int) -> None:
        """建 total 只股票的证券名 + 关注记录 + 一条信号。

        upsert_symbol 是 ON CONFLICT DO UPDATE，所以重复 seed 是幂等的，
        可以先 seed(40) 再 seed(200) 来做增量对比。
        """

        from chanpicker.core import Signal, SignalKind, WatchItem, WatchState

        for i in range(total):
            sym = f"sh6{i:05d}"
            self.store.upsert_symbol(sym, f"股票{i}")
            self.store.save_watch_items(
                [
                    WatchItem(
                        symbol=sym,
                        state=WatchState.WAIT_SECOND_BUY,
                        first_buy_date="2026-01-02",
                        first_buy_index=1,
                        first_buy_price=10.0,
                        first_buy_low=9.5,
                        last_update_date="2026-01-03",
                    )
                ]
            )
            self.store.save_signals(
                [
                    Signal(
                        symbol=sym,
                        kind=SignalKind.FIRST_BUY,
                        date="2026-01-03",
                        bar_index=10,
                        price=10.0,
                        anchor_index=9,
                        anchor_price=9.0,
                        message="x",
                        meta={"divergence_score": 0.2},
                    )
                ]
            )

    @staticmethod
    def _count_selects(store: Store, action) -> int:
        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        try:
            action()
        finally:
            store._conn.set_trace_callback(None)
        return len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")])

    def test_watchlist_select_count_does_not_grow_with_rows(self) -> None:
        self._seed(40)
        small = self._count_selects(self.store, self.service.watchlist)
        self._seed(200)
        large = self._count_selects(self.store, self.service.watchlist)
        self.assertLessEqual(
            large - small,
            2,
            f"行数 40→200，SELECT 次数 {small}→{large}：补名退回逐行单查了",
        )

    def test_pools_select_count_does_not_grow_with_signals(self) -> None:
        from chanpicker.core import BUY_KINDS

        self._seed(20)
        rows_20 = self.store.recent_signals()
        small = self._count_selects(self.store, lambda: self.service._group_recent(rows_20, BUY_KINDS))
        self._seed(100)
        rows_100 = self.store.recent_signals()
        large = self._count_selects(self.store, lambda: self.service._group_recent(rows_100, BUY_KINDS))
        self.assertLessEqual(
            large - small, 2, f"信号数 20→100，SELECT 次数 {small}→{large}：补名退回逐行单查了"
        )

    def test_candidates_select_count_does_not_grow_with_hits(self) -> None:
        self._seed(30)
        small = self._count_selects(self.store, lambda: self.service.candidates(recent_days=365))
        self._seed(150)
        large = self._count_selects(self.store, lambda: self.service.candidates(recent_days=365))
        self.assertLessEqual(
            large - small, 2, f"命中数 30→150，SELECT 次数 {small}→{large}：补名退回逐行单查了"
        )

    def test_scan_status_select_count_does_not_grow_with_hits(self) -> None:
        """hits_with_names 的直接入口。

        candidates() 在没有扫描记录时直接返回空列表，走不到补名逻辑，
        所以这条必须直接盯 scan_status() —— 它才是 hits_with_names 的主要消费者。
        """

        def make_run(run_id: str, count: int) -> None:
            self.store.create_run(run_id, scope="all", total=count)
            self.store.update_run(run_id, processed=count, hits=count)
            self.store.save_hits(
                run_id,
                [
                    {
                        "symbol": f"sh6{i:05d}",
                        "name": "",
                        "kind": "FIRST_BUY",
                        "date": "2026-10-14",
                        "price": 10.0,
                        "score": 0.3,
                        "message": "x",
                    }
                    for i in range(count)
                ],
            )

        make_run("run-a", 30)
        small = self._count_selects(self.store, lambda: self.service.scan_status("run-a"))
        make_run("run-b", 150)
        large = self._count_selects(self.store, lambda: self.service.scan_status("run-b"))
        self.assertLessEqual(
            large - small, 2, f"命中数 30→150，SELECT 次数 {small}→{large}：hits_with_names 退回逐行单查了"
        )

    def test_scan_status_still_fills_names(self) -> None:
        """批量补名不能把 name 补丢——这是正确性底线。"""

        self._seed(5)
        self.store.create_run("run-n", scope="all", total=5)
        self.store.save_hits(
            "run-n",
            [
                {
                    "symbol": f"sh6{i:05d}",
                    "name": "",
                    "kind": "FIRST_BUY",
                    "date": "2026-10-14",
                    "price": 10.0,
                    "score": 0.3,
                    "message": "x",
                }
                for i in range(5)
            ],
        )
        status = self.service.scan_status("run-n")
        names = {hit["symbol"]: hit["name"] for hit in status["hits"]}
        self.assertEqual(len(names), 5)
        for i in range(5):
            self.assertEqual(names[f"sh6{i:05d}"], f"股票{i}")


if __name__ == "__main__":
    unittest.main()
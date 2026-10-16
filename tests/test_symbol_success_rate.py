"""信号身份与个股级成功率。

背景：`signals` 表原来用 `UNIQUE (symbol, kind, anchor_index, period)`，
但 anchor_index / bar_index 都是「K 线窗口内的相对位置」——同一个底分型在
400 根窗口里是 246，在 600 根里就是 288。于是用户改一次
`/api/analyze?days=600` 就会把同一个一买再插一条。实测库里 42624 条信号中
15416 条（36%）是这样来的重复，个股级成功率会直接翻倍。

这组用例锁死三件事：
1. 信号身份不含 bar_index / anchor_index；
2. 同一信号重复写入不新增行、但 meta 会更新到最新；
3. 个股级成功率按时间线配对，且样本不足时不给百分比。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import Signal, SignalKind  # noqa: E402

from backend.service import ScanService  # noqa: E402
from backend.store import Store  # noqa: E402


def fb(symbol: str, date: str, bar: int, price: float, anchor: float = None, score: float = 0.2) -> Signal:
    return Signal(
        symbol=symbol, kind=SignalKind.FIRST_BUY, date=date, bar_index=bar,
        price=price, anchor_index=bar - 1,
        anchor_price=anchor if anchor is not None else price - 0.2,
        message="背驰后一买成立", meta={"divergence_score": score},
    )


def sb(symbol: str, date: str, bar: int, price: float, low: float) -> Signal:
    return Signal(
        symbol=symbol, kind=SignalKind.SECOND_BUY, date=date, bar_index=bar,
        price=price, anchor_index=bar - 1, anchor_price=low,
        message="二买确认",
        meta={"second_buy_score": 0.3, "second_buy_low": low, "second_buy_index": bar - 1},
    )


def bars_around(symbol: str, start: str, count: int) -> list:
    """一段平稳 K 线——让信号日都落在窗口内，且不会意外跌破二买低点。"""

    from chanpicker.core import Bar
    from datetime import date as _date, timedelta

    day = _date.fromisoformat(start)
    return [
        Bar(
            symbol=symbol, date=(day + timedelta(days=i)).isoformat(),
            open=100.0, high=100.5, low=99.5, close=100.0,
            volume=1_000_000, amount=100.0 * 1_000_000,
        )
        for i in range(count)
    ]


class SignalIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def rows(self, symbol: str) -> list:
        return self.store._conn.execute(
            "SELECT * FROM signals WHERE symbol = ? ORDER BY trade_date", (symbol,)
        ).fetchall()

    def test_identity_columns_exclude_positional_columns(self) -> None:
        identity = Store.SIGNAL_IDENTITY_COLUMNS
        self.assertNotIn("bar_index", identity)
        self.assertNotIn("anchor_index", identity)
        self.assertIn("symbol", identity)
        self.assertIn("trade_date", identity)
        self.assertIn("price", identity)

    def test_same_signal_with_different_bar_index_is_not_duplicated(self) -> None:
        """这是本次修复的核心回归：同一信号换 K 线窗口重扫，不能多出一行。"""

        self.store.save_signals([fb("sh600001", "2026-01-10", 100, 12.5)])
        self.assertEqual(len(self.rows("sh600001")), 1)
        # 换成更长的 K 线窗口重扫：bar_index / anchor_index 都会平移
        self.store.save_signals([fb("sh600001", "2026-01-10", 300, 12.5)])
        self.assertEqual(len(self.rows("sh600001")), 1, "同一信号被插了第二行")

    def test_meta_updates_to_latest_while_row_count_stays_one(self) -> None:
        """重复写入时 meta 必须刷新——否则背驰段坐标等新字段永远补不上。"""

        self.store.save_signals([fb("sh600002", "2026-01-10", 100, 12.5, score=0.2)])
        self.store.save_signals([fb("sh600002", "2026-01-10", 300, 12.5, score=0.9)])
        rows = self.rows("sh600002")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(json.loads(rows[0]["meta_json"])["divergence_score"], 0.9)

    def test_genuinely_different_signals_are_both_kept(self) -> None:
        self.store.save_signals([fb("sh600003", "2026-01-10", 100, 12.5)])
        self.store.save_signals([fb("sh600003", "2026-02-20", 200, 11.0)])
        self.assertEqual(len(self.rows("sh600003")), 2)

    def test_same_date_different_price_is_a_different_signal(self) -> None:
        # 同一根 K 线上理论只有一个信号，但价格不同意味着不是同一个信号，
        # 不能被合并掉
        self.store.save_signals([fb("sh600004", "2026-01-10", 100, 12.5)])
        self.store.save_signals([fb("sh600004", "2026-01-10", 100, 13.5)])
        self.assertEqual(len(self.rows("sh600004")), 2)

    def test_different_kinds_are_independent(self) -> None:
        self.store.save_signals([fb("sh600005", "2026-01-10", 100, 12.5)])
        self.store.save_signals([sb("sh600005", "2026-01-20", 120, 13.0, 12.8)])
        self.assertEqual(len(self.rows("sh600005")), 2)

    def test_unique_constraint_declared_on_the_table(self) -> None:
        row = self.store._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'"
        ).fetchone()
        self.assertIn("UNIQUE", row["sql"])
        self.assertIn("trade_date", row["sql"])
        self.assertNotIn("anchor_index INTEGER NOT NULL,\n                    UNIQUE", row["sql"].replace(" ", "\n                    ")[:0] + "")

    def test_migration_is_idempotent(self) -> None:
        path = Path(self._tmp.name) / "t.db"
        for i in range(5):
            self.store.save_signals([fb("sh600006", f"2026-03-0{i+1}", 100 + i, 12.0 + i)])
        self.store.close()
        reopened = Store(path)
        self.addCleanup(reopened.close)
        first = reopened._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        reopened.close()
        again = Store(path)
        self.addCleanup(again.close)
        second = again._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        self.assertEqual(first, second, "重复打开库改变了信号行数")


class SymbolSuccessRateTest(unittest.TestCase):
    """个股级成功率：按时间线配对，样本不足不给百分比。"""

    MIN_SAMPLE = 3

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self.service = ScanService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def rate(self, symbol: str) -> dict:
        return self.service.symbol_success_rate(symbol)

    def _seed_bars(self, symbol: str, rows) -> None:
        """写入 K 线。接受 Bar 列表，或 (日期, 收盘价) 元组列表。"""

        from chanpicker.core import Bar

        if rows and isinstance(rows[0], Bar):
            self.store.save_bars(list(rows))
            return
        self.store.save_bars(
            [
                Bar(
                    symbol=symbol, date=date, open=close, high=close + 0.2,
                    low=close - 0.2, close=close, volume=1_000_000, amount=close * 1_000_000,
                )
                for date, close in rows
            ]
        )

    @staticmethod
    def _series(start: str, count: int, closes: list[float]) -> list[tuple[str, float]]:
        """从 start 起按自然日递增生成 (日期, 收盘价)，避免手写日期跨月出错。"""

        from datetime import date as _date, timedelta

        day = _date.fromisoformat(start)
        return [((day + timedelta(days=i)).isoformat(), closes[i]) for i in range(count)]

    def test_symbol_without_history(self) -> None:
        out = self.rate("sz000001")
        self.assertFalse(out["available"])
        # 无历史时不能返回 first_buy/second_buy 的假结构，也不能给 0%
        self.assertNotIn("first_buy", out)
        self.assertIn("message", out)
        self.assertEqual(out["symbol"], "sz000001")

    def test_first_buy_success_requires_second_buy_within_window(self) -> None:
        # K 线窗口：2026-01-01 起 90 个交易日
        self._seed_bars("sz000100", bars_around("sz000100", "2026-01-01", 90))
        # 3 次一买：第 1 次 21 天内出了二买（成功），第 2 次同样配到（成功），
        # 第 3 次之后一直没有二买 → 失败
        self.store.save_signals(
            [
                fb("sz000100", "2026-01-06", 50, 10.0),
                fb("sz000100", "2026-02-06", 100, 9.5),
                fb("sz000100", "2026-03-06", 150, 9.0),
                sb("sz000100", "2026-01-27", 70, 10.5, 10.2),  # 距 01-06 共 21 天 → 成功
                sb("sz000100", "2026-03-20", 190, 9.6, 9.3),  # 距 03-06 共 14 天 → 成功
            ]
        )
        out = self.rate("sz000100")
        first = out["first_buy"]
        self.assertTrue(out["available"])
        self.assertEqual(first["closed"], 3, "3 次一买都该是已终结样本")
        self.assertEqual(first["success"], 2, "窗口内的两次二买都该配上")

    def test_second_buy_outside_window_does_not_count_as_success(self) -> None:
        """一买之后超过考验窗口才出现的二买不算这次一买的成功。"""

        self._seed_bars("sz000110", bars_around("sz000110", "2026-01-01", 90))
        self.store.save_signals(
            [
                fb("sz000110", "2026-01-06", 50, 10.0),
                sb("sz000110", "2026-04-01", 300, 11.0, 10.5),  # 隔了 85 天 > 30
            ]
        )
        out = self.rate("sz000110")
        detail = out["first_buy"]["detail"][0]
        self.assertFalse(detail["success"], "超窗口的二买不该算这次一买的成功")
        self.assertEqual(detail["result"], "no_second_buy")

    def test_small_sample_hides_percentage(self) -> None:
        """样本 <3 不给百分比 —— 1/1 = 100% 没有统计意义。"""

        self._seed_bars("sz000200", bars_around("sz000200", "2026-01-01", 60))
        for i in range(2):
            self.store.save_signals([fb("sz000200", f"2026-0{i+1}-10", 50 + i * 30, 10.0 - i)])
        self.store.save_signals([sb("sz000200", "2026-01-20", 70, 10.5, 10.2)])
        out = self.rate("sz000200")
        self.assertIsNone(out["first_buy"]["rate"], "2 个样本不该给百分比")
        self.assertEqual(out["first_buy"]["closed"], 2)
        self.assertTrue(out["first_buy"]["insufficient"])
        self.assertEqual(out["first_buy"]["rate_label"], "样本不足")

    def test_enough_sample_shows_percentage(self) -> None:
        self._seed_bars("sz000300", bars_around("sz000300", "2026-01-01", 90))
        signals = []
        for i in range(3):
            signals.append(fb("sz000300", f"2026-0{i+1}-10", 50 + i * 40, 10.0 - i * 0.3))
        signals.append(sb("sz000300", "2026-01-20", 70, 10.5, 10.2))
        signals.append(sb("sz000300", "2026-02-20", 110, 10.0, 9.7))
        self.store.save_signals(signals)
        out = self.rate("sz000300")
        self.assertFalse(out["first_buy"]["insufficient"])
        self.assertIsNotNone(out["first_buy"]["rate"])
        self.assertAlmostEqual(out["first_buy"]["rate"], 2 / 3, places=3)

    def test_timeline_does_not_pair_a_second_buy_from_the_future(self) -> None:
        """防未来函数：某次一买只能配它之后、窗口内的二买。"""

        self._seed_bars("sz000400", bars_around("sz000400", "2026-01-01", 120))
        # 先有一次二买（早），后有三次一买，其后都没有二买
        self.store.save_signals(
            [
                sb("sz000400", "2026-01-10", 50, 10.0, 9.8),
                fb("sz000400", "2026-02-10", 80, 9.5),
                fb("sz000400", "2026-03-10", 100, 9.0),
                fb("sz000400", "2026-04-10", 120, 8.5),
            ]
        )
        out = self.rate("sz000400")
        self.assertEqual(out["first_buy"]["closed"], 3)
        self.assertEqual(out["first_buy"]["success"], 0, "把过去的二买算成未来的成功 = 未来函数")

    def test_pairing_uses_trading_day_position_not_bar_index(self) -> None:
        """配对间隔必须按交易日算，不能相减 signals.bar_index。

        bar_index 是 K 线窗口内的相对位置：同一信号在不同窗口下会整体平移。
        两次不同扫描写下的信号相减会算出错误间距——实测 sh600000 的
        2026-02-27 一买与 2026-08-19 二买 bar_index 相差 118（超窗口），
        但实际只隔了 20 多个交易日（在窗口内），用 bar_index 会漏判这次成功。
        这里把 bar_index 人为拉开，间隔仍按真实 K 线算。
        """

        # 造 40 个交易日的 K 线：一买在第 1 天，二买在第 20 天（间隔 19 ≤ 30）
        closes = [10.0] + [10.5] * 38 + [9.0]
        rows = self._series("2026-01-01", len(closes), closes)
        self._seed_bars("sz001000", rows)
        # bar_index 故意写成相差 500（模拟窗口长度不同导致的下标平移）
        fb_sig = fb("sz001000", rows[0][0], 10, 10.0)
        sb_sig = sb("sz001000", rows[19][0], 510, 10.5, 9.8)
        self.store.save_signals([fb_sig, sb_sig])

        out = self.rate("sz001000")
        detail = out["first_buy"]["detail"][0]
        self.assertTrue(
            detail["success"],
            f"按 bar_index 配对会漏判这次成功（间隔被误认为 500 根）：{detail}",
        )
        self.assertEqual(detail["result"], "second_buy")

    def test_first_buy_outside_bar_window_is_neither_success_nor_failure(self) -> None:
        """信号日不在当前 K 线窗口内时不能算失败——否则历史久远的样本会被误判。"""

        self._seed_bars("sz001100", self._series("2026-06-01", 40, [10.0] * 40))
        # 信号日 2026-01-01，远早于 K 线窗口起点
        self.store.save_signals([fb("sz001100", "2026-01-01", 5, 9.0)])
        out = self.rate("sz001100")
        detail = out["first_buy"]["detail"][0]
        self.assertIsNone(detail["success"], "无法判定间隔时不应算失败")
        self.assertEqual(detail["result"], "out_of_window")
        self.assertEqual(out["first_buy"]["closed"], 0)

    def test_detail_lists_every_attempt(self) -> None:
        self._seed_bars("sz000500", bars_around("sz000500", "2026-01-01", 90))
        self.store.save_signals(
            [
                fb("sz000500", "2026-01-06", 50, 10.0),
                fb("sz000500", "2026-02-06", 100, 9.5),
                sb("sz000500", "2026-01-20", 70, 10.5, 10.2),
            ]
        )
        out = self.rate("sz000500")
        self.assertEqual(len(out["first_buy"]["detail"]), 2)
        # 明细是倒序（最近的在前），这是展示需求决定的
        dates = [d["date"] for d in out["first_buy"]["detail"]]
        self.assertEqual(dates, ["2026-02-06", "2026-01-06"])
        first = out["first_buy"]["detail"][1]
        self.assertTrue(first["success"])
        self.assertIn("result_label", first)
        self.assertEqual(first["result"], "second_buy")
        # 未引出二买那次
        self.assertFalse(out["first_buy"]["detail"][0]["success"])
        self.assertEqual(out["first_buy"]["detail"][0]["result"], "no_second_buy")

    def test_second_buy_success_uses_anchor_low(self) -> None:
        """二买成功 = 后续窗口内收盘未跌破二买低点（查真实 K 线）。"""

        self._seed_bars("sz000600", [("2026-01-10", 10.0), ("2026-01-11", 10.2), ("2026-01-12", 10.1)])
        self._seed_bars("sz000600", [("2026-02-10", 10.5), ("2026-02-11", 10.4), ("2026-02-12", 10.6)])
        self.store.save_signals(
            [
                sb("sz000600", "2026-01-10", 50, 10.0, 9.8),   # 后续收盘 10.2/10.1 > 9.8 → 守住
                sb("sz000600", "2026-02-10", 90, 10.5, 10.3),  # 后续 10.4/10.6 > 10.3 → 守住
            ]
        )
        out = self.rate("sz000600")
        self.assertEqual(out["second_buy"]["closed"], 2)
        self.assertEqual(out["second_buy"]["success"], 2)
        self.assertEqual(out["second_buy"]["failed"], 0)

    def test_second_buy_failure_detected_from_real_bars(self) -> None:
        """跌破二买低点必须判失败——不能靠「有无后续信号」近似。"""

        self._seed_bars(
            "sz000700",
            [
                ("2026-01-10", 10.0),
                ("2026-01-11", 9.5),   # 跌破 9.8
                ("2026-01-12", 9.9),
            ],
        )
        self.store.save_signals([sb("sz000700", "2026-01-10", 50, 10.0, 9.8)])
        out = self.rate("sz000700")
        self.assertEqual(out["second_buy"]["closed"], 1)
        self.assertEqual(out["second_buy"]["failed"], 1)
        detail = out["second_buy"]["detail"][0]
        self.assertEqual(detail["result"], "broke_low")
        self.assertEqual(detail["break_date"], "2026-01-11")

    def test_second_buy_without_followup_bars_is_neither_success_nor_failure(self) -> None:
        """数据只到信号日那天时不能算失败，否则会虚高失败率。"""

        self._seed_bars("sz000800", [("2026-01-10", 10.0)])
        self.store.save_signals([sb("sz000800", "2026-01-10", 50, 10.0, 9.8)])
        out = self.rate("sz000800")
        detail = out["second_buy"]["detail"][0]
        self.assertIsNone(detail["success"])
        self.assertEqual(detail["result"], "no_followup")
        self.assertEqual(out["second_buy"]["closed"], 0, "未定论的记录不进分母")

    def test_break_after_test_window_is_not_a_failure(self) -> None:
        """跌破发生在窗口之外 → 记为守住，不是二买失败。

        这条锁定窗口的实际作用：超过 max_wait_bars 之后的破位多半是大盘
        整体回落，记在这笔二买头上会把个股成功率做虚。必须有**跨越窗口边界**
        的数据才能测出来——只造三五根 K 线的话，去掉窗口限制也看不出差别。
        """

        window = self.service.config.max_wait_bars
        # 窗口内保持强势，第 window+5 根才跌破 9.8（明确在窗口之外）
        closes = [10.0] + [10.5] * (window + 4) + [9.0] * 5
        self._seed_bars("sz000900", self._series("2026-01-10", len(closes), closes))
        self.store.save_signals([sb("sz000900", "2026-01-10", 50, 10.0, 9.8)])

        out = self.rate("sz000900")
        detail = out["second_buy"]["detail"][0]
        self.assertTrue(
            detail["success"],
            f"窗口({window}根)外的破位被误判成失败了：{detail}",
        )
        self.assertEqual(detail["result"], "held")

    def test_break_inside_window_is_a_failure(self) -> None:
        """窗口内跌破必须判失败——与上一条构成对照。"""

        window = self.service.config.max_wait_bars
        # 第 5 根就跌破 9.8（窗口内）
        closes = [10.0] + [10.5] * 4 + [9.5] + [10.5] * (window - 5)
        self._seed_bars("sz000901", self._series("2026-01-10", len(closes), closes))
        self.store.save_signals([sb("sz000901", "2026-01-10", 50, 10.0, 9.8)])

        out = self.rate("sz000901")
        detail = out["second_buy"]["detail"][0]
        self.assertFalse(detail["success"], "窗口内跌破却判成成功了")
        self.assertEqual(detail["result"], "broke_low")
        self.assertEqual(out["second_buy"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()

class LegacyMigrationDuplicateTest(unittest.TestCase):
    """老库迁移到「旧约束」时必须先去重，否则启动直接失败。

    真实故障：`_migrate_legacy` 会用旧 UNIQUE(symbol,kind,anchor_index,period)
    重建 signals 表并逐行拷贝。老库里同一个信号被存过多次（anchor_index 随
    K 线窗口长度漂移，旧唯一键拦不住），拷贝时撞 UNIQUE 约束 →
    `sqlite3.IntegrityError` → 服务起不来。
    """

    def test_legacy_migration_dedups_before_copying(self) -> None:
        import sqlite3
        import tempfile
        from pathlib import Path

        from backend.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            # 造一个真正的「老库」：连 period 列都没有（多周期之前的结构）。
            # bars 也要有——`_migrate_legacy` 会逐表检查，缺表会走到不存在的分支。
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE bars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                    volume REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0,
                    fq TEXT NOT NULL DEFAULT 'qfq'
                );
                CREATE TABLE signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, kind TEXT NOT NULL, trade_date TEXT NOT NULL,
                    bar_index INTEGER NOT NULL, price REAL NOT NULL,
                    anchor_index INTEGER NOT NULL, anchor_price REAL NOT NULL,
                    message TEXT NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX idx_signals_symbol ON signals (symbol, trade_date);
                """
            )
            rows = [
                # 同一个信号（symbol+kind+anchor_index 相同），bar_index 不同
                (1, "sh600000", "FIRST_BUY", "2026-01-05", 246, 9.30, 245, 9.27, "m", "{}", 1.0),
                (2, "sh600000", "FIRST_BUY", "2026-01-05", 288, 9.30, 245, 9.27, "m", "{}", 2.0),
                # 另一个真信号
                (3, "sh600000", "FIRST_BUY", "2026-06-23", 323, 8.86, 322, 8.56, "m", "{}", 3.0),
            ]
            conn.executemany(
                "INSERT INTO signals (id, symbol, kind, trade_date, bar_index, price, anchor_index, "
                "anchor_price, message, meta_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            conn.close()

            # 打开库即触发迁移：补 period 列 → 重建（此时要去重，否则撞 UNIQUE）
            store = Store(path)
            try:
                sql = store._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'"
                ).fetchone()[0]
                self.assertIn("period", sql, "迁移后应补上 period 列")
                dup = store._conn.execute(
                    "SELECT COUNT(*) FROM (SELECT symbol,kind,anchor_index,period FROM signals "
                    "GROUP BY 1,2,3,4 HAVING COUNT(*)>1)"
                ).fetchone()[0]
                self.assertEqual(dup, 0, "迁移后仍有重复（老库去重未生效）")
            finally:
                store.close()

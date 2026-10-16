"""Store 层测试：多周期迁移 + 并发读一致性。

并发用例对应一个真实缺陷：sqlite3 连接不是线程安全的，FastAPI 同步端点跑在
线程池里，两个并发请求会互相读到对方的行（现象是 /api/market/pools 的
trading_dates 变成空数组、/api/market/risk 里混进空字符串）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chanpicker.core import Bar, Signal, SignalKind  # noqa: E402

from store import Store  # noqa: E402

LEGACY_SCHEMA = """
CREATE TABLE symbols (symbol TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE bars (
    symbol TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
    low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0, fq TEXT NOT NULL DEFAULT 'qfq',
    PRIMARY KEY (symbol, trade_date, fq)
);
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, kind TEXT NOT NULL,
    trade_date TEXT NOT NULL, bar_index INTEGER NOT NULL, price REAL NOT NULL,
    anchor_index INTEGER NOT NULL, anchor_price REAL NOT NULL, message TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
    UNIQUE (symbol, kind, anchor_index)
);
CREATE INDEX idx_bars_symbol ON bars (symbol, trade_date);
CREATE INDEX idx_signals_symbol ON signals (symbol, trade_date);
"""


def make_legacy_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO bars (symbol, trade_date, open, high, low, close, volume, amount, fq) "
        "VALUES (?, ?, 1, 2, 0.5, 1.5, 10, 100, 'qfq')",
        [(f"sh60{i:04d}", "2026-10-1%d" % (i % 5), ) for i in range(24)],
    )
    conn.executemany(
        "INSERT INTO signals (symbol, kind, trade_date, bar_index, price, anchor_index, anchor_price, "
        "message, meta_json, created_at) VALUES (?, 'FIRST_BUY', '2026-10-01', 5, 10, 4, 9.5, 'm', "
        "'{\"divergence_score\": 0.5}', 0)",
        [(f"sh60{i:04d}",) for i in range(6)],
    )
    conn.commit()
    conn.close()


class MigrationTest(unittest.TestCase):
    def test_legacy_db_is_upgraded_and_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "legacy.db"
            make_legacy_db(db)
            store = Store(db)

            counts = store.period_counts()
            self.assertEqual(counts["bars"], {"day": 24})
            self.assertEqual(counts["signals"], {"day": 6})

            # period 索引与唯一约束都重建成功，说明 period 列已真正加入
            self.assertEqual(store.recent_trading_dates(3), ["2026-10-14", "2026-10-13", "2026-10-12"])

            # 升级后仍可写入新周期数据，且与日线互不干扰
            store.save_bars(
                [Bar(symbol="sh600519", date="2026-10-15 10:00:00", open=1, high=2, low=0.5, close=1.5, period="m15")],
                period="m15",
            )
            self.assertEqual(len(store.load_bars("sh600519", period="m15")), 1)
            self.assertEqual(len(store.load_bars("sh600519", period="day")), 0)

    def test_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "legacy.db"
            make_legacy_db(db)
            Store(db).close()
            store = Store(db)  # 第二次打开不应重复迁移或丢数据
            self.assertEqual(store.period_counts()["bars"], {"day": 24})
            store.close()

    def test_signals_are_isolated_per_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.save_signals(
                [
                    Signal(
                        symbol="sh600519",
                        kind=SignalKind.FIRST_SELL,
                        date="2026-10-15",
                        bar_index=10,
                        price=100.0,
                        anchor_index=9,
                        anchor_price=101.0,
                        message="顶背驰",
                        meta={"top_divergence_score": 0.42},
                        period="day",
                    )
                ]
            )
            store.save_signals(
                [
                    Signal(
                        symbol="sh600519",
                        kind=SignalKind.FIRST_SELL,
                        date="2026-10-15 10:15:00",
                        bar_index=10,
                        price=100.0,
                        anchor_index=9,
                        anchor_price=101.0,
                        message="顶背驰",
                        meta={"top_divergence_score": 0.42},
                        period="m15",
                    )
                ]
            )
            # 同 symbol/kind/anchor_index 但不同 period：应各存一条，不互相覆盖
            self.assertEqual(len(store.sell_signals_since("2026-01-01", period="day")), 1)
            self.assertEqual(len(store.sell_signals_since("2026-01-01", period="m15")), 1)
            self.assertEqual(len(store.sell_signals_since("2026-01-01", period=None)), 2)
            store.close()


class ConcurrentReadTest(unittest.TestCase):
    def test_parallel_queries_do_not_cross_contaminate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            for i in range(12):
                store.save_bars(
                    [
                        Bar(
                            symbol=f"sh60{i:04d}",
                            date=f"2026-10-{d:02d}",
                            open=1,
                            high=2,
                            low=0.5,
                            close=1.5,
                            period="day",
                        )
                        for d in range(1, 6)
                    ]
                )
            store.save_signals(
                [
                    Signal(
                        symbol=f"sh60{i:04d}",
                        kind=SignalKind.FIRST_BUY,
                        date="2026-10-05",
                        bar_index=4,
                        price=1.5,
                        anchor_index=3,
                        anchor_price=1.4,
                        message="m",
                        meta={"divergence_score": 0.1 * i},
                    )
                    for i in range(12)
                ]
            )

            errors: list[str] = []
            stop = threading.Event()

            def reader_worker() -> None:
                while not stop.is_set():
                    dates = store.recent_trading_dates(3)
                    if dates != ["2026-10-05", "2026-10-04", "2026-10-03"]:
                        errors.append(f"recent_trading_dates 串扰: {dates!r}")

            def signal_worker() -> None:
                while not stop.is_set():
                    rows = store.buy_signals_since("2026-01-01")
                    if len(rows) != 12:
                        errors.append(f"buy_signals_since 行数异常: {len(rows)}")
                    if any(not r["symbol"] for r in rows):
                        errors.append("buy_signals_since 出现空 symbol")

            def writer_worker() -> None:
                i = 0
                while not stop.is_set():
                    store.upsert_symbol(f"sz00{i % 50:04d}", f"名称{i % 50}")
                    i += 1

            threads = [
                threading.Thread(target=reader_worker),
                threading.Thread(target=reader_worker),
                threading.Thread(target=signal_worker),
                threading.Thread(target=signal_worker),
                threading.Thread(target=writer_worker),
            ]
            for t in threads:
                t.start()
            threading.Event().wait(1.5)
            stop.set()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(errors[:5], [], f"并发读出现 {len(errors)} 次串扰")
            store.close()


if __name__ == "__main__":
    unittest.main()

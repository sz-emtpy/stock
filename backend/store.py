"""SQLite persistence for ChanPicker backend.

Tables:
- symbols: cached symbol -> name mapping
- bars: daily adjusted OHLCV cache
- signals: historical first/second buy signals
- watchlist: focus pool state machine
- scan_runs / scan_hits: market scan task state and results
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from chanpicker.core import BUY_KINDS, Bar, Signal, SignalKind, WatchItem, WatchState


def default_db_path() -> Path:
    """数据库路径：优先环境变量 CHANPICKER_DB，默认 <项目>/data/chanpicker.db。"""

    env_path = os.getenv("CHANPICKER_DB")
    if env_path:
        return Path(env_path).expanduser()
    return Path(__file__).resolve().parent.parent / "data" / "chanpicker.db"


DEFAULT_DB_PATH = default_db_path()


class Store:
    def __init__(self, db_path: Union[str, Path] = DEFAULT_DB_PATH) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS symbols (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL DEFAULT 0,
                    amount REAL NOT NULL DEFAULT 0,
                    fq TEXT NOT NULL DEFAULT 'qfq',
                    PRIMARY KEY (symbol, trade_date, fq)
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    bar_index INTEGER NOT NULL,
                    price REAL NOT NULL,
                    anchor_index INTEGER NOT NULL,
                    anchor_price REAL NOT NULL,
                    message TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    UNIQUE (symbol, kind, anchor_index)
                );
                CREATE TABLE IF NOT EXISTS watchlist (
                    symbol TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    first_buy_date TEXT NOT NULL,
                    first_buy_index INTEGER NOT NULL,
                    first_buy_price REAL NOT NULL,
                    first_buy_low REAL NOT NULL,
                    last_update_date TEXT NOT NULL,
                    second_buy_date TEXT,
                    second_buy_price REAL,
                    exit_date TEXT,
                    exit_reason TEXT,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS scan_runs (
                    run_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0,
                    hits INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    finished_at REAL
                );
                CREATE TABLE IF NOT EXISTS scan_hits (
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    price REAL NOT NULL,
                    score REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (run_id, symbol, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_bars_symbol ON bars (symbol, trade_date);
                CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol, trade_date);
                CREATE INDEX IF NOT EXISTS idx_hits_run ON scan_hits (run_id);
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert_symbol(self, symbol: str, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO symbols (symbol, name, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at",
                (symbol, name, time.time()),
            )
            self._conn.commit()

    def symbol_name(self, symbol: str) -> str:
        row = self._conn.execute("SELECT name FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
        return row["name"] if row else symbol

    def all_symbols(self) -> list[dict[str, str]]:
        rows = self._conn.execute("SELECT symbol, name FROM symbols ORDER BY symbol").fetchall()
        return [{"symbol": row["symbol"], "name": row["name"]} for row in rows]

    def save_bars(self, bars: Iterable[Bar], fq: str = "qfq") -> int:
        payload = [
            (bar.symbol, bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount, fq)
            for bar in bars
        ]
        if not payload:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO bars (symbol, trade_date, open, high, low, close, volume, amount, fq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, trade_date, fq) DO UPDATE SET "
                "open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close, "
                "volume = excluded.volume, amount = excluded.amount",
                payload,
            )
            self._conn.commit()
        return len(payload)

    def load_bars(self, symbol: str, fq: str = "qfq", limit: int = 1000) -> list[Bar]:
        rows = self._conn.execute(
            "SELECT symbol, trade_date, open, high, low, close, volume, amount FROM bars "
            "WHERE symbol = ? AND fq = ? ORDER BY trade_date DESC LIMIT ?",
            (symbol, fq, limit),
        ).fetchall()
        bars = [
            Bar(
                symbol=row["symbol"],
                date=row["trade_date"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                amount=row["amount"],
            )
            for row in rows
        ]
        return sorted(bars, key=lambda item: item.date)

    def latest_bar_date(self, symbol: str, fq: str = "qfq") -> Optional[str]:
        row = self._conn.execute(
            "SELECT MAX(trade_date) AS latest FROM bars WHERE symbol = ? AND fq = ?", (symbol, fq)
        ).fetchone()
        return row["latest"] if row and row["latest"] else None

    def save_signals(self, signals: Iterable[Signal]) -> int:
        payload = [
            (
                signal.symbol,
                signal.kind.value,
                signal.date,
                signal.bar_index,
                signal.price,
                signal.anchor_index,
                signal.anchor_price,
                signal.message,
                json.dumps(signal.meta, ensure_ascii=False),
                time.time(),
            )
            for signal in signals
        ]
        if not payload:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO signals (symbol, kind, trade_date, bar_index, price, anchor_index, anchor_price, "
                "message, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (symbol, kind, anchor_index) DO UPDATE SET "
                "trade_date = excluded.trade_date, price = excluded.price, meta_json = excluded.meta_json",
                payload,
            )
            self._conn.commit()
        return len(payload)

    def recent_signals(self, kind: Optional[SignalKind] = None, limit: int = 200) -> List[dict]:
        if kind is None:
            rows = self._conn.execute(
                "SELECT * FROM signals ORDER BY trade_date DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM signals WHERE kind = ? ORDER BY trade_date DESC, id DESC LIMIT ?",
                (kind.value, limit),
            ).fetchall()
        return [self._signal_row_to_dict(row) for row in rows]

    def recent_trading_dates(self, limit: int = 3) -> list[str]:
        """最近 N 个交易日（降序），取自本地 K 线缓存的日期集合。"""

        rows = self._conn.execute(
            "SELECT DISTINCT trade_date FROM bars ORDER BY trade_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [row["trade_date"] for row in rows]

    def buy_signals_since(self, cutoff: str) -> list[dict]:
        """返回 >= cutoff 日期的买点类信号（一买/二买/三买），按日期降序。"""

        kinds = [kind.value for kind in BUY_KINDS]
        placeholders = ",".join("?" for _ in kinds)
        rows = self._conn.execute(
            f"SELECT * FROM signals WHERE kind IN ({placeholders}) AND trade_date >= ? "
            "ORDER BY trade_date DESC, id DESC",
            (*kinds, cutoff),
        ).fetchall()
        return [self._signal_row_to_dict(row) for row in rows]

    def signals_for_symbol(self, symbol: str, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM signals WHERE symbol = ? ORDER BY bar_index DESC LIMIT ?", (symbol, limit)
        ).fetchall()
        return [self._signal_row_to_dict(row) for row in rows]

    @staticmethod
    def _signal_row_to_dict(row: sqlite3.Row) -> dict:
        meta = json.loads(row["meta_json"] or "{}")
        score = meta.get("divergence_score", meta.get("second_buy_score", meta.get("third_buy_score", 0)))
        return {
            "symbol": row["symbol"],
            "name": "",
            "kind": row["kind"],
            "date": row["trade_date"],
            "price": row["price"],
            "anchor_price": row["anchor_price"],
            "score": float(score or 0),
            "message": row["message"],
            "meta": meta,
        }

    def save_watch_items(self, items: Iterable[WatchItem]) -> None:
        payload = [
            (
                item.symbol,
                item.state.value,
                item.first_buy_date,
                item.first_buy_index,
                item.first_buy_price,
                item.first_buy_low,
                item.last_update_date,
                item.second_buy_date,
                item.second_buy_price,
                item.exit_date,
                item.exit_reason,
                json.dumps(item.meta, ensure_ascii=False),
            )
            for item in items
        ]
        if not payload:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO watchlist (symbol, state, first_buy_date, first_buy_index, first_buy_price, "
                "first_buy_low, last_update_date, second_buy_date, second_buy_price, exit_date, exit_reason, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET state = excluded.state, last_update_date = excluded.last_update_date, "
                "second_buy_date = excluded.second_buy_date, second_buy_price = excluded.second_buy_price, "
                "exit_date = excluded.exit_date, exit_reason = excluded.exit_reason, meta_json = excluded.meta_json",
                payload,
            )
            self._conn.commit()

    def load_watch_items(self, symbol: Optional[str] = None) -> List[WatchItem]:
        if symbol:
            rows = self._conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM watchlist ORDER BY last_update_date DESC").fetchall()
        items = []
        for row in rows:
            items.append(
                WatchItem(
                    symbol=row["symbol"],
                    state=WatchState(row["state"]),
                    first_buy_date=row["first_buy_date"],
                    first_buy_index=row["first_buy_index"],
                    first_buy_price=row["first_buy_price"],
                    first_buy_low=row["first_buy_low"],
                    last_update_date=row["last_update_date"],
                    second_buy_date=row["second_buy_date"],
                    second_buy_price=row["second_buy_price"],
                    exit_date=row["exit_date"],
                    exit_reason=row["exit_reason"],
                    meta=json.loads(row["meta_json"] or "{}"),
                )
            )
        return items

    def create_run(self, run_id: str, scope: str, total: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO scan_runs (run_id, scope, status, total, processed, hits, message, started_at) "
                "VALUES (?, ?, 'running', ?, 0, 0, '', ?)",
                (run_id, scope, total, time.time()),
            )
            self._conn.commit()

    def update_run(self, run_id: str, processed: int, hits: int, status: str = "running", message: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE scan_runs SET processed = ?, hits = ?, status = ?, message = ? WHERE run_id = ?",
                (processed, hits, status, message, run_id),
            )
            self._conn.commit()

    def finish_run(self, run_id: str, status: str, message: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE scan_runs SET status = ?, message = ?, finished_at = ? WHERE run_id = ?",
                (status, message, time.time(), run_id),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM scan_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest_run(self) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def save_hits(self, run_id: str, hits: Iterable[dict]) -> None:
        payload = [
            (run_id, hit["symbol"], hit.get("name", ""), hit["kind"], hit["date"], hit["price"], hit.get("score", 0), hit.get("message", ""))
            for hit in hits
        ]
        if not payload:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO scan_hits (run_id, symbol, name, kind, trade_date, price, score, message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (run_id, symbol, kind) DO UPDATE SET "
                "name = excluded.name, trade_date = excluded.trade_date, price = excluded.price, score = excluded.score",
                payload,
            )
            self._conn.commit()

    def hits_for_run(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM scan_hits WHERE run_id = ? ORDER BY score DESC", (run_id,)
        ).fetchall()
        hits = []
        for row in rows:
            hit = dict(row)
            hit["date"] = hit.pop("trade_date")
            hits.append(hit)
        return hits

    def clear_run_hits(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM scan_hits WHERE run_id = ?", (run_id,))
            self._conn.commit()

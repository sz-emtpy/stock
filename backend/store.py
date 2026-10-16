"""SQLite persistence for ChanPicker backend.

Tables:
- symbols: cached symbol -> name mapping
- bars: daily adjusted OHLCV cache
- signals: historical first/second buy signals
- watchlist: focus pool state machine
- scan_runs / scan_hits: market scan task state and results
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from chanpicker.core import (
    BUY_KINDS,
    SELL_KINDS,
    Bar,
    Signal,
    SignalKind,
    WatchItem,
    WatchState,
    signal_score_of,
)

DEFAULT_PERIOD = "day"


def default_db_path() -> Path:
    """数据库路径：优先环境变量 CHANPICKER_DB，默认 <项目>/data/chanpicker.db。"""

    env_path = os.getenv("CHANPICKER_DB")
    if env_path:
        return Path(env_path).expanduser()
    return Path(__file__).resolve().parent.parent / "data" / "chanpicker.db"


DEFAULT_DB_PATH = default_db_path()


def _sync(func):
    """串行化对共享 sqlite 连接的访问。

    sqlite3 连接不是线程安全的：FastAPI 的同步端点跑在线程池里，多个请求同时
    execute 会让彼此的游标串行错乱（曾观察到两个并发接口互相读到了对方的行，
    返回的数组里混入空串/丢失元素）。这里统一加锁，RLock 允许方法内部再嵌套加锁。
    """

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return func(self, *args, **kwargs)

    return wrapper


class Store:
    def __init__(self, db_path: Union[str, Path] = DEFAULT_DB_PATH) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # RLock：方法内部允许嵌套加锁（如 _init_schema -> _migrate_legacy）
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    @_sync
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
                    period TEXT NOT NULL DEFAULT 'day',
                    PRIMARY KEY (symbol, trade_date, fq, period)
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
                    period TEXT NOT NULL DEFAULT 'day',
                    UNIQUE (symbol, kind, anchor_index, period)
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
            # 必须在迁移之后才建 period 索引：老表尚无该列。
            self._migrate_legacy()
            self._migrate_signal_identity()
            self._conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_bars_period ON bars (symbol, period, trade_date);
                CREATE INDEX IF NOT EXISTS idx_signals_period ON signals (period, trade_date);
                -- period 必须做前导列：recent_trading_dates / period_counts 都是
                -- 「只按 period 过滤」的跨股查询（`SELECT DISTINCT trade_date ... WHERE period=?`
                -- 和 `GROUP BY period`）。原索引 (symbol, period, trade_date) 的前导列是 symbol，
                -- 这类查询用不上索引，会全扫 bars 两百多万行 —— 实测 8990ms / 7809ms。
                -- 加上 (period, trade_date) 覆盖索引后降到 0.2ms / 55ms。
                CREATE INDEX IF NOT EXISTS idx_bars_period_date ON bars (period, trade_date);
                -- signals 的买点池查询同时过滤 period + kind + trade_date 区间，
                -- 把 kind 放进索引省掉回表。
                CREATE INDEX IF NOT EXISTS idx_signals_period_kind_date
                    ON signals (period, kind, trade_date);
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # 一次性迁移：老库（无 period 列）就地升级为多周期结构
    # ------------------------------------------------------------------

    @staticmethod
    def _unique_index_columns(conn: sqlite3.Connection, table: str) -> List[tuple]:
        """列出表上所有唯一索引的列组合（含主键 autoindex）。"""

        combos: List[tuple] = []
        for row in conn.execute(f"PRAGMA index_list({table})"):
            if not row[2]:  # unique flag
                continue
            columns = tuple(info[2] for info in conn.execute(f"PRAGMA index_info({row[1]})"))
            combos.append(columns)
        return combos

    @_sync
    def _run_script(self, statements: List[str]) -> None:
        """在单个事务内执行语句序列，失败整体回滚。"""

        self._conn.execute("BEGIN")
        try:
            for statement in statements:
                self._conn.execute(statement)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    @_sync
    def _migrate_legacy(self) -> None:
        """把单周期老库升级为多周期库。

        `CREATE TABLE IF NOT EXISTS` 不会修改已存在的表，因此老库必须显式重建：
        1. 补齐 `period` 列；
        2. 重建唯一约束（bars 主键、signals UNIQUE）以纳入 period；
        3. 把存量数据的 period 回填为 'day'。
        否则 30 万+ 存量 K 线和 1700+ 信号会被判定为「当前周期无数据」，
        触发重新全量拉取（数据源有限流，代价很高）。
        """

        bars_keys = self._unique_index_columns(self._conn, "bars")
        if ("symbol", "trade_date", "fq", "period") not in bars_keys:
            self._run_script(
                [
                    "DROP INDEX IF EXISTS idx_bars_symbol",
                    "DROP INDEX IF EXISTS idx_bars_period",
                    "ALTER TABLE bars RENAME TO bars_legacy",
                    """
                    CREATE TABLE bars (
                        symbol TEXT NOT NULL,
                        trade_date TEXT NOT NULL,
                        open REAL NOT NULL,
                        high REAL NOT NULL,
                        low REAL NOT NULL,
                        close REAL NOT NULL,
                        volume REAL NOT NULL DEFAULT 0,
                        amount REAL NOT NULL DEFAULT 0,
                        fq TEXT NOT NULL DEFAULT 'qfq',
                        period TEXT NOT NULL DEFAULT 'day',
                        PRIMARY KEY (symbol, trade_date, fq, period)
                    )
                    """,
                    "INSERT INTO bars (symbol, trade_date, open, high, low, close, volume, amount, fq, period) "
                    "SELECT symbol, trade_date, open, high, low, close, volume, amount, fq, 'day' FROM bars_legacy",
                    "DROP TABLE bars_legacy",
                    "CREATE INDEX IF NOT EXISTS idx_bars_symbol ON bars (symbol, trade_date)",
                    "CREATE INDEX IF NOT EXISTS idx_bars_period ON bars (symbol, period, trade_date)",
                ]
            )

        signal_keys = self._unique_index_columns(self._conn, "signals")
        if ("symbol", "kind", "anchor_index", "period") not in signal_keys:
            self._run_script(
                [
                    "DROP INDEX IF EXISTS idx_signals_symbol",
                    "DROP INDEX IF EXISTS idx_signals_period",
                    "ALTER TABLE signals RENAME TO signals_legacy",
                    """
                    CREATE TABLE signals (
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
                        period TEXT NOT NULL DEFAULT 'day',
                        UNIQUE (symbol, kind, anchor_index, period)
                    )
                    """,
                    # 拷贝前必须先去重：老库里同一个信号可能被存过多次
                    # （anchor_index 随 K 线窗口长度漂移，唯一键拦不住），
                    # 直接 INSERT 会撞 UNIQUE 约束并让整个启动失败。
                    # 保留 id 最小的行——同组内除 bar_index 外字段一致。
                    # 注意：老表还没有 period 列（这一段迁移才刚补上），
                    # 所以去重键里不能出现 period。
                    "INSERT INTO signals (id, symbol, kind, trade_date, bar_index, price, anchor_index, "
                    "anchor_price, message, meta_json, created_at, period) "
                    "SELECT id, symbol, kind, trade_date, bar_index, price, anchor_index, anchor_price, "
                    "message, meta_json, created_at, 'day' FROM signals_legacy "
                    "WHERE id IN (SELECT MIN(id) FROM signals_legacy "
                    "GROUP BY symbol, kind, anchor_index)",
                    "DROP TABLE signals_legacy",
                    "CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol, trade_date)",
                    "CREATE INDEX IF NOT EXISTS idx_signals_period ON signals (period, trade_date)",
                ]
            )

    # ------------------------------------------------------------------
    # 信号唯一性：bar_index 不能作为信号身份的一部分
    # ------------------------------------------------------------------

    # 一个信号的真实身份 = 代码 + 类型 + 周期 + 信号日 + 信号价 + 锚定价。
    #
    # 为什么不能包含 bar_index：它是「K 线窗口内的相对位置」，同一个底分型在
    # 400 根窗口里是 index 246、在 600 根窗口里就是 index 288。原 UNIQUE 约束
    # `(symbol, kind, anchor_index, period)` 因此拦不住——用户改一次
    # `/api/analyze?days=600` 就会把同一个一买再插一条。实测库里 42624 条
    # 信号里有 15416 条是这样来的重复，个股级成功率会直接算错一倍。
    SIGNAL_IDENTITY_COLUMNS = ("symbol", "kind", "period", "trade_date", "price", "anchor_price")

    @_sync
    def _migrate_signal_identity(self) -> int:
        """把 signals 收敛到「一行即一个信号」，并把唯一约束换成 SIGNAL_IDENTITY_COLUMNS。

        幂等：已收敛的库直接返回 0。
        """

        identity = ", ".join(self.SIGNAL_IDENTITY_COLUMNS)
        current = self._unique_index_columns(self._conn, "signals")
        if tuple(self.SIGNAL_IDENTITY_COLUMNS) in current:
            return 0

        before = self._conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
        # 去重与重建必须放在同一个事务里，失败整体回滚——
        # 中途失败会留下「已去重但约束还没换」的半成品状态。
        # 注意不能复用 _run_script：上面这条 DELETE 已经开启了隐式事务，
        # sqlite3 不允许在事务内再 BEGIN。
        self._conn.execute("BEGIN")
        try:
            # 保留 id 最小的行：同组内除 bar_index 外字段完全一致，删哪条都不丢信息。
            self._conn.execute(
                f"DELETE FROM signals WHERE id NOT IN "
                f"(SELECT MIN(id) FROM signals GROUP BY {identity})"
            )
            # 表结构重建：UNIQUE 约束在 SQLite 里只能随建表声明，改不了。
            statements = [
                "DROP INDEX IF EXISTS idx_signals_symbol",
                "DROP INDEX IF EXISTS idx_signals_period",
                "ALTER TABLE signals RENAME TO signals_legacy",
                """
                CREATE TABLE signals (
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
                    period TEXT NOT NULL DEFAULT 'day',
                    UNIQUE (symbol, kind, period, trade_date, price, anchor_price)
                )
                """,
                f"INSERT INTO signals (id, symbol, kind, trade_date, bar_index, price, anchor_index, anchor_price, "
                f"message, meta_json, created_at, period) "
                f"SELECT id, symbol, kind, trade_date, bar_index, price, anchor_index, anchor_price, "
                f"message, meta_json, created_at, period FROM signals_legacy",
                "DROP TABLE signals_legacy",
                "CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol, trade_date)",
                "CREATE INDEX IF NOT EXISTS idx_signals_period ON signals (period, trade_date)",
            ]
            for statement in statements:
                self._conn.execute(statement)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        removed = before - self._conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
        self._conn.commit()
        return removed

    @_sync
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @_sync
    def upsert_symbol(self, symbol: str, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO symbols (symbol, name, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at",
                (symbol, name, time.time()),
            )
            self._conn.commit()

    @_sync
    def symbol_name(self, symbol: str) -> str:
        row = self._conn.execute("SELECT name FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
        return row["name"] if row else symbol

    @_sync
    def symbol_names(self, symbols: Iterable[str]) -> Dict[str, str]:
        """批量查名：一次 `WHERE symbol IN (...)` 取回整批。

        为什么必须有这个方法：候选表（8974 条）、关注圈（4000+ 条）、买卖点池都是
        「逐行补一个 name」，用 symbol_name() 单查就是 N+1 次往返。本地 SQLite 上
        单次 <0.05ms 还能忍，一旦换到 MySQL（哪怕是 127.0.0.1）就会放大成秒级。
        批量后无论多少行都是一次查询。

        SQLite 的变量数上限是 999（默认），MySQL 更高；这里按 500 分批，
        既能过 SQLite 上限，也避免一次 IN 列表过长导致 SQL 文本失控。
        """

        wanted = list(dict.fromkeys(s for s in symbols if s))
        if not wanted:
            return {}
        result: Dict[str, str] = {}
        chunk = 500
        for offset in range(0, len(wanted), chunk):
            batch = wanted[offset : offset + chunk]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(
                f"SELECT symbol, name FROM symbols WHERE symbol IN ({placeholders})", tuple(batch)
            ).fetchall()
            for row in rows:
                result[row["symbol"]] = row["name"]
        # 查不到的代码回退成代码本身，保持与单查 symbol_name() 一致的语义
        for symbol in wanted:
            result.setdefault(symbol, symbol)
        return result

    @_sync
    def all_symbols(self) -> list[dict[str, str]]:
        rows = self._conn.execute("SELECT symbol, name FROM symbols ORDER BY symbol").fetchall()
        return [{"symbol": row["symbol"], "name": row["name"]} for row in rows]

    @_sync
    def save_bars(self, bars: Iterable[Bar], fq: str = "qfq", period: Optional[str] = None) -> int:
        payload = [
            (
                bar.symbol,
                bar.date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.amount,
                fq,
                period or bar.period or DEFAULT_PERIOD,
            )
            for bar in bars
        ]
        if not payload:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO bars (symbol, trade_date, open, high, low, close, volume, amount, fq, period) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, trade_date, fq, period) DO UPDATE SET "
                "open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close, "
                "volume = excluded.volume, amount = excluded.amount",
                payload,
            )
            self._conn.commit()
        return len(payload)

    @_sync
    def load_bars(
        self, symbol: str, fq: str = "qfq", limit: int = 1000, period: str = DEFAULT_PERIOD
    ) -> list[Bar]:
        rows = self._conn.execute(
            "SELECT symbol, trade_date, open, high, low, close, volume, amount, period FROM bars "
            "WHERE symbol = ? AND fq = ? AND period = ? ORDER BY trade_date DESC LIMIT ?",
            (symbol, fq, period, limit),
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
                period=row["period"],
            )
            for row in rows
        ]
        return sorted(bars, key=lambda item: item.date)

    @_sync
    def latest_bar_date(self, symbol: str, fq: str = "qfq", period: str = DEFAULT_PERIOD) -> Optional[str]:
        row = self._conn.execute(
            "SELECT MAX(trade_date) AS latest FROM bars WHERE symbol = ? AND fq = ? AND period = ?",
            (symbol, fq, period),
        ).fetchone()
        return row["latest"] if row and row["latest"] else None

    @_sync
    def save_signals(self, signals: Iterable[Signal], period: Optional[str] = None) -> int:
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
                period or signal.period or DEFAULT_PERIOD,
            )
            for signal in signals
        ]
        if not payload:
            return 0
        # 冲突键必须与 Store.SIGNAL_IDENTITY_COLUMNS 一致：包含 bar_index 会让
        # 同一信号因 K 线窗口不同被当成新信号插入（实测污染了 15416 条）。
        with self._lock:
            self._conn.executemany(
                "INSERT INTO signals (symbol, kind, trade_date, bar_index, price, anchor_index, anchor_price, "
                "message, meta_json, created_at, period) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (symbol, kind, period, trade_date, price, anchor_price) DO UPDATE SET "
                "bar_index = excluded.bar_index, anchor_index = excluded.anchor_index, "
                "meta_json = excluded.meta_json",
                payload,
            )
            self._conn.commit()
        return len(payload)

    @_sync
    def recent_signals(
        self, kind: Optional[SignalKind] = None, limit: int = 200, period: Optional[str] = None
    ) -> List[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if period is not None:
            clauses.append("period = ?")
            params.append(period)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM signals {where}ORDER BY trade_date DESC, id DESC LIMIT ?", tuple(params)
        ).fetchall()
        return [self._signal_row_to_dict(row) for row in rows]

    @_sync
    def recent_trading_dates(self, limit: int = 3, period: str = DEFAULT_PERIOD) -> list[str]:
        """最近 N 个交易日（降序），取自本地日线缓存的日期集合。

        注意：只取日线。分钟周期的 trade_date 带时间戳，直接 DISTINCT 会退化成
        「最近 N 根分钟 K」，失去交易日的语义。
        """

        rows = self._conn.execute(
            "SELECT DISTINCT trade_date FROM bars WHERE period = ? ORDER BY trade_date DESC LIMIT ?",
            (period, limit),
        ).fetchall()
        return [row["trade_date"] for row in rows]

    @_sync
    def _signals_since(self, kinds: Iterable[SignalKind], cutoff: str, period: Optional[str] = None) -> list[dict]:
        kind_values = [kind.value for kind in kinds]
        if not kind_values:
            return []
        placeholders = ",".join("?" for _ in kind_values)
        params: list[Any] = [*kind_values, cutoff]
        extra = ""
        if period is not None:
            extra = "AND period = ? "
            params.append(period)
        rows = self._conn.execute(
            f"SELECT * FROM signals WHERE kind IN ({placeholders}) AND trade_date >= ? {extra}"
            "ORDER BY trade_date DESC, id DESC",
            tuple(params),
        ).fetchall()
        return [self._signal_row_to_dict(row) for row in rows]

    @_sync
    def buy_signals_since(self, cutoff: str, period: Optional[str] = DEFAULT_PERIOD) -> list[dict]:
        """返回 >= cutoff 日期的买点类信号（一买/二买/三买），按日期降序。"""

        return self._signals_since(BUY_KINDS, cutoff, period)

    @_sync
    def sell_signals_since(self, cutoff: str, period: Optional[str] = DEFAULT_PERIOD) -> list[dict]:
        """返回 >= cutoff 日期的卖点类信号（一卖/二卖），按日期降序。"""

        return self._signals_since(SELL_KINDS, cutoff, period)

    @_sync
    def signals_for_symbol(
        self, symbol: str, limit: int = 50, period: Optional[str] = None
    ) -> list[dict]:
        if period is None:
            rows = self._conn.execute(
                "SELECT * FROM signals WHERE symbol = ? ORDER BY bar_index DESC, id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM signals WHERE symbol = ? AND period = ? ORDER BY bar_index DESC, id DESC LIMIT ?",
                (symbol, period, limit),
            ).fetchall()
        return [self._signal_row_to_dict(row) for row in rows]

    @_sync
    def period_counts(self) -> dict:
        """各周期的本地缓存概况，供 /api/health 自检。"""

        counts: dict = {"bars": {}, "signals": {}}
        for row in self._conn.execute("SELECT period, COUNT(*) AS n FROM bars GROUP BY period"):
            counts["bars"][row["period"]] = row["n"]
        for row in self._conn.execute("SELECT period, COUNT(*) AS n FROM signals GROUP BY period"):
            counts["signals"][row["period"]] = row["n"]
        return counts

    @staticmethod
    def _signal_row_to_dict(row: sqlite3.Row) -> dict:
        meta = json.loads(row["meta_json"] or "{}")
        score = signal_score_of(meta)
        return {
            "symbol": row["symbol"],
            "name": "",
            "kind": row["kind"],
            "date": row["trade_date"],
            "bar_index": row["bar_index"],
            "price": row["price"],
            "anchor_index": row["anchor_index"],
            "anchor_price": row["anchor_price"],
            "period": row["period"],
            "score": float(score or 0),
            "message": row["message"],
            "meta": meta,
        }

    @_sync
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

    @_sync
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

    @_sync
    def create_run(self, run_id: str, scope: str, total: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO scan_runs (run_id, scope, status, total, processed, hits, message, started_at) "
                "VALUES (?, ?, 'running', ?, 0, 0, '', ?)",
                (run_id, scope, total, time.time()),
            )
            self._conn.commit()

    @_sync
    def update_run(self, run_id: str, processed: int, hits: int, status: str = "running", message: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE scan_runs SET processed = ?, hits = ?, status = ?, message = ? WHERE run_id = ?",
                (processed, hits, status, message, run_id),
            )
            self._conn.commit()

    @_sync
    def finish_run(self, run_id: str, status: str, message: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE scan_runs SET status = ?, message = ?, finished_at = ? WHERE run_id = ?",
                (status, message, time.time(), run_id),
            )
            self._conn.commit()

    @_sync
    def get_run(self, run_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM scan_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    @_sync
    def mark_interrupted_runs(self) -> int:
        """把残留的 running 行判为 interrupted，返回修正的行数。

        扫描跑在后台线程里，进程被 kill（手动停服务、OOM、部署重启）时
        `finish_run` 来不及执行，scan_runs 里会留下永远 running 的行，
        前端因此一直轮询一个再也不会结束的任务。启动时统一修一次。
        """

        with self._lock:
            cursor = self._conn.execute(
                "UPDATE scan_runs SET status = 'interrupted', "
                "message = CASE WHEN message = '' THEN '服务重启前任务未正常结束' ELSE message END "
                "WHERE status = 'running'"
            )
            self._conn.commit()
            return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    @_sync
    def latest_run(self) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    @_sync
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

    @_sync
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

    @_sync
    def clear_run_hits(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM scan_hits WHERE run_id = ?", (run_id,))
            self._conn.commit()

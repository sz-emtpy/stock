"""SQLite persistence for ChanPicker backend.

Tables:
- symbols: cached symbol -> name mapping
- bars: daily adjusted OHLCV cache
- signals: historical first/second buy signals
- watchlist: focus pool state machine
- stock_sectors: 个股 -> 所属板块（反查结果缓存，避免重复问数据源）
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
    # `WHERE symbol IN (...)` 的分批大小。SQLite 变量上限默认 999，取 500 留余量。
    BATCH_SIZE = 500

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
                CREATE TABLE IF NOT EXISTS stock_sectors (
                    symbol TEXT NOT NULL,
                    sector_code TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    level TEXT NOT NULL,
                    from_theme TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (symbol, sector_code)
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
                    PRIMARY KEY (run_id, symbol, kind, trade_date)
                );
                -- 外部取数时间戳：同一 (kind, key) 在冷却期内不再联网重抓。
                -- 详情页慢的根因之一就是「每次进来都去抓」——板块反查一次要 5~6 秒。
                CREATE TABLE IF NOT EXISTS fetch_stamps (
                    kind TEXT NOT NULL,
                    key TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (kind, key)
                );
                -- 自选 / 持仓：用户手动维护的少量标的。
                -- 卖点本来是「持仓风险提示」，但 risk_pool 是全市场的，
                -- 有了这张表才能回答「我手上这几只今天有没有卖点」。
                CREATE TABLE IF NOT EXISTS favorites (
                    symbol TEXT PRIMARY KEY,
                    note TEXT NOT NULL DEFAULT '',
                    cost_price REAL,
                    created_at REAL NOT NULL
                );
                -- 买点结局 + 特征样本库（自成长体系的地基）。
                -- 一行 = 一个历史买点信号 + 它后续走势的结局 + 信号时刻可得的特征。
                -- 主键与 SIGNAL_IDENTITY_COLUMNS 对齐（幂等 upsert），
                -- 「赚钱」用三重结局量化：max_return 最大涨幅 / hold_return 期末收益
                -- / max_drawdown 期间最大回撤（均相对信号价，非二元）。
                -- 2026-10-18 体系体检后新增**规则化出场**字段（迁移见 _migrate_outcomes_exit_columns）：
                --   exit_return   规则化出场收益（相对信号价，未扣成本）——出场规则见 _compute_outcome
                --   exit_date     实际出场日（卖点日或强平日）
                --   exit_days     信号日到出场日的交易日数
                --   exit_kind     FIRST_SELL / SECOND_SELL（卖点出场）或 FLAT_CLOSE（窗口末强平）
                --   net_exit_return  扣往返交易成本后的到手收益——**体系训练标签**
                --   alpha_exit    net_exit_return − 同期上证涨跌（跑赢大盘才算数）
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    symbol TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    period TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    price REAL NOT NULL,
                    anchor_price REAL NOT NULL,
                    max_return REAL,
                    hold_return REAL,
                    max_drawdown REAL,
                    days_to_peak INTEGER,
                    window_size INTEGER NOT NULL DEFAULT 0,
                    exit_return REAL,
                    exit_date TEXT,
                    exit_days INTEGER,
                    exit_kind TEXT,
                    net_exit_return REAL,
                    alpha_exit REAL,
                    features_json TEXT NOT NULL DEFAULT '{}',
                    computed_at REAL NOT NULL,
                    PRIMARY KEY (symbol, kind, period, signal_date, price, anchor_price)
                );
                CREATE INDEX IF NOT EXISTS idx_outcomes_kind ON signal_outcomes (kind, period);
                -- 市场情绪日线：每个交易日的宽度 / 涨停数 / 平均涨跌幅 / 指数涨跌幅。
                -- 从全市场 bars 用窗口函数算（「情绪市场」特征的底座），幂等重算。
                CREATE TABLE IF NOT EXISTS market_breadth (
                    trade_date TEXT PRIMARY KEY,
                    up_count INTEGER NOT NULL DEFAULT 0,
                    down_count INTEGER NOT NULL DEFAULT 0,
                    flat_count INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    limit_up INTEGER NOT NULL DEFAULT 0,
                    avg_change REAL,
                    index_change REAL
                );
                -- 体系快照：每次增益分析存一份，追踪「体系如何随数据成长」。
                -- gain_json / cards_json / validate_json 分别是单特征增益、
                -- 结构-买点-胜率卡、时间分割验证结果的快照。
                CREATE TABLE IF NOT EXISTS system_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    samples INTEGER NOT NULL,
                    threshold REAL NOT NULL,
                    gain_json TEXT NOT NULL DEFAULT '{}',
                    cards_json TEXT NOT NULL DEFAULT '[]',
                    validate_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_bars_symbol ON bars (symbol, trade_date);
                CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol, trade_date);
                CREATE INDEX IF NOT EXISTS idx_hits_run ON scan_hits (run_id);
                """
            )
            # 必须在迁移之后才建 period 索引：老表尚无该列。
            self._migrate_legacy()
            self._migrate_signal_identity()
            self._migrate_scan_hits_identity()
            self._migrate_outcomes_exit_columns()
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

    # ------------------------------------------------------------------
    # 扫描命中：唯一键必须带 trade_date，否则每股票每类只剩一条
    # ------------------------------------------------------------------

    SCAN_HIT_IDENTITY_COLUMNS = ("run_id", "symbol", "kind", "trade_date")

    @_sync
    def _migrate_scan_hits_identity(self) -> int:
        """把 scan_hits 的主键从 `(run_id, symbol, kind)` 扩到含 `trade_date`。

        为什么必须改：一次全市场扫描里，同一只股票的 400 根 K 线会产出**多个**
        同类买点（例如 2025-11 和 2026-03 各一个一买）。旧主键让后者覆盖前者，
        整张表只剩「每股票每类最新一条」，扫描结果被系统性吃掉约 10 倍
        （实测同一 20 交易日窗口：signals 一买 1404 条 vs scan_hits 145 条）。

        幂等：已升级的库直接返回 0。迁移保留全部已有行（去重键 = 新主键）。
        """

        wanted = tuple(self.SCAN_HIT_IDENTITY_COLUMNS)
        if wanted in self._unique_index_columns(self._conn, "scan_hits"):
            return 0

        before = self._conn.execute("SELECT COUNT(*) AS n FROM scan_hits").fetchone()["n"]
        self._conn.execute("BEGIN")
        try:
            statements = [
                "DROP INDEX IF EXISTS idx_hits_run",
                "ALTER TABLE scan_hits RENAME TO scan_hits_legacy",
                """
                CREATE TABLE scan_hits (
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    price REAL NOT NULL,
                    score REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (run_id, symbol, kind, trade_date)
                )
                """,
                # 老库里同一 (run_id, symbol, kind) 已被压成一行，这里用新主键去重
                # 只是防御历史脏数据（例如手工导入过重复行），正常情况全量保留。
                "INSERT INTO scan_hits (run_id, symbol, name, kind, trade_date, price, score, message) "
                "SELECT run_id, symbol, name, kind, trade_date, price, score, message FROM scan_hits_legacy "
                "WHERE rowid IN (SELECT MIN(rowid) FROM scan_hits_legacy "
                "GROUP BY run_id, symbol, kind, trade_date)",
                "DROP TABLE scan_hits_legacy",
                "CREATE INDEX IF NOT EXISTS idx_hits_run ON scan_hits (run_id)",
            ]
            for statement in statements:
                self._conn.execute(statement)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        after = self._conn.execute("SELECT COUNT(*) AS n FROM scan_hits").fetchone()["n"]
        self._conn.commit()
        return after - before

    # ------------------------------------------------------------------
    # 结局表规则化出场字段：体系 v2 的训练标签地基
    # ------------------------------------------------------------------

    # 体检报告（2026-10-18）的结论：旧标签 max_return 回答「结构冲没冲高」，
    # 不是「拿到手的钱」。v2 的标签是 net_exit_return（规则化出场 + 扣成本），
    # 这批列就是它的落点。老库缺列时在这里补齐（SQLite 加列是元数据操作，秒级）。
    OUTCOME_EXIT_COLUMNS: tuple[tuple[str, str], ...] = (
        ("exit_return", "REAL"),
        ("exit_date", "TEXT"),
        ("exit_days", "INTEGER"),
        ("exit_kind", "TEXT"),
        ("net_exit_return", "REAL"),
        ("alpha_exit", "REAL"),
    )

    @_sync
    def _migrate_outcomes_exit_columns(self) -> int:
        """给 signal_outcomes 补规则化出场列（幂等：已有列直接跳过）。"""

        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(signal_outcomes)")}
        added = 0
        for name, decl in self.OUTCOME_EXIT_COLUMNS:
            if name in existing:
                continue
            self._conn.execute(f"ALTER TABLE signal_outcomes ADD COLUMN {name} {decl}")
            added += 1
        if added:
            self._conn.commit()
        return added

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
    def period_earliest_date(self, period: str, fq: Optional[str] = None) -> Optional[str]:
        """某周期在本地库里的最早日期（m30 数据窗口起点等覆盖性判断用）。"""

        if fq is None:
            row = self._conn.execute(
                "SELECT MIN(trade_date) AS earliest FROM bars WHERE period = ?",
                (period,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT MIN(trade_date) AS earliest FROM bars WHERE period = ? AND fq = ?",
                (period, fq),
            ).fetchone()
        return row["earliest"] if row and row["earliest"] else None

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
    def delete_signals(
        self, symbol: str, kind: str, trade_date: str, period: str = DEFAULT_PERIOD
    ) -> int:
        """按 (symbol, kind, trade_date, period) 删除信号，返回删除行数。

        供 analyze 时清除「过时信号」用（盘中扫描写入、收盘后不复现的残留）。
        """

        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM signals WHERE symbol = ? AND kind = ? AND trade_date = ? AND period = ?",
                (symbol, kind, trade_date, period),
            )
            self._conn.commit()
            return int(cursor.rowcount or 0)

    @_sync
    def delete_outcomes(
        self, symbol: str, kind: str, signal_date: str, period: str = DEFAULT_PERIOD
    ) -> int:
        """删除 signal_outcomes 里对应的结局样本（与 delete_signals 配套，保持两表一致）。"""

        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM signal_outcomes WHERE symbol = ? AND kind = ? AND signal_date = ? AND period = ?",
                (symbol, kind, signal_date, period),
            )
            self._conn.commit()
            return int(cursor.rowcount or 0)

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
    def save_stock_sectors(self, symbol: str, sectors: Iterable[dict]) -> int:
        """落地个股所属板块。反查要 1 次产业链 + N 次板块搜索，必须缓存。"""

        payload = [
            (
                symbol,
                str(item.get("code", "")).strip(),
                str(item.get("name", "")).strip(),
                str(item.get("level", "concept")).strip(),
                str(item.get("from_theme", "")).strip(),
                time.time(),
            )
            for item in sectors
            if str(item.get("code", "")).strip()
        ]
        if not payload:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO stock_sectors (symbol, sector_code, sector_name, level, from_theme, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, sector_code) DO UPDATE SET "
                "sector_name = excluded.sector_name, level = excluded.level, "
                "from_theme = excluded.from_theme, updated_at = excluded.updated_at",
                payload,
            )
            self._conn.commit()
        return len(payload)

    @_sync
    def load_stock_sectors(self, symbol: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT sector_code, sector_name, level, from_theme, updated_at FROM stock_sectors "
            "WHERE symbol = ? ORDER BY updated_at",
            (symbol,),
        ).fetchall()
        return [
            {
                "code": row["sector_code"],
                "name": row["sector_name"],
                "level": row["level"],
                "from_theme": row["from_theme"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 外部取数冷却戳：详情页「本地优先、没有才抓、抓一次就记住」
    #
    # 为什么需要：日线的「最新一根是不是今天」判断在周末/节假日永远不成立，
    # 会导致每次进详情都联网；板块反查更贵（一次 5~6 秒）。有了戳就能保证
    # 同一 (kind, key) 在冷却期内只抓一次，失败也记戳（避免反复重试拖死页面）。
    # ------------------------------------------------------------------

    @_sync
    def fetch_stamp(self, kind: str, key: str) -> Optional[float]:
        """返回上次抓取时间（epoch 秒）；从未抓过返回 None。"""

        row = self._conn.execute(
            "SELECT fetched_at FROM fetch_stamps WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        return float(row["fetched_at"]) if row else None

    @_sync
    def mark_fetch(self, kind: str, key: str, at: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO fetch_stamps (kind, key, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(kind, key) DO UPDATE SET fetched_at = excluded.fetched_at",
                (kind, key, time.time() if at is None else at),
            )
            self._conn.commit()

    @_sync
    def fetched_within(self, kind: str, key: str, seconds: float) -> bool:
        """冷却判断：`seconds` 秒内抓过就返回 True（调用方应直接用本地缓存）。"""

        stamp = self.fetch_stamp(kind, key)
        return stamp is not None and (time.time() - stamp) < seconds

    @_sync
    def signals_for_symbols(
        self, symbols: Iterable[str], limit: int = 1000, period: Optional[str] = None
    ) -> Dict[str, list[dict]]:
        """批量取多只票的信号，返回 {symbol: [信号字典]}。

        为什么必须批量：逐只调 `signals_for_symbol` 实测 241 只要 6206 ms，
        批量 `symbol IN (...)` 只要 62 ms。买卖点池要在一次请求里给每行算胜率，
        逐只查询会让 /api/market/pools 慢到不可用。

        `limit` 是**每只票**的上限（与 signals_for_symbol 语义一致），
        查询侧已按 trade_date 排序，这里只保留每只票最近 limit 条。
        """

        unique = sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()})
        if not unique:
            return {}
        result: Dict[str, list[dict]] = {}
        for offset in range(0, len(unique), self.BATCH_SIZE):
            batch = unique[offset : offset + self.BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            params: list = list(batch)
            extra = ""
            if period is not None:
                extra = "AND period = ? "
                params.append(period)
            rows = self._conn.execute(
                f"SELECT * FROM signals WHERE symbol IN ({placeholders}) {extra}"
                "ORDER BY symbol, trade_date DESC, id DESC",
                tuple(params),
            ).fetchall()
            for row in rows:
                bucket = result.setdefault(row["symbol"], [])
                if len(bucket) < limit:
                    bucket.append(self._signal_row_to_dict(row))
        return result

    @_sync
    def load_close_series(
        self, symbols: Iterable[str], period: Optional[str] = DEFAULT_PERIOD, fq: str = "qfq"
    ) -> Dict[str, list[tuple]]:
        """批量取收盘价序列，返回 {symbol: [(trade_date, close)]}（按日期升序）。

        判定二买成败只需要收盘价（是否跌破二买低点），取全量 OHLC 是浪费：
        241 只 × 400 根只需传输 1 个字段，实测 100 ms。
        """

        unique = sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()})
        if not unique:
            return {}
        result: Dict[str, list[tuple]] = {}
        for offset in range(0, len(unique), self.BATCH_SIZE):
            batch = unique[offset : offset + self.BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            params: list = [fq, *batch]
            extra = ""
            if period is not None:
                extra = "AND period = ? "
                params.append(period)
            rows = self._conn.execute(
                f"SELECT symbol, trade_date, close FROM bars WHERE fq = ? AND symbol IN ({placeholders}) "
                f"{extra}ORDER BY symbol, trade_date",
                tuple(params),
            ).fetchall()
            for row in rows:
                result.setdefault(row["symbol"], []).append((row["trade_date"], row["close"]))
        return result

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
    def list_runs(self, limit: int = 20) -> list[dict]:
        """扫描历史（最近的在前）。前端「扫描历史」与命中变化趋势都用它。"""

        rows = self._conn.execute(
            "SELECT run_id, scope, status, total, processed, hits, message, started_at, finished_at "
            "FROM scan_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_sync
    def hit_counts(self, run_id: str) -> dict:
        """某次扫描按信号类型的命中数（复盘总结用）。"""

        rows = self._conn.execute(
            "SELECT kind, COUNT(*) AS n FROM scan_hits WHERE run_id = ? GROUP BY kind", (run_id,)
        ).fetchall()
        return {row["kind"]: row["n"] for row in rows}

    @_sync
    def last_bar_dates(self, period: str = DEFAULT_PERIOD) -> Dict[str, str]:
        """每只标的的本地最后一根 K 线日期，返回 {symbol: trade_date}。

        增量扫描靠它判断「谁落后了」。一次 GROUP BY 走 `(period, trade_date)` 索引，
        实测全市场 5000 只约几十毫秒——比逐只 load_bars 快三个数量级。
        """

        rows = self._conn.execute(
            "SELECT symbol, MAX(trade_date) AS last FROM bars WHERE period = ? GROUP BY symbol",
            (period,),
        ).fetchall()
        return {row["symbol"]: row["last"] for row in rows if row["last"]}

    # ------------------------------------------------------------------
    # 自选 / 持仓
    # ------------------------------------------------------------------

    @_sync
    def list_favorites(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT symbol, note, cost_price, created_at FROM favorites ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]

    @_sync
    def upsert_favorite(
        self, symbol: str, note: str = "", cost_price: Optional[float] = None
    ) -> None:
        """加入自选；已存在则更新备注与成本价（成本价传 None 表示清空）。"""

        with self._lock:
            self._conn.execute(
                "INSERT INTO favorites (symbol, note, cost_price, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET note = excluded.note, cost_price = excluded.cost_price",
                (symbol, note, cost_price, time.time()),
            )
            self._conn.commit()

    @_sync
    def delete_favorite(self, symbol: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM favorites WHERE symbol = ?", (symbol,))
            self._conn.commit()
            return bool(cursor.rowcount)

    @_sync
    def favorite_symbols(self) -> list[str]:
        rows = self._conn.execute("SELECT symbol FROM favorites ORDER BY created_at").fetchall()
        return [row["symbol"] for row in rows]

    # ------------------------------------------------------------------
    # 买点结局样本库（自成长体系地基）
    # ------------------------------------------------------------------

    @_sync
    def all_signals_by_kinds(
        self, kinds: Iterable[SignalKind], period: str = DEFAULT_PERIOD
    ) -> list[dict]:
        """取某类信号的全量（不设 cutoff），按 symbol/date 排序。

        rebuild_outcomes 用它一次性拿全部日线买点，避免逐只查询的 N+1。
        """

        kind_values = [k.value for k in kinds]
        if not kind_values:
            return []
        placeholders = ",".join("?" for _ in kind_values)
        rows = self._conn.execute(
            f"SELECT * FROM signals WHERE kind IN ({placeholders}) AND period = ? "
            "ORDER BY symbol, trade_date, id",
            (*kind_values, period),
        ).fetchall()
        return [self._signal_row_to_dict(row) for row in rows]

    @_sync
    def save_outcomes(self, outcomes: Iterable[dict]) -> int:
        payload = [
            (
                o["symbol"],
                o["kind"],
                o["period"],
                o["signal_date"],
                o["price"],
                o["anchor_price"],
                o.get("max_return"),
                o.get("hold_return"),
                o.get("max_drawdown"),
                o.get("days_to_peak"),
                o.get("window_size", 0),
                o.get("exit_return"),
                o.get("exit_date"),
                o.get("exit_days"),
                o.get("exit_kind"),
                o.get("net_exit_return"),
                o.get("alpha_exit"),
                json.dumps(o.get("features", {}), ensure_ascii=False),
                time.time(),
            )
            for o in outcomes
        ]
        if not payload:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO signal_outcomes "
                "(symbol, kind, period, signal_date, price, anchor_price, "
                " max_return, hold_return, max_drawdown, days_to_peak, window_size, "
                " exit_return, exit_date, exit_days, exit_kind, net_exit_return, alpha_exit, "
                " features_json, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, kind, period, signal_date, price, anchor_price) DO UPDATE SET "
                "max_return = excluded.max_return, hold_return = excluded.hold_return, "
                "max_drawdown = excluded.max_drawdown, days_to_peak = excluded.days_to_peak, "
                "window_size = excluded.window_size, "
                "exit_return = excluded.exit_return, exit_date = excluded.exit_date, "
                "exit_days = excluded.exit_days, exit_kind = excluded.exit_kind, "
                "net_exit_return = excluded.net_exit_return, alpha_exit = excluded.alpha_exit, "
                "features_json = excluded.features_json, computed_at = excluded.computed_at",
                payload,
            )
            self._conn.commit()
        return len(payload)

    @_sync
    def load_outcomes(
        self, kind: Optional[str] = None, period: str = DEFAULT_PERIOD, limit: int = 1000
    ) -> list[dict]:
        clauses = ["period = ?"]
        params: list[Any] = [period]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        params.append(limit)
        rows = self._conn.execute(
            "SELECT * FROM signal_outcomes WHERE " + " AND ".join(clauses)
            + " ORDER BY signal_date DESC, symbol LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._outcome_row_to_dict(row) for row in rows]

    @_sync
    def outcome_stats(self, period: str = DEFAULT_PERIOD) -> dict:
        """按信号类型聚合的结局统计（回答「哪类买点整体更赚」）。"""

        rows = self._conn.execute(
            "SELECT kind, COUNT(*) AS n, "
            "AVG(max_return) AS avg_max, AVG(hold_return) AS avg_hold, "
            "AVG(max_drawdown) AS avg_dd, AVG(window_size) AS avg_win "
            "FROM signal_outcomes WHERE period = ? GROUP BY kind",
            (period,),
        ).fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            kind = row["kind"]
            n = row["n"]
            win = self._conn.execute(
                "SELECT COUNT(*) AS c FROM signal_outcomes "
                "WHERE kind = ? AND period = ? AND max_return IS NOT NULL AND max_return > 0",
                (kind, period),
            ).fetchone()["c"]
            closed = self._conn.execute(
                "SELECT COUNT(*) AS c FROM signal_outcomes "
                "WHERE kind = ? AND period = ? AND max_return IS NOT NULL",
                (kind, period),
            ).fetchone()["c"]
            out[kind] = {
                "count": n,
                "closed": closed,
                "win": win,
                "win_rate": round(win / closed, 4) if closed else None,
                "avg_max_return": round(row["avg_max"], 4) if row["avg_max"] is not None else None,
                "avg_hold_return": round(row["avg_hold"], 4) if row["avg_hold"] is not None else None,
                "avg_max_drawdown": round(row["avg_dd"], 4) if row["avg_dd"] is not None else None,
                "avg_window": round(row["avg_win"], 1) if row["avg_win"] is not None else None,
            }
        return out

    @_sync
    def outcome_count(self, period: str = DEFAULT_PERIOD) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM signal_outcomes WHERE period = ?", (period,)
        ).fetchone()
        return int(row["n"]) if row else 0

    @_sync
    def load_all_outcomes(self, period: str = DEFAULT_PERIOD) -> list[dict]:
        """全量取结局样本（增益分析用，不分页）。数据量几万条，SQLite 秒级。"""

        rows = self._conn.execute(
            "SELECT * FROM signal_outcomes WHERE period = ? ORDER BY signal_date, symbol",
            (period,),
        ).fetchall()
        return [self._outcome_row_to_dict(row) for row in rows]

    @_sync
    def recent_outcomes(self, cutoff: str, period: str = DEFAULT_PERIOD) -> list[dict]:
        """取 signal_date >= cutoff 的买点样本（体系选股池用）。"""

        rows = self._conn.execute(
            "SELECT * FROM signal_outcomes WHERE period = ? AND signal_date >= ? "
            "ORDER BY signal_date DESC, symbol",
            (period, cutoff),
        ).fetchall()
        return [self._outcome_row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 市场情绪（情绪市场特征的底座）
    # ------------------------------------------------------------------

    @_sync
    def compute_market_breadth(self) -> dict:
        """用 SQL 窗口函数一次性算全市场每个交易日的宽度/涨停/平均涨跌幅 + 指数涨跌幅。

        数据源是本地 bars（218 万行），不联网。窗口函数 LAG 在 SQLite 3.25+ 可用。
        涨停阈值按代码区分：创业板(sz30)/科创板(sh68) 用 20%，其余用 10%（近似 9.5%）。
        """

        rows = self._conn.execute(
            """
            WITH chg AS (
              SELECT symbol, trade_date,
                     (close - LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date))
                     / NULLIF(LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date), 0) AS pct
              FROM bars WHERE period = 'day' AND fq = 'qfq'
            )
            SELECT trade_date,
                   COUNT(pct) AS total,
                   SUM(CASE WHEN pct > 0 THEN 1 ELSE 0 END) AS up_count,
                   SUM(CASE WHEN pct < 0 THEN 1 ELSE 0 END) AS down_count,
                   SUM(CASE WHEN pct = 0 THEN 1 ELSE 0 END) AS flat_count,
                   SUM(CASE WHEN pct >= CASE WHEN symbol LIKE 'sz30%' OR symbol LIKE 'sh68%'
                                            THEN 0.195 ELSE 0.095 END THEN 1 ELSE 0 END) AS limit_up,
                   AVG(pct) AS avg_change
            FROM chg WHERE pct IS NOT NULL
            GROUP BY trade_date ORDER BY trade_date
            """
        ).fetchall()

        idx_rows = self._conn.execute(
            """
            WITH ic AS (
              SELECT trade_date,
                     (close - LAG(close) OVER (ORDER BY trade_date))
                     / NULLIF(LAG(close) OVER (ORDER BY trade_date), 0) AS pct
              FROM bars WHERE symbol = 'sh000001' AND fq = 'ref' AND period = 'day'
            )
            SELECT trade_date, pct FROM ic WHERE pct IS NOT NULL
            """
        ).fetchall()
        idx_map = {r["trade_date"]: r["pct"] for r in idx_rows}

        payload = [
            (
                r["trade_date"],
                r["up_count"] or 0,
                r["down_count"] or 0,
                r["flat_count"] or 0,
                r["total"] or 0,
                r["limit_up"] or 0,
                round(r["avg_change"], 4) if r["avg_change"] is not None else None,
                round(idx_map[r["trade_date"]], 4) if r["trade_date"] in idx_map else None,
            )
            for r in rows
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO market_breadth "
                "(trade_date, up_count, down_count, flat_count, total, limit_up, avg_change, index_change) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(trade_date) DO UPDATE SET "
                "up_count = excluded.up_count, down_count = excluded.down_count, "
                "flat_count = excluded.flat_count, total = excluded.total, "
                "limit_up = excluded.limit_up, avg_change = excluded.avg_change, "
                "index_change = excluded.index_change",
                payload,
            )
            self._conn.commit()
        return {"days": len(payload), "message": f"已计算 {len(payload)} 个交易日的市场情绪"}

    @_sync
    def load_market_breadth(self, trade_date: str) -> Optional[dict]:
        """取某交易日的市场情绪指标；没有返回 None。"""

        row = self._conn.execute(
            "SELECT * FROM market_breadth WHERE trade_date = ?", (trade_date,)
        ).fetchone()
        return dict(row) if row else None

    @_sync
    def market_breadth_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM market_breadth").fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # 体系快照
    # ------------------------------------------------------------------

    @_sync
    def save_snapshot(
        self, samples: int, threshold: float, gain: dict, cards: list, validate: dict
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO system_snapshots "
                "(created_at, samples, threshold, gain_json, cards_json, validate_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    samples,
                    threshold,
                    json.dumps(gain, ensure_ascii=False),
                    json.dumps(cards, ensure_ascii=False),
                    json.dumps(validate, ensure_ascii=False),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    @_sync
    def latest_snapshot(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM system_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return self._snapshot_row(row) if row else None

    @_sync
    def list_snapshots(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM system_snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._snapshot_row(row) for row in rows]

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "samples": row["samples"],
            "threshold": row["threshold"],
            "gain": json.loads(row["gain_json"] or "{}"),
            "cards": json.loads(row["cards_json"] or "[]"),
            "validate": json.loads(row["validate_json"] or "{}"),
        }

    @staticmethod
    def _outcome_row_to_dict(row: sqlite3.Row) -> dict:
        keys = row.keys()
        d = {
            "symbol": row["symbol"],
            "kind": row["kind"],
            "period": row["period"],
            "signal_date": row["signal_date"],
            "price": row["price"],
            "anchor_price": row["anchor_price"],
            "max_return": row["max_return"],
            "hold_return": row["hold_return"],
            "max_drawdown": row["max_drawdown"],
            "days_to_peak": row["days_to_peak"],
            "window_size": row["window_size"],
            "features": json.loads(row["features_json"] or "{}"),
        }
        # 出场列是 2026-10-18 后加的，测试里手工建的老表可能没有——按有无取值
        for key in ("exit_return", "exit_date", "exit_days", "exit_kind", "net_exit_return", "alpha_exit"):
            d[key] = row[key] if key in keys else None
        return d

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
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (run_id, symbol, kind, trade_date) DO UPDATE SET "
                "name = excluded.name, price = excluded.price, score = excluded.score",
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

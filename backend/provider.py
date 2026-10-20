"""Market data provider backed by the westock CLI.

The provider never fabricates quotes: if the CLI is unavailable or not
authorized, it raises MarketDataError so callers can surface a real status.

多源互补（2026-10-20 起）：
`fetch_bars` 优先走 `backend.sources`（baostock 长历史 + 东财当天/盘中，合成一条序列），
失败才回落到原来的 westock CLI。`fetch_bars_batch` 保持走 westock 批量 ——
全市场 5000 只逐只走 SDK 会把扫描从几十分钟拖成几小时，批量速度必须留给 CLI。

回滚开关：`CHANPICKER_SOURCE=westock` 可一键退回纯 westock（出问题时不改代码就能恢复）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from chanpicker.core import Bar

log = logging.getLogger(__name__)

# 最近一次多源取数的报告（诊断用，/api/health 会带出来）
LAST_SOURCE_REPORT: Dict[str, dict] = {}

ENV_SOURCE_MODE = "CHANPICKER_SOURCE"


def multi_source_enabled() -> bool:
    """默认开启多源；设为 westock 即整段回退（出问题时不改代码就能恢复）。

    未显式设置时，**测试进程默认关闭**：单测里只要漏掉一处 mock，
    多源路径就会真的去连 baostock/东财，让整个套件变慢、变脆、依赖外网。
    显式设置 CHANPICKER_SOURCE 时一律以显式值为准，不猜。
    """
    raw = os.getenv(ENV_SOURCE_MODE)
    if raw is not None:
        return raw.strip().lower() != "westock"
    if "unittest" in sys.modules:
        return False
    return True


WESTOCK_BIN_CANDIDATES = (
    "westock",
    str(Path.home() / ".local" / "bin" / "westock"),
    "/usr/local/bin/westock",
)


class MarketDataError(RuntimeError):
    """Raised when market data cannot be fetched or parsed."""


# 周期能力表（数据源实测结论，勿凭直觉修改）：
# - 分钟周期必须用 --start/--end 取历史，--limit 对分钟无效（实测 m30 --limit 500 只返回当日 8 根）
# - 官方跨度上限：m15=20 天、m30=30 天；分钟数据仅保留近 1 个月
# - 实测可得：m30 ≈ 72 根（9 个交易日），m15 ≈ 144 根（9 个交易日），日线 400 根
PERIODS: Dict[str, Dict[str, object]] = {
    "day": {"span_days": None, "max_bars": 400, "intraday": False},
    "m30": {"span_days": 30, "max_bars": 200, "intraday": True},
    "m15": {"span_days": 20, "max_bars": 400, "intraday": True},
}

DEFAULT_PERIOD = "day"

# 可对用户开放的周期（顺序即前端切换顺序）
LAUNCH_PERIODS = ("day", "m30", "m15")


def normalize_period(period: Optional[str]) -> str:
    key = (period or DEFAULT_PERIOD).strip().lower()
    if key not in PERIODS:
        raise ValueError(f"不支持的周期: {period}（可选: {', '.join(sorted(PERIODS))}）")
    return key


def is_intraday(period: str) -> bool:
    return bool(PERIODS[normalize_period(period)]["intraday"])


def period_max_bars(period: str) -> int:
    return int(PERIODS[normalize_period(period)]["max_bars"])  # type: ignore[arg-type]


def resolve_westock_bin() -> str:
    for candidate in WESTOCK_BIN_CANDIDATES:
        found = shutil.which(candidate) if "/" not in candidate else candidate
        if found and Path(found).exists():
            return candidate if "/" not in candidate else found
    raise MarketDataError("westock CLI 未安装，请先执行 westockdata 技能包内的 scripts/setup.sh")


def run_westock(args: list[str], timeout: int = 30) -> str:
    binary = resolve_westock_bin()
    try:
        completed = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MarketDataError(f"westock 调用超时: {' '.join(args)}") from exc
    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or not output:
        raise MarketDataError(f"westock 调用失败: {' '.join(args)} -> {(completed.stderr or output)[:200]}")
    if "AUTH_REQUIRED" in output:
        raise MarketDataError("westock 未授权，请先在浏览器完成授权后再拉取行情")
    return output


def parse_markdown_table(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    header: Optional[List[str]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or all(not cell for cell in cells):
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


_COLUMN_ALIASES = {
    "symbol": ("代码", "证券代码", "symbol", "code"),
    "name": ("名称", "股票名称", "简称", "name"),
    "date": ("日期", "交易日期", "时间", "date", "dt"),
    "open": ("开盘", "开盘价", "open"),
    "high": ("最高", "最高价", "high"),
    "low": ("最低", "最低价", "low"),
    "close": ("收盘", "收盘价", "close", "last"),
    "volume": ("成交量", "volume", "vol"),
    "amount": ("成交额", "成交金额", "amount"),
}


def _match_column(header_keys: List[str], aliases: Tuple[str, ...]) -> Optional[str]:
    lowered = {key.lower(): key for key in header_keys}
    for alias in aliases:
        if alias.lower() in lowered:
            return lowered[alias.lower()]
    for alias in aliases:
        for key in header_keys:
            if alias in key:
                return key
    return None


def _to_float(value: str) -> float:
    if value is None:
        return 0.0
    cleaned = value.replace(",", "").replace("%", "").replace("亿", "").replace("万", "").strip()
    if not cleaned or cleaned in {"-", "--", "null", "None"}:
        return 0.0
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(match.group()) if match else 0.0


def parse_klines(text: str, symbol: str, intraday: bool = False, period: str = DEFAULT_PERIOD) -> list[Bar]:
    """解析 kline 表格。

    intraday=True 时保留「日期+时间」（分钟 K 必须保留时间，否则同一交易日的
    多根 K 线会塌缩成同一个 date）。日线仍只取日期部分。
    """

    rows = parse_markdown_table(text)
    if not rows:
        raise MarketDataError(f"westock 返回内容无法解析为K线表格: {text[:200]}")
    header_keys = list(rows[0].keys())
    columns = {field: _match_column(header_keys, aliases) for field, aliases in _COLUMN_ALIASES.items()}
    for required in ("date", "open", "high", "low", "close"):
        if columns[required] is None:
            raise MarketDataError(f"K线数据缺少 {required} 列，实际列: {header_keys}")

    bars: list[Bar] = []
    for row in rows:
        row_symbol = row.get(columns["symbol"], symbol) if columns["symbol"] else symbol
        bar_date = row[columns["date"]]
        open_price = _to_float(row[columns["open"]])
        high_price = _to_float(row[columns["high"]])
        low_price = _to_float(row[columns["low"]])
        close_price = _to_float(row[columns["close"]])
        high_price = max(high_price, open_price, close_price, low_price)
        low_price = min(low_price, open_price, close_price, high_price)
        bars.append(
            Bar(
                symbol=str(row_symbol).strip() or symbol,
                date=bar_date.strip() if intraday else bar_date.split(" ")[0],
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=_to_float(row[columns["volume"]]) if columns["volume"] else 0.0,
                amount=_to_float(row[columns["amount"]]) if columns["amount"] else 0.0,
                period=period,
            )
        )
    return sorted(bars, key=lambda item: item.date)


def _kline_args(codes: str, period: str, days: int, fq: str) -> List[str]:
    """构造 kline 参数。分钟周期按数据源跨度上限取窗口，日线按 days 回溯。"""

    end = date.today()
    span = PERIODS[period]["span_days"]
    if span is None:
        start = end - timedelta(days=int(days * 1.6) + 30)
    else:
        start = end - timedelta(days=int(span))  # type: ignore[arg-type]
    return [
        "kline",
        codes,
        "--period",
        period,
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
        "--fq",
        fq,
    ]


def is_throttled(text: str) -> bool:
    """数据源限流判定。实测连续取数会返回「请求过于频繁，请稍后再试」。"""

    return "频繁" in text or "稍后再试" in text


def fetch_bars(
    symbol: str,
    period: str = DEFAULT_PERIOD,
    days: int = 400,
    fq: str = "qfq",
    retries: int = 3,
) -> List[Bar]:
    period = normalize_period(period)
    if period == "day" and days < 30:
        raise ValueError("days must be >= 30")

    # 1) 多源互补优先：baostock 长历史 + 东财当天/盘中尾部。
    #    返回的 report 记到 LAST_SOURCE_REPORT，便于 /api/health 排查
    #    「这只票到底用了哪个源、有没有源冲突」。
    if multi_source_enabled():
        try:
            from .sources import registry  # 局部导入：缺 baostock 时不影响 westock 路径

            bars, report = registry.fetch_bars(symbol, period, days, fq)
            if bars:
                LAST_SOURCE_REPORT[symbol] = report
                if report.get("conflicts"):
                    log.warning(
                        "%s %s 多源冲突 %s 根（最大 %.2f%%）",
                        symbol,
                        period,
                        report.get("conflicts"),
                        report.get("max_diff_pct", 0.0),
                    )
                return bars
        except Exception as exc:  # noqa: BLE001 - 任何异常都降级，不能把详情页拖死
            log.warning("多源取数失败，降级 westock: %s %s: %s", symbol, period, exc)

    # 2) 兜底：原 westock CLI 路径（逻辑保持原样，别在这里加新行为）
    args = _kline_args(symbol, period, days, fq)
    output = ""
    for attempt in range(retries):
        output = run_westock(args, timeout=90)
        if is_throttled(output):
            if attempt < retries - 1:
                # 长退避：短退避（2-4s）无法穿越限流窗口
                time.sleep(20.0 * (attempt + 1))
                continue
            raise MarketDataError(f"数据源限流：{output.strip()[:60]}（已重试 {retries} 次，请稍后再试）")
        return parse_klines(output, symbol, intraday=is_intraday(period), period=period)
    raise MarketDataError(f"数据源限流：{output.strip()[:60]}")


def fetch_daily_bars(symbol: str, days: int = 400, fq: str = "qfq") -> List[Bar]:
    """向后兼容入口：等价于 fetch_bars(symbol, 'day', days, fq)。"""

    return fetch_bars(symbol, DEFAULT_PERIOD, days, fq)


def fetch_bars_batch(
    symbols: List[str],
    period: str = DEFAULT_PERIOD,
    days: int = 400,
    fq: str = "qfq",
    batch_size: int = 20,
    batch_pause: float = 0.6,
) -> Tuple[Dict[str, List[Bar]], List[str]]:
    """Fetch bars for many symbols via batched westock calls.

    westock accepts at most 20 codes per call. Returns
    (bars_by_symbol, failed_symbols).
    """

    period = normalize_period(period)
    if period == "day" and days < 30:
        raise ValueError("days must be >= 30")
    bars_by_symbol: Dict[str, List[Bar]] = {}
    failed: List[str] = []

    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset : offset + batch_size]
        args = _kline_args(",".join(batch), period, days, fq)
        bars = None
        for attempt in range(3):
            try:
                output = run_westock(args, timeout=120)
                bars = parse_klines(output, symbol=batch[0], intraday=is_intraday(period), period=period)
                break
            except (MarketDataError, ValueError):
                if attempt < 2:
                    # 长退避：穿越服务端限流窗口（实测短退避 2-4s 无效）
                    time.sleep(20.0 * (attempt + 1))
        if bars is None:
            failed.extend(batch)
        else:
            for bar in bars:
                bars_by_symbol.setdefault(bar.symbol, []).append(bar)
            for symbol in batch:
                if symbol not in bars_by_symbol:
                    failed.append(symbol)
        if offset + batch_size < len(symbols) and batch_pause > 0:
            time.sleep(batch_pause)
    for symbol, bars in bars_by_symbol.items():
        bars_by_symbol[symbol] = sorted(bars, key=lambda item: item.date)
    return bars_by_symbol, failed


def fetch_daily_bars_batch(
    symbols: List[str],
    days: int = 400,
    fq: str = "qfq",
    batch_size: int = 20,
    batch_pause: float = 0.6,
) -> Tuple[Dict[str, List[Bar]], List[str]]:
    """向后兼容入口：等价于 fetch_bars_batch(symbols, 'day', ...)。"""

    return fetch_bars_batch(symbols, DEFAULT_PERIOD, days, fq, batch_size, batch_pause)


# 扫描范围：all=全部 A 股（实测 5576 只），hs300=沪深300 成分股（用于快速验证）
UNIVERSE_SCOPES = ("all", "hs300")
UNIVERSE_SCOPE_LABELS = {"all": "全部A股", "hs300": "沪深300成分股"}
UNIVERSE_TYPE_STOCK = "stock"
UNIVERSE_MARKET_HS = "hs"
UNIVERSE_INDEX_CODE = "sh000300"
# 数据源每次返回上限未公开，取一个明显大于 A 股总数的量，再用标题行校验
UNIVERSE_HARD_LIMIT = 6000

# 标题行形如：`📈 A股股票（5576 只）`；用它校验是否被 --limit 静默截断
_UNIVERSE_TOTAL_RE = re.compile(r"（\s*(\d+)\s*只\s*）")

# 全市场剔除口径（用户明确要求）：北交所 + 风险警示股。
# 实测 5576 只里北交所 350 只、ST 201 只（含 *ST），两者重叠 3 只，剔除后剩 5028 只。
EXCLUDED_SYMBOL_PREFIXES = ("bj",)
# 覆盖 ST / *ST / SST / S*ST 四种写法
_ST_NAME_RE = re.compile(r"^\s*(?:\*?ST|S\*ST|SST)", re.IGNORECASE)


def is_excluded_market(symbol: str) -> bool:
    """北交所（bj 前缀）不纳入选股。"""

    return symbol.strip().lower().startswith(EXCLUDED_SYMBOL_PREFIXES)


def is_st_name(name: str) -> bool:
    """风险警示股：ST / *ST / SST / S*ST 均算。"""

    return bool(_ST_NAME_RE.match(name or ""))


def apply_universe_filters(
    universe: list[tuple[str, str]], exclude_bj: bool = True, exclude_st: bool = True
) -> tuple[list[tuple[str, str]], dict]:
    """按剔除口径过滤证券池，并返回可对外展示的统计。"""

    kept: list[tuple[str, str]] = []
    stats = {"total": len(universe), "excluded_bj": 0, "excluded_st": 0}
    for symbol, name in universe:
        if exclude_bj and is_excluded_market(symbol):
            stats["excluded_bj"] += 1
            continue
        if exclude_st and is_st_name(name):
            stats["excluded_st"] += 1
            continue
        kept.append((symbol, name))
    stats["kept"] = len(kept)
    return kept, stats


def normalize_scope(scope: Optional[str]) -> str:
    key = (scope or "all").strip().lower()
    if key not in UNIVERSE_SCOPES:
        raise ValueError(f"不支持的扫描范围: {scope}（可选: {', '.join(UNIVERSE_SCOPES)}）")
    return key


def parse_universe_total(text: str) -> Optional[int]:
    """从标题行解析数据源声明的证券总数；解析不到返回 None（不阻断）。"""

    matched = _UNIVERSE_TOTAL_RE.search(text or "")
    return int(matched.group(1)) if matched else None


def parse_universe_rows(text: str) -> list[tuple[str, str]]:
    """把证券池/成分股的 markdown 表格解析成 (symbol, name) 列表。"""

    rows = parse_markdown_table(text)
    if not rows:
        return []
    header_keys = list(rows[0].keys())
    symbol_column = _match_column(header_keys, _COLUMN_ALIASES["symbol"])
    name_column = _match_column(header_keys, _COLUMN_ALIASES["name"])
    if symbol_column is None:
        raise MarketDataError(f"证券池数据缺少代码列，实际列: {header_keys}")
    universe: list[tuple[str, str]] = []
    for row in rows:
        symbol = str(row.get(symbol_column, "")).strip()
        if not symbol:
            continue
        name = str(row.get(name_column, "")).strip() if name_column else symbol
        universe.append((symbol, name))
    return universe


def fetch_all_a_share_symbols(
    limit: int = UNIVERSE_HARD_LIMIT, exclude_bj: bool = True, exclude_st: bool = True
) -> tuple[list[tuple[str, str]], dict]:
    """Return (universe, stats) for A-shares after applying the exclusion rules."""

    output = run_westock(
        [
            "universe",
            "--market",
            UNIVERSE_MARKET_HS,
            "--type",
            UNIVERSE_TYPE_STOCK,
            "--limit",
            str(limit),
        ],
        timeout=120,
    )
    universe = parse_universe_rows(output)
    if not universe:
        raise MarketDataError("无法获取全市场 A 股清单")
    total = parse_universe_total(output)
    if total is not None and len(universe) < total:
        raise MarketDataError(
            f"全市场清单不完整：数据源声明 {total} 只，实际解析 {len(universe)} 只（可能已被上限截断）"
        )
    kept, stats = apply_universe_filters(universe, exclude_bj=exclude_bj, exclude_st=exclude_st)
    if not kept:
        raise MarketDataError("按剔除口径过滤后证券池为空，请检查剔除规则")
    return kept, stats


def fetch_universe_symbols(
    scope: str = "all", exclude_bj: bool = True, exclude_st: bool = True
) -> tuple[list[tuple[str, str]], dict]:
    """Return (universe, stats) for the requested scope: all A-shares or CSI 300."""

    key = normalize_scope(scope)
    if key == "all":
        return fetch_all_a_share_symbols(exclude_bj=exclude_bj, exclude_st=exclude_st)
    output = run_westock(["index", "constituent", UNIVERSE_INDEX_CODE], timeout=60)
    universe = parse_universe_rows(output)
    if not universe:
        raise MarketDataError("无法获取指数成分股列表")
    return universe, {"total": len(universe), "excluded_bj": 0, "excluded_st": 0, "kept": len(universe)}


def fetch_quote_name(symbol: str) -> str:
    try:
        output = run_westock(["quote", symbol], timeout=20)
    except MarketDataError:
        return symbol
    rows = parse_markdown_table(output)
    if not rows:
        return symbol
    header_keys = list(rows[0].keys())
    name_column = _match_column(header_keys, _COLUMN_ALIASES["name"])
    if name_column and rows[0].get(name_column):
        return str(rows[0][name_column]).strip()
    return symbol


# ---------------------------------------------------------------------------
# 板块 / 大盘：个股 vs 板块 vs 大盘 的同期力度对比
#
# 数据源能力（本机实测，勿凭直觉改）：
# - `kline` 对板块（pt 前缀）与指数（sh000001）同样可用，且接受 --fq qfq，
#   所以板块/指数 K 线直接复用 fetch_bars 即可，不需要另写一套。
# - `quote` **不返回板块字段**，因此「这只票属于哪些板块」必须反查：
#     industry-chain stock <symbol>  → 主题名（如 白酒 / 食品饮料）
#     search <主题名> --type sector  → pt 代码（如 pt01801125 白酒Ⅱ）
#   两步都走网络，结果必须落库缓存，否则每次分析个股都要重新问一遍。
# - 连续调用会触发限流（实测「请求过于频繁」），两次之间必须留间隔。
# ---------------------------------------------------------------------------

MARKET_INDEX_CODE = "sh000001"
MARKET_INDEX_NAME = "上证指数"

# 板块查询之间的最小间隔（秒）。实测短于 3 秒就开始出现限流提示。
SECTOR_QUERY_PAUSE = 3.0

# 申万行业清单优先级：二级最常用（白酒Ⅱ 这类），概念板块兜底
SECTOR_LEVEL_BY_CATEGORY = {
    "申万一级行业清单": "sw1",
    "申万二级行业清单": "sw2",
    "申万三级行业清单": "sw3",
}
SECTOR_LEVEL_PRIORITY = ("sw2", "sw3", "sw1", "concept")


def parse_industry_chain_themes(text: str) -> List[str]:
    """从 `industry-chain stock` 输出里取「所属主题」名称。

    输出里有两张表（所属主题 / 所属节点），只认带 themeName 列的那张，
    否则会把产业链节点名（如「酱香型白酒」）当成板块名用。
    """

    themes: List[str] = []
    header: Optional[List[str]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            # 遇到非表格行就重置：下一张表会重新给表头
            header = None
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) != len(header):
            continue
        row = dict(zip(header, cells))
        theme = str(row.get("themeName", "")).strip()
        if theme and theme not in themes:
            themes.append(theme)
    return themes


def parse_sector_search(text: str) -> List[dict]:
    """从 `search <关键词> --type sector` 输出里取板块代码 / 名称 / 层级。"""

    sectors: List[dict] = []
    for row in parse_markdown_table(text):
        code = str(row.get("code", "")).strip()
        name = str(row.get("name", "")).strip()
        category = str(row.get("sectorCategory", "")).strip()
        if not code or not name:
            continue
        sectors.append(
            {
                "code": code,
                "name": name,
                "level": SECTOR_LEVEL_BY_CATEGORY.get(category, "concept"),
                "category": category,
            }
        )
    return sectors


def _sector_rank(theme: str, sector: dict) -> tuple:
    """板块挑选的排序键：先按层级，再按名称与主题的相关度。

    同名优先很关键：`search 白酒` 会同时返回「白酒Ⅱ」「非白酒」「白酒概念」，
    只看层级优先级会把「非白酒」也排到前面去。
    """

    level = sector["level"]
    level_rank = (
        SECTOR_LEVEL_PRIORITY.index(level)
        if level in SECTOR_LEVEL_PRIORITY
        else len(SECTOR_LEVEL_PRIORITY)
    )
    name = sector["name"]
    name_rank = 0 if (theme and (theme in name or name in theme)) else 1
    return (level_rank, name_rank)


def fetch_stock_sectors(
    symbol: str, max_themes: int = 3, pause: float = SECTOR_QUERY_PAUSE
) -> List[dict]:
    """反查个股所属板块，返回 [{code, name, level, from_theme}]。

    拿不到就返回空列表——板块只是辅助信息，绝不能因为反查失败让个股分析崩掉。
    """

    if max_themes <= 0:
        return []
    try:
        output = run_westock(["industry-chain", "stock", symbol], timeout=30)
    except MarketDataError:
        return []

    themes = parse_industry_chain_themes(output)[:max_themes]
    picked: List[dict] = []
    seen: set = set()
    for index, theme in enumerate(themes):
        if index > 0 and pause > 0:
            time.sleep(pause)
        try:
            found = parse_sector_search(run_westock(["search", theme, "--type", "sector"], timeout=30))
        except MarketDataError:
            continue
        candidates = [item for item in found if item["code"] not in seen]
        if not candidates:
            continue
        best = min(candidates, key=lambda item: _sector_rank(theme, item))
        seen.add(best["code"])
        picked.append({**best, "from_theme": theme})
    return picked

"""Market data provider backed by the westock CLI.

The provider never fabricates quotes: if the CLI is unavailable or not
authorized, it raises MarketDataError so callers can surface a real status.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from chanpicker.core import Bar

WESTOCK_BIN_CANDIDATES = (
    "westock",
    str(Path.home() / ".local" / "bin" / "westock"),
    "/usr/local/bin/westock",
)


class MarketDataError(RuntimeError):
    """Raised when market data cannot be fetched or parsed."""


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


def parse_klines(text: str, symbol: str) -> list[Bar]:
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
                date=bar_date.split(" ")[0],
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=_to_float(row[columns["volume"]]) if columns["volume"] else 0.0,
                amount=_to_float(row[columns["amount"]]) if columns["amount"] else 0.0,
            )
        )
    return sorted(bars, key=lambda item: item.date)


def fetch_daily_bars(symbol: str, days: int = 400, fq: str = "qfq") -> list[Bar]:
    if days < 30:
        raise ValueError("days must be >= 30")
    end = date.today()
    start = end - timedelta(days=int(days * 1.6) + 30)
    output = run_westock(
        ["kline", symbol, "--period", "day", "--start", start.isoformat(), "--end", end.isoformat(), "--fq", fq]
    )
    return parse_klines(output, symbol)


def fetch_daily_bars_batch(
    symbols: list[str],
    days: int = 400,
    fq: str = "qfq",
    batch_size: int = 20,
    batch_pause: float = 0.6,
) -> tuple[dict[str, list[Bar]], list[str]]:
    """Fetch daily bars for many symbols via batched westock calls.

    westock accepts at most 20 codes per call. Returns
    (bars_by_symbol, failed_symbols).
    """

    if days < 30:
        raise ValueError("days must be >= 30")
    end = date.today()
    start = end - timedelta(days=int(days * 1.6) + 30)
    bars_by_symbol: dict[str, list[Bar]] = {}
    failed: list[str] = []
    import time

    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset : offset + batch_size]
        args = [
            "kline",
            ",".join(batch),
            "--period",
            "day",
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
            "--fq",
            fq,
        ]
        bars = None
        for attempt in range(3):
            try:
                output = run_westock(args, timeout=120)
                bars = parse_klines(output, symbol=batch[0])
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


def fetch_universe_symbols() -> list[tuple[str, str]]:
    """Return (symbol, name) list from the CSI 300 index constituents."""

    output = run_westock(["index", "constituent", "sh000300"], timeout=60)
    rows = parse_markdown_table(output)
    if not rows:
        raise MarketDataError("无法获取指数成分股列表")
    header_keys = list(rows[0].keys())
    symbol_column = _match_column(header_keys, _COLUMN_ALIASES["symbol"])
    name_column = _match_column(header_keys, _COLUMN_ALIASES["name"])
    if symbol_column is None:
        raise MarketDataError(f"成分股数据缺少代码列，实际列: {header_keys}")
    universe: list[tuple[str, str]] = []
    for row in rows:
        symbol = str(row.get(symbol_column, "")).strip()
        if not symbol:
            continue
        name = str(row.get(name_column, "")).strip() if name_column else symbol
        universe.append((symbol, name))
    return universe


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

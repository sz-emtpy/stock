"""东方财富 HTTP 行情源：日线/分钟 K 线 + 实时报价。

为什么选它（2026-10-20 实测）：
- **日线能取到 25 年**（6024 根，2001 起），前复权真实生效
  （茅台 2026-06-15：不复权 1271.10 / 前复权 1243.08，差 28.02 元 = 每股分红）
- **盘中返回当天正在形成的那根 K 线**（10:36 请求拿到 11:00 那根）→ 负责「实时」
- 分钟只保留约 31 个交易日（服务器硬限制，改 beg 或翻页都没用）→ 深度交给 baostock

反爬注意：
- 必须带 UA 与 Referer，否则直接拒绝
- 实测连续 8 只后开始 RemoteDisconnected，所以内置节流与重试
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from chanpicker.core import Bar

from .base import (
    SourceError,
    ensure_ohlc_valid,
    is_intraday,
    normalize_symbol,
    normalize_time,
    safe_float,
    to_eastmoney_secid,
)

BASE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Referer": "https://quote.eastmoney.com/"}

# klt 参数：1/5/15/30/60 分钟，101 日线
KLT = {"m1": 1, "m5": 5, "m15": 15, "m30": 30, "m60": 60, "day": 101}

# 复权：0 不复权 1 前复权 2 后复权。
# 分钟线的 fqt 实测无效（0 与 1 结果完全一致），所以分钟只用 0，避免误导。
FQT = {"qfq": 1, "hfq": 2, "none": 0}

# 同进程内共享的节流器：东财按 IP 限频，多线程并发请求会一起被断连
_throttle_lock = threading.Lock()
_last_call = [0.0]
MIN_INTERVAL = 0.5

# 熔断：一旦被限流，短时间内不再尝试。
# 为什么必须要有：东财失败时会走超时+重试（最坏十几秒），而 baostock 已经能
# 兜住历史段；让它反复拖慢详情页是不可接受的。实测连续 8 只后就会被断连，
# 所以冷却 60s 比「每只都重试」划算得多。
_circuit = {"blocked_until": 0.0, "failures": 0}
CIRCUIT_COOLDOWN = 60.0


def _circuit_open() -> bool:
    return time.time() < _circuit["blocked_until"]


def _trip_circuit() -> None:
    _circuit["blocked_until"] = time.time() + CIRCUIT_COOLDOWN
    _circuit["failures"] += 1


def circuit_state() -> dict:
    return {
        "open": _circuit_open(),
        "resume_in": round(max(_circuit["blocked_until"] - time.time(), 0.0), 1),
        "failures": _circuit["failures"],
    }


def _throttle() -> None:
    with _throttle_lock:
        wait = MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _get_json(url: str, timeout: float = 5.0, retries: int = 2) -> dict:
    if _circuit_open():
        raise SourceError(f"东财熔断中（{circuit_state()['resume_in']}s 后重试）")
    last_error = ""
    for attempt in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 统一转成 SourceError 交给上层降级
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                # 东财是「连续 N 只后整段断连」的限流，短退避穿不过去。
                # 重试只留 2 次、超时压到 5s：调用方（详情页）等不起，
                # 失败就交给 baostock 的历史段，比「死等」体验好。
                time.sleep(1.2 * (attempt + 1))
    _trip_circuit()
    raise SourceError(f"东财请求失败（重试 {retries} 次）：{last_error}")


def available() -> bool:
    """轻量探活：拿一次茅台日线最后几根。失败即视为不可用。"""
    try:
        fetch_daily("sh600519", days=3, fq="qfq")
        return True
    except Exception:  # noqa: BLE001
        return False


def fetch_daily(symbol: str, days: int = 400, fq: str = "qfq") -> List[Bar]:
    """日线。前复权真实生效，可回溯到上市首日。

    注意：东财会**忽略 lmt** 直接返回较长区间（实测要 10 根回了 2492 根），
    所以这里必须自己截断，否则合并时会整段重叠、把大量「同一天但复权基准不同」
    的差异算成冲突。
    """
    secid = to_eastmoney_secid(symbol)
    lmt = max(int(days), 5)
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": KLT["day"],
        "fqt": FQT.get(fq, 1),
        "beg": 0,
        "end": 20500101,
        "lmt": lmt,
    }
    payload = _get_json(BASE_URL + "?" + urllib.parse.urlencode(params))
    bars = _parse_klines(payload, symbol, "day")
    return bars[-days:] if days > 0 and len(bars) > days else bars


def fetch_minute(symbol: str, period: str = "m30") -> List[Bar]:
    """分钟 K 线。深度约 31 个交易日（服务器上限），但包含当天正在形成的那根。"""
    if period not in KLT or period == "day":
        raise SourceError(f"不支持的分钟周期: {period}")
    secid = to_eastmoney_secid(symbol)
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": KLT[period],
        "fqt": 0,  # 分钟线 fqt 无效，显式写 0 表明口径是不复权
        "beg": 0,
        "end": 20500101,
        "lmt": 100000,
    }
    payload = _get_json(BASE_URL + "?" + urllib.parse.urlencode(params))
    return _parse_klines(payload, symbol, period)


def fetch_bars(symbol: str, period: str = "day", days: int = 400, fq: str = "qfq") -> List[Bar]:
    """统一入口，供 registry 调用。"""
    bars = fetch_minute(symbol, period) if is_intraday(period) else fetch_daily(symbol, days, fq)
    return bars[-days:] if days > 0 and len(bars) > days else bars


def _parse_klines(payload: dict, symbol: str, period: str) -> List[Bar]:
    data = (payload or {}).get("data") or {}
    rows = data.get("klines") or []
    if not rows:
        raise SourceError(f"东财未返回 K 线: {symbol} {period}")
    intraday = is_intraday(period)
    out: List[Bar] = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 6:
            continue
        date_text = normalize_time(parts[0]) if intraday else str(parts[0])[:10]
        prices = ensure_ohlc_valid(
            symbol,
            date_text,
            safe_float(parts[1]),
            safe_float(parts[3]),
            safe_float(parts[4]),
            safe_float(parts[2]),
        )
        if prices is None:
            continue
        open_, high, low, close = prices
        out.append(
            Bar(
                symbol=normalize_symbol(symbol),
                date=date_text,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=safe_float(parts[5]),
                amount=safe_float(parts[6]) if len(parts) > 6 else 0.0,
                period=period,
            )
        )
    if not out:
        raise SourceError(f"东财 K 线全部无效: {symbol} {period}")
    return sorted(out, key=lambda item: item.date)


def fetch_quotes(symbols: List[str]) -> Dict[str, dict]:
    """批量实时快照（最新价/涨跌/成交额）。盘中刷新用，比拉 K 线便宜。"""
    if not symbols:
        return {}
    secids = ",".join(to_eastmoney_secid(s) for s in symbols)
    params = {
        "secids": secids,
        # f13 是市场号（1=沪 0=深），不请求它就只能靠猜，会把深市票标成 sh
        "fields": "f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18",
        "fltt": 2,
        "invt": 2,
    }
    payload = _get_json(QUOTE_URL + "?" + urllib.parse.urlencode(params))
    rows = ((payload or {}).get("data") or {}).get("diff") or []
    if isinstance(rows, dict):  # 东财有时返回 {"0": {...}} 形式
        rows = list(rows.values())
    out: Dict[str, dict] = {}
    for row in rows:
        code = str(row.get("f12") or "")
        if not code:
            continue
        # 用请求方给的代码反查市场号，避免 f13 缺失时默认值把市场判错
        market = "sh" if str(row.get("f13", "")) == "1" else "sz"
        out[f"{market}{code}"] = {
            "symbol": f"{market}{code}",
            "name": row.get("f14"),
            "price": safe_float(row.get("f2")),
            "change_pct": safe_float(row.get("f3")),
            "change": safe_float(row.get("f4")),
            "volume": safe_float(row.get("f5")),
            "amount": safe_float(row.get("f6")),
            "high": safe_float(row.get("f15")),
            "low": safe_float(row.get("f16")),
            "open": safe_float(row.get("f17")),
            "last_close": safe_float(row.get("f18")),
        }
    return out

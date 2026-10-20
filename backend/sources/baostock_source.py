"""baostock 历史源：本系统「分钟历史太浅」的唯一免费解。

为什么必须用它（2026-10-20 实测）：
- **m30 = 13,152 根，2020-01-02 至今，约 1638 个交易日**（现状只有 10 天，差 160 倍）
- m15 = 26,304 根，同样 2020 起
- **分钟线复权真实生效**：除权日附近三种口径完全不同
  （不复权 1222.30 / 前复权 1194.04 / 后复权 9157.43）——
  westock 的分钟强制不复权、东财分钟 fqt 无效，只有它能给复权分钟
- 日线 2863 根（2015 起）

已知限制：
- **T+1**：当天数据要收盘后才有（实测末条是前一交易日 15:00）→ 盘中实时交给东财
- 日线查询的 fields **不能带 `time`**，否则报「日线指标参数传入错误:time」
- SDK 是进程级全局连接且非线程安全（FastAPI 同步端点跑在线程池）→ 用 RLock 串行
"""

from __future__ import annotations

import threading
from typing import List, Optional

from chanpicker.core import Bar

from .base import (
    SourceError,
    ensure_ohlc_valid,
    is_intraday,
    normalize_symbol,
    normalize_time,
    safe_float,
    to_baostock,
)

# baostock 的 frequency 取值
FREQ = {"day": "d", "m5": "5", "m15": "15", "m30": "30", "m60": "60"}
# adjustflag: 1 后复权 / 2 前复权 / 3 不复权
ADJUST = {"qfq": "2", "hfq": "1", "none": "3"}

_lock = threading.RLock()
_login_state = {"logged_in": False}
_import_error: Optional[str] = None


def _bs():
    """惰性导入，缺包时给出可执行的提示而不是 ImportError 栈。"""
    global _import_error
    try:
        import baostock as bs  # noqa: PLC0415 - 按需导入，避免拖慢启动
        return bs
    except Exception as exc:  # noqa: BLE001
        _import_error = f"{type(exc).__name__}: {exc}"
        raise SourceError(f"baostock 不可用（pip install baostock）：{_import_error}") from exc


def _ensure_login(bs) -> None:
    if _login_state["logged_in"]:
        return
    result = bs.login()
    if result.error_code != "0":
        raise SourceError(f"baostock 登录失败: {result.error_msg}")
    _login_state["logged_in"] = True


def logout() -> None:
    """进程退出/测试清理时调用。"""
    with _lock:
        if not _login_state["logged_in"]:
            return
        try:
            _bs().logout()
        except Exception:  # noqa: BLE001
            pass
        _login_state["logged_in"] = False


def available() -> bool:
    try:
        fetch_bars("sh600519", period="day", days=5, fq="qfq")
        return True
    except Exception:  # noqa: BLE001
        return False


def fetch_bars(
    symbol: str,
    period: str = "day",
    days: int = 400,
    fq: str = "qfq",
    start_date: Optional[str] = None,
) -> List[Bar]:
    """取 K 线。

    Args:
        symbol: 项目格式代码（sh600519）或 baostock 格式（sh.600519）。
        period: day / m5 / m15 / m30 / m60。
        days: 期望根数上限（用于裁剪返回，不用于决定起始日——那样会截断历史）。
        fq: qfq / hfq / none。分钟线只有在 baostock 上才真正支持复权。
        start_date: 显式起始日（YYYY-MM-DD）。None 时按周期给默认起点。

    Returns:
        按时间升序的 Bar 列表，`date` 在分钟周期带时间（"YYYY-MM-DD HH:MM"）。
    """
    if period not in FREQ:
        raise SourceError(f"baostock 不支持的周期: {period}")
    code = to_baostock(symbol)
    intraday = is_intraday(period)
    # 起始日必须按请求根数反推：固定从 2020 年拉的话，
    # 取 500 根 m30 也要传 13152 行，实测 26s；按需推窗口后只要 1~2s。
    start = start_date or _estimate_start(period, days)

    fields = "date,time,open,high,low,close,volume,amount" if intraday else "date,open,high,low,close,volume,amount"

    bs = _bs()
    with _lock:
        _ensure_login(bs)
        rs = bs.query_history_k_data_plus(
            code,
            fields,
            start_date=start,
            end_date=_today(),
            frequency=FREQ[period],
            adjustflag=ADJUST.get(fq, "2"),
        )
        if rs.error_code != "0":
            raise SourceError(f"baostock 查询失败: {rs.error_msg}")
        rows: List[List[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())

    if not rows:
        raise SourceError(f"baostock 无数据: {symbol} {period}")

    bars: List[Bar] = []
    for row in rows:
        # 分钟：date,time,o,h,l,c,vol,amount；日线：date,o,h,l,c,vol,amount
        if intraday:
            raw_time, o, h, l, c, vol, amt = row[1], row[2], row[3], row[4], row[5], row[6], row[7]
            date_text = normalize_time(raw_time)
        else:
            o, h, l, c, vol, amt = row[1], row[2], row[3], row[4], row[5], row[6]
            date_text = str(row[0])[:10]
        prices = ensure_ohlc_valid(
            symbol, date_text, safe_float(o), safe_float(h), safe_float(l), safe_float(c)
        )
        if prices is None:
            continue
        open_, high, low, close = prices
        bars.append(
            Bar(
                symbol=normalize_symbol(symbol),
                date=date_text,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=safe_float(vol),
                amount=safe_float(amt),
                period=period,
            )
        )
    if not bars:
        raise SourceError(f"baostock 数据全部无效: {symbol} {period}")
    bars.sort(key=lambda item: item.date)
    return bars[-days:] if days > 0 and len(bars) > days else bars


def count_available(symbol: str, period: str = "m30") -> int:
    """诊断用：某只票能取到多少根（不裁剪）。"""
    return len(fetch_bars(symbol, period, days=0, fq="none"))  # type: ignore[arg-type]


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


# 每个交易日的 K 线根数，用于把「要多少根」换算成「从哪天开始」
_BARS_PER_DAY = {"day": 1, "m60": 4, "m30": 8, "m15": 16, "m5": 48}


def _estimate_start(period: str, days: int) -> str:
    """按请求根数估算起始日，避免每次都拉全量历史。

    余量系数 1.5 是给周末/节假日/停牌留的（A 股一年约 244 个交易日，
    自然日/交易日 ≈ 1.5）。宁可多取几根再裁，也不要取不够。

    days=0 表示「要全量」（补历史脚本用），此时给该源的完整起点。
    """
    from datetime import date, timedelta

    if days <= 0:
        return "2020-01-01" if is_intraday(period) else "2015-01-01"
    bars_per_day = _BARS_PER_DAY.get(period, 1)
    trading_days = max(days // bars_per_day, 1) + 5
    span = int(trading_days * 1.5) + 10
    return (date.today() - timedelta(days=span)).isoformat()

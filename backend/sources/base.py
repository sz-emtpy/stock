"""多数据源公共层：符号格式转换、Bar 合并与交叉校验。

设计约束（改这里之前先读）：

1. **对外接口不变**：`backend.provider.fetch_bars` 等函数签名与返回类型保持原样，
   service 层与测试的大量打桩（`mock.patch.object(service_module, "fetch_bars")`）
   因此完全不受影响。多源融合发生在 provider 内部。
2. **周期与复权口径必须与现状一致**，否则历史信号会整体失真：
   - 日线：前复权（qfq）
   - 分钟：**不复权**（westock 的分钟强制不复权，东财分钟 fqt 也无效，
     只有保持不复权才能与已落库的历史信号可比）
3. **互补而非替换**：历史段用长历史源（baostock），尾部当天段用实时源（东财），
   重叠日逐根校验；任何一源失败只是降级，不阻断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from chanpicker.core import Bar

# 分钟周期的 date 带时间（"YYYY-MM-DD HH:MM"），日线只到日期
INTRADAY_PERIODS = ("m30", "m15", "m5", "m1")


class SourceError(RuntimeError):
    """数据源不可用（网络/协议/限流）。调用方应降级到下一个源。"""


def is_intraday(period: str) -> bool:
    return period in INTRADAY_PERIODS


def to_baostock(symbol: str) -> str:
    """sh600519 -> sh.600519；已带点的原样返回。"""
    s = symbol.strip().lower()
    if "." in s:
        return s
    if len(s) >= 8 and s[:2] in ("sh", "sz", "bj"):
        return f"{s[:2]}.{s[2:]}"
    raise SourceError(f"无法识别的代码格式: {symbol}")


def to_eastmoney_secid(symbol: str) -> str:
    """sh600519 -> 1.600519（沪=1，深=0）。北交所东财用 0，但本系统已剔除北交所。"""
    s = symbol.strip().lower()
    if "." in s:
        market, code = s.split(".", 1)
    elif len(s) >= 8 and s[:2] in ("sh", "sz", "bj"):
        market, code = s[:2], s[2:]
    else:
        raise SourceError(f"无法识别的代码格式: {symbol}")
    return f"{'1' if market == 'sh' else '0'}.{code}"


def normalize_symbol(symbol: str) -> str:
    """统一成项目内部格式 sh600519。"""
    s = symbol.strip().lower()
    if "." in s:
        market, code = s.split(".", 1)
        return f"{market}{code}"
    return s


def normalize_time(raw: str) -> str:
    """把各源的分钟时间统一成 'YYYY-MM-DD HH:MM'。

    支持：
    - 东财 "2026-10-20 11:00"
    - baostock "20261020110000000"（date + time 两列，调用方拼接后传入）
    """
    text = str(raw).strip()
    if not text:
        raise SourceError("空时间")
    if len(text) >= 14 and text[:8].isdigit():
        # baostock: YYYYMMDDHHMMSSsss
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
    if " " in text and ":" in text:
        day, clock = text.split(" ", 1)
        return f"{day} {clock[:5]}"
    return text


def _bar_key(bar: Bar, intraday: bool) -> str:
    return bar.date if intraday else bar.date[:10]


@dataclass
class MergeReport:
    """一次合并的元信息，供日志与诊断使用。"""

    history_source: str = ""
    tail_source: str = ""
    history_rows: int = 0
    tail_rows: int = 0
    overlap_rows: int = 0
    conflicts: int = 0
    max_diff_pct: float = 0.0
    used_fallback: bool = False

    def as_dict(self) -> dict:
        return {
            "history_source": self.history_source,
            "tail_source": self.tail_source,
            "history_rows": self.history_rows,
            "tail_rows": self.tail_rows,
            "overlap_rows": self.overlap_rows,
            "conflicts": self.conflicts,
            "max_diff_pct": round(self.max_diff_pct, 4),
            "used_fallback": self.used_fallback,
        }


def merge_bars(
    history: List[Bar],
    tail: List[Bar],
    period: str,
    tolerance_pct: float = 0.5,
) -> Tuple[List[Bar], MergeReport]:
    """把「长历史段」与「最新尾部段」合成一条连续序列。

    为什么需要它：baostock 有 6.8 年分钟历史但是 T+1（当天没有），
    东财有当天正在形成的 K 线但只有 31 个交易日。两者叠加才既深又新。

    合并规则：
    - 以时间键去重，**尾部段优先**（它更新，包含当天未收盘的那根）
    - 重叠日逐根比对收盘价，差异超过 tolerance_pct 记为冲突（不丢弃，只计数）
      冲突不丢弃是因为两边都可能有理：复权因子不同、盘中未收盘价 vs 收盘价。
      静默丢弃会让「为什么数字对不上」变成无法排查的黑盒。
    """
    intraday = is_intraday(period)
    report = MergeReport(history_rows=len(history), tail_rows=len(tail))

    merged: Dict[str, Bar] = {}
    for bar in history:
        merged[_bar_key(bar, intraday)] = bar

    history_close = {_bar_key(b, intraday): b.close for b in history}
    for bar in tail:
        key = _bar_key(bar, intraday)
        prev = history_close.get(key)
        if prev is not None and prev > 0:
            report.overlap_rows += 1
            diff = abs(bar.close - prev) / prev * 100
            report.max_diff_pct = max(report.max_diff_pct, diff)
            if diff > tolerance_pct:
                report.conflicts += 1
        merged[key] = bar  # 尾部优先

    out = sorted(merged.values(), key=lambda item: item.date)
    return out, report


def clamp_to_days(bars: List[Bar], days: int, period: str) -> List[Bar]:
    """按请求根数裁剪（各源能给的深度不同，统一收口）。

    日线按根数裁；分钟也按根数裁 —— 调用方传的 days 对分钟已经是
    「能取多少根」的语义（见 provider.period_max_bars）。
    """
    if days <= 0 or len(bars) <= days:
        return bars
    return bars[-days:]


def dedupe_sorted(bars: List[Bar]) -> List[Bar]:
    """同键去重 + 升序，兜底用（各源返回顺序不保证）。"""
    seen: Dict[str, Bar] = {}
    for bar in bars:
        seen[bar.date] = bar
    return sorted(seen.values(), key=lambda item: item.date)


def safe_float(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def ensure_ohlc_valid(
    symbol: str, date_text: str, open_: float, high: float, low: float, close: float
) -> Optional[Tuple[float, float, float, float]]:
    """把各源可能越界的 OHLC 归一（高取 max、低取 min），无效数据返回 None。

    Bar.__post_init__ 会对 high<low 或价格不在区间抛 ValueError；
    数据源偶发脏数据不该让整只票失败，这里先兜住再交给 Bar。
    """
    o, h, l, c = open_, high, low, close
    if min(o, h, l, c) <= 0:
        return None
    h = max(h, o, c, l)
    l = min(l, o, c, h)
    if h <= 0 or l <= 0 or l > h:
        return None
    return o, h, l, c

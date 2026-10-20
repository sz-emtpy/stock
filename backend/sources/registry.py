"""多源互补调度：把「长历史」与「最新盘中」合成一条连续序列。

分工（都是 2026-10-20 实测出来的事实，不是猜的）：

| 源 | 拿到什么 | 角色 |
|---|---|---|
| baostock | m30 13152 根 / 6.8 年，日线 11 年；T+1（当天没有） | **历史主力** |
| 东财 | 当天正在形成的 K 线；日线 25 年；分钟只有 31 天 | **尾部实时** |
| westock | 现有唯一源，批量快；分钟仅 10 天、强制不复权 | **兜底与批量扫描** |

**复权口径必须与现状一致**，否则已落库的历史信号会整体失真：
- 日线：前复权（qfq）
- 分钟：**不复权**（westock 强制不复权、东财分钟 fqt 也无效；只有保持不复权，
  baostock 的历史段与东财的当天段才能拼在一起）
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from chanpicker.core import Bar

from . import baostock_source, eastmoney
from .base import SourceError, is_intraday, merge_bars

log = logging.getLogger(__name__)

# 可用源开关，便于排查与灰度：CHANPICKER_SOURCES=baostock,eastmoney
ENV_SOURCES = "CHANPICKER_SOURCES"


def _enabled() -> List[str]:
    raw = os.getenv(ENV_SOURCES, "baostock,eastmoney")
    return [name.strip().lower() for name in raw.split(",") if name.strip()]


def effective_fq(period: str, fq: str) -> str:
    """分钟周期强制不复权，与 westock 及东财口径对齐（见模块 docstring）。"""
    return "none" if is_intraday(period) else fq


def _last_day(bars: List[Bar]) -> str:
    return bars[-1].date[:10] if bars else ""


def market_open(now: Optional[datetime] = None) -> bool:
    """是否在交易日盘中（9:15–15:05，含集合竞价与收盘余量）。"""
    cur = now or datetime.now()
    if cur.weekday() >= 5:
        return False
    minutes = cur.hour * 60 + cur.minute
    return (9 * 60 + 15) <= minutes <= (15 * 60 + 5)


def needs_tail(history: List[Bar], period: str, now: Optional[datetime] = None) -> bool:
    """是否还需要向实时源补「当天」这一段。

    用**数据**判断而不是用时钟判断，因为：
    - baostock 是 T+1，盘中/盘后它的末条都还是上一交易日 → 需要补
    - 到了次日它已经包含昨天 → 不该再打东财（省配额、省时间）

    日线例外：**盘中绝不补当天**。当天日线还没收盘，用它算出来的中枢/背驰
    到了收盘可能全部不成立；provider 里已有 `_prune_stale_signals` 就是为
    清理这种盘中假信号而写的，这里从源头挡住更干净。
    """
    cur = now or datetime.now()
    today = cur.date().isoformat()
    if not history:
        return True
    if _last_day(history) >= today:
        return False
    if cur.weekday() >= 5:
        return False  # 周末不会有新数据
    if is_intraday(period):
        return True  # 分钟：盘中要最新，盘后要当天完整段
    return cur.hour * 60 + cur.minute >= 15 * 60 + 5  # 日线只收盘后补


def fetch_bars(
    symbol: str, period: str = "day", days: int = 400, fq: str = "qfq"
) -> Tuple[List[Bar], dict]:
    """互补取数。返回 (bars, report)，bus 为空时抛 SourceError 让上层降级。

    Args:
        symbol: sh600519 形式。
        period: day / m30 / m15。
        days: 期望根数（对各源都是「最多取多少」）。
        fq: qfq / hfq / none；分钟会被强制改成 none。

    Returns:
        (按时间升序的 Bar 列表, 合并报告 dict)

    Raises:
        SourceError: 所有可用源都失败。
    """
    enabled = _enabled()
    use_fq = effective_fq(period, fq)
    intraday = is_intraday(period)
    report = {"period": period, "fq": use_fq, "sources": enabled}

    history: List[Bar] = []
    history_source = ""
    tail: List[Bar] = []
    tail_source = ""
    errors: List[str] = []

    if intraday:
        # ---- 分钟：历史段 + 当天尾部必须合并 ----
        # 为什么可以合：两边都是不复权（东财分钟 fqt 无效、westock 分钟强制不复权），
        # 实测重叠 248 根、冲突 0 根、最大偏差 0.008%，口径完全一致。
        if "baostock" in enabled:
            try:
                history = baostock_source.fetch_bars(symbol, period, days=days, fq=use_fq)
                history_source = "baostock"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"baostock: {exc}")
                log.debug("baostock 取数失败 %s %s: %s", symbol, period, exc)

        if "eastmoney" in enabled and needs_tail(history, period):
            try:
                tail = eastmoney.fetch_minute(symbol, period)
                tail_source = "eastmoney"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"eastmoney: {exc}")
                log.debug("东财取数失败 %s %s: %s", symbol, period, exc)
    else:
        # ---- 日线：**只用单一源，绝不跨源合并** ----
        # 实测两家的前复权基准不同：同一天 400 根里 71 根偏差 >0.5%、最大 0.99%。
        # 混起来会在除权处留下断层，笔/中枢/背驰的判定会因此翻转——这种错误
        # 不会报错，只会让信号悄悄变样。东财日线有 25 年且前复权已验证，
        # 所以首选它；baostock 只作为东财失败时的整段替换（仍不混用）。
        if "eastmoney" in enabled:
            try:
                history = eastmoney.fetch_daily(symbol, days=days, fq=use_fq)
                if market_open():
                    # 盘中东财会带上「今天还没收盘」的日线。用它算出来的中枢/背驰
                    # 到收盘可能全不成立（provider._prune_stale_signals 就是清理这种），
                    # 所以从源头剔除，宁可少一根也不给假信号。
                    today = datetime.now().date().isoformat()
                    trimmed = [b for b in history if b.date[:10] < today]
                    if trimmed:
                        history = trimmed
                history_source = "eastmoney"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"eastmoney: {exc}")
                log.debug("东财日线失败 %s: %s", symbol, exc)

        if not history and "baostock" in enabled:
            try:
                history = baostock_source.fetch_bars(symbol, "day", days=days, fq=use_fq)
                history_source = "baostock"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"baostock: {exc}")
                log.debug("baostock 日线失败 %s: %s", symbol, exc)

    # 3) 合并
    if history and tail:
        merged, merge_report = merge_bars(history, tail, period)
        report.update(merge_report.as_dict())
        report.update(
            {"history_source": history_source, "tail_source": tail_source, "errors": errors}
        )
        if merge_report.conflicts:
            log.warning(
                "%s %s 多源重叠不一致 %d 根（最大偏差 %.2f%%），可能复权口径不同",
                symbol,
                period,
                merge_report.conflicts,
                merge_report.max_diff_pct,
            )
        return merged[-days:] if days > 0 else merged, report

    if history:
        report.update(
            {
                "history_source": history_source,
                "tail_source": "",
                "history_rows": len(history),
                "tail_rows": 0,
                "errors": errors,
            }
        )
        return history, report

    if tail:
        report.update(
            {
                "history_source": "",
                "tail_source": tail_source,
                "history_rows": 0,
                "tail_rows": len(tail),
                "errors": errors,
            }
        )
        return tail, report

    raise SourceError("; ".join(errors) or "没有可用的数据源")


def fetch_quote(symbols: List[str]) -> Dict[str, dict]:
    """盘中实时快照。只有东财提供。"""
    if "eastmoney" not in _enabled():
        return {}
    try:
        return eastmoney.fetch_quotes(symbols)
    except Exception as exc:  # noqa: BLE001
        log.debug("实时报价失败: %s", exc)
        return {}


def status() -> dict:
    """各源探活（诊断接口用）。每次调用会真发一次小请求，别放轮询里。"""
    out: Dict[str, dict] = {"enabled": _enabled()}
    if "baostock" in _enabled():
        try:
            rows = baostock_source.fetch_bars("sh600519", "day", days=3, fq="qfq")
            out["baostock"] = {"ok": True, "rows": len(rows)}
        except Exception as exc:  # noqa: BLE001
            out["baostock"] = {"ok": False, "error": str(exc)[:200]}
    if "eastmoney" in _enabled():
        try:
            rows = eastmoney.fetch_daily("sh600519", days=3, fq="qfq")
            out["eastmoney"] = {"ok": True, "rows": len(rows)}
        except Exception as exc:  # noqa: BLE001
            out["eastmoney"] = {"ok": False, "error": str(exc)[:200]}
    return out

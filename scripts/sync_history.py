#!/usr/bin/env python3
"""用 baostock 补齐分钟/日线历史深度。

为什么需要它
------------
原来的分钟数据只能取到约 10 个交易日（westock 限制），导致 p100 买点定律
（日线二买 ← 30 分钟一买）根本没法验证：段口径下一买需要「中枢(3段) +
趋势背驰(两个同向中枢) + 背驰段」≈24 根，10 天窗口里几乎凑不出来。

baostock 实测能提供（2026-10-20 验证）：
- **m30：13,152 根，2020-01-02 起，约 1638 个交易日**
- m15：26,304 根，同样 2020 起
- 日线：2,863 根（2015 起）
- 分钟线**复权真实生效**（1后复权 / 2前复权 / 3不复权）

所以这个脚本能把「存量历史」真正补回来——这是 sync_intraday.py（每日增量）
做不到的，那个脚本只能让未来进入稳态。

口径约定（别改）
----------------
- 分钟写库用 **不复权**（adjustflag=3）。理由：历史 signals 里的分钟全是
  不复权口径（westock 强制不复权），换成复权会让新旧信号不可比。
- 日线写库用 **前复权**（adjustflag=2），与 qfq 口径一致。

范围与成本
----------
全市场 5000 只 × 13152 根 ≈ 6500 万行，数据库会从 544MB 涨到数 GB，且没必要：
分钟只服务「区间套确认 / 日内择时」，按项目原则只对**关注圈 + 候选池**补。

实测速度与成本（2026-10-20，单只 m30）
--------------------------------------
| 深度 | 根数 | 单只耗时 | 关注圈 2000 只 |
|---|---|---|---|
| 1 年 | ~2000 | ~5s | 约 2.8 小时 |
| 3 年 | ~5800 | ~12s | 约 6.7 小时 |
| 全量（2020 起） | 13152 | ~24s | 约 13 小时 |

所以默认取 **2000 根（约 1 年）** —— 足够覆盖绝大多数存量信号日
（p100 验证只需要信号日前后各十几个交易日），先跑得完比一次补到 2020 更重要。
要更久历史就显式传 `--days 0`（全量）或 `--days 5800`。

用法：
    python3 scripts/sync_history.py --period m30 --limit 50   # 先小规模验证
    python3 scripts/sync_history.py --period m30              # 关注圈，默认 1 年
    python3 scripts/sync_history.py --period m30 --days 5800  # 补到 3 年
    python3 scripts/sync_history.py --period m15 --limit 200
    python3 scripts/sync_history.py --period day              # 日线（前复权）
    python3 scripts/sync_history.py --symbols sh600519,sz000001 --dry-run

cron（每交易日 18:30，在日线扫描之后）：
    30 18 * * 1-5 cd /opt/chanpicker && .venv/bin/python scripts/sync_history.py \
        --period m30 >> /var/log/chanpicker/history.log 2>&1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from backend.sources import baostock_source  # noqa: E402
from backend.store import Store  # noqa: E402
from chanpicker.core import BUY_KINDS, TRACKING_STATES  # noqa: E402


def resolve_symbols(store: Store, scope: str) -> list[str]:
    if scope == "watchlist":
        return sorted(
            {item.symbol for item in store.load_watch_items() if item.state in TRACKING_STATES}
        )
    if scope == "candidates":
        dates = store.recent_trading_dates(30)
        cutoff = dates[-1] if dates else ""
        rows = store.buy_signals_since(cutoff, period="day") if cutoff else []
        kinds = {k.value for k in BUY_KINDS}
        return sorted({r["symbol"] for r in rows if r["kind"] in kinds})
    if scope == "all":
        return sorted({row["symbol"] for row in store.all_symbols()})
    raise ValueError(f"未知 scope: {scope}")


def main() -> int:
    parser = argparse.ArgumentParser(description="用 baostock 补齐历史 K 线")
    parser.add_argument("--period", default="m30", choices=["day", "m5", "m15", "m30", "m60"])
    parser.add_argument("--scope", default="watchlist", choices=["watchlist", "candidates", "all"])
    parser.add_argument("--symbols", default="", help="逗号分隔，指定后忽略 --scope")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少只（0=不限）")
    parser.add_argument(
        "--days", type=int, default=2000,
        help="取多少根，默认 2000（约 1 年分钟）；传 0 表示全量（2020 起，很慢）",
    )
    parser.add_argument("--pause", type=float, default=0.15, help="每只之间的间隔秒数")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args()

    db = Path(__file__).resolve().parent.parent / "data" / "chanpicker.db"
    store = Store()
    print(f"数据库: {db}")
    print(f"周期={args.period} 范围={args.scope} 根数={args.days or '全量'} 写库={not args.dry_run}")

    try:
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        else:
            symbols = resolve_symbols(store, args.scope)
        if not symbols:
            print("没有需要处理的标的")
            return 1
        if args.limit > 0:
            symbols = symbols[: args.limit]
        print(f"待处理 {len(symbols)} 只\n")

        # 口径：分钟不复权（与历史 signals 可比），日线前复权（与 qfq 一致）
        fq = "qfq" if args.period == "day" else "none"
        # bars 表的 fq 列：分钟写 none，日线写 qfq，读的时候要能对上
        fq_label = "qfq" if args.period == "day" else "none"

        ok = skipped = failed = 0
        written = 0
        started = time.time()

        for index, symbol in enumerate(symbols, 1):
            try:
                bars = baostock_source.fetch_bars(
                    symbol, period=args.period, days=args.days, fq=fq
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"[{index}/{len(symbols)}] {symbol} 失败: {exc}")
                continue
            if not bars:
                skipped += 1
                continue

            existing = len(store.load_bars(symbol, fq=fq_label, limit=10**6, period=args.period))
            if not args.dry_run:
                # save_bars 是 upsert：重复跑幂等，可以放心重试
                written += store.save_bars(bars, fq=fq_label, period=args.period)
            ok += 1

            if index % 20 == 0 or index == len(symbols):
                elapsed = time.time() - started
                rate = index / elapsed if elapsed else 0
                eta = (len(symbols) - index) / rate if rate else 0
                print(
                    f"[{index}/{len(symbols)}] {symbol} 取到 {len(bars)} 根"
                    f"（原有 {existing}）· 已成功 {ok} 失败 {failed} · "
                    f"{rate:.1f} 只/秒 · 预计剩余 {eta / 60:.0f} 分钟"
                )
            if args.pause > 0:
                time.sleep(args.pause)

        elapsed = time.time() - started
        print(
            f"\n完成：成功 {ok} / 跳过 {skipped} / 失败 {failed}，"
            f"写库 {written} 根，耗时 {elapsed / 60:.1f} 分钟"
        )
        if failed:
            print("失败标的可重跑本脚本补齐（写库是幂等的）")
        return 0 if ok else 1
    finally:
        baostock_source.logout()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

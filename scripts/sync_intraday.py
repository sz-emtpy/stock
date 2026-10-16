#!/usr/bin/env python3
"""每日增量拉取 30 分钟 K 线（只拉关注圈），为 p100 买点定律积累次级别数据。

为什么需要它
------------
缠中说禅买点定律（p100）说：
    「大级别的第二类买点由**次一级别**相应走势的第一类买点构成……
      有了这个缠中说禅买点定律，所有的买点都可以归结到第一类买点。」

也就是「日线二买 ← 30 分钟一买」。但实测数据源（westock）的分钟数据
**只有约 10 个交易日**（2026-09-28 ~ 2026-10-16 实测 81 根），
m30 名义上允许查 30 天，实际只回这么多。

所以：
- **存量二买**（今天之前）里 89% 落在数据窗口外，永久补不回；
- **未来的二买**——只要每天增量拉取，次日就有数据，T+3 起（p100 递归需要
  30 分钟上形成两个同向中枢，约 3 个交易日）就能做次级别验证。

**这个脚本只能让 p100 验证进入稳态，不能补历史。** 别指望它修复存量。

实测的取数约束
--------------
- 分钟 K 线**必须**用 `--start/--end`，`--limit` 无效
- m30 允许跨度 30 天（代码已用满），但实际只回约 10 个交易日
- 连续请求会触发限流「请求过于频繁」，实测需间隔 **25 秒以上**
- 分钟 K 线**强制不复权**（日线是 qfq），跨周期对比时口径不同，注意

运行成本
--------
关注圈约 1978 只，westock 每次最多 20 只 → 99 批 × 25 秒 ≈ **41 分钟**。
所以做成独立脚本，夜间与日线扫描并行，而不是塞进 daily_scan.py 阻塞它。

用法：
    python3 scripts/sync_intraday.py --limit 200      # 只拉 200 只，快速验证
    python3 scripts/sync_intraday.py                  # 拉关注圈全部
    python3 scripts/sync_intraday.py --scope all      # 拉全市场（很慢，慎用）
    python3 scripts/sync_intraday.py --period m15     # 换周期

cron 示例（每交易日 17:30，与日线扫描错开）：
    30 17 * * 1-5 cd /opt/chanpicker && .venv/bin/python scripts/sync_intraday.py \
        >> /var/log/chanpicker/intraday.log 2>&1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.provider import (  # noqa: E402
    MarketDataError,
    fetch_bars_batch,
    fetch_universe_symbols,
)
from backend.store import Store  # noqa: E402
from chanpicker import TRACKING_STATES  # noqa: E402

# 实测：连续请求触发限流「请求过于频繁」，25 秒以上才稳
BATCH_PAUSE = 25.0
BATCH_SIZE = 20
# 在圈约 1978 只 / 20 × 25s ≈ 41 分钟，给足 3 小时
MAX_WAIT_SECONDS = 3 * 3600


def load_watchlist_symbols(store: Store, scope: str, limit: int | None) -> list[tuple[str, str]]:
    """按范围取标的列表。

    scope="watchlist" 只取**在圈**的标的（有买卖点且状态未终结的）——
    已出圈的（FAILED / SECOND_BUY_FAILED）不会再更新二买，没必要继续拉分钟数据。
    实测在圈约 1978 只，若把出圈的也算进来是 4439 只，多一倍多的无效取数。
    scope="all" 取全市场（5028 只，一轮约 105 分钟，不建议日常跑）。
    """

    if scope == "all":
        universe, stats = fetch_universe_symbols("all")
        print(f"全市场 {stats.get('kept')} 只（已剔除北交所与风险警示股）")
    else:
        items = store.load_watch_items()
        active = [item for item in items if item.state in TRACKING_STATES]
        seen: dict[str, str] = {}
        for item in active:
            seen.setdefault(item.symbol, store.symbol_name(item.symbol))
        universe = list(seen.items())
        print(
            f"关注圈在圈 {len(universe)} 只"
            f"（关注记录共 {len(items)} 条，已出圈 {len(items) - len(active)} 条不再取数）"
        )
    if limit:
        universe = universe[:limit]
    return universe


def main() -> int:
    parser = argparse.ArgumentParser(description="每日增量拉取分钟 K 线")
    parser.add_argument(
        "--scope", default="watchlist", choices=("watchlist", "all"),
        help="拉取范围：watchlist=只拉关注圈（默认）；all=全市场（很慢）",
    )
    parser.add_argument("--period", default="m30", choices=("m30", "m15"), help="分钟周期")
    parser.add_argument("--limit", type=int, default=0, help="最多拉多少只（0=不限制）")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="每次请求的代码数")
    parser.add_argument("--batch-pause", type=float, default=BATCH_PAUSE, help="批间隔秒数（防限流）")
    args = parser.parse_args()

    store = Store()
    try:
        universe = load_watchlist_symbols(store, args.scope, args.limit or None)
        if not universe:
            print("没有需要拉取的标的", file=sys.stderr)
            return 1

        symbols = [sym for sym, _ in universe]
        total_batches = (len(symbols) + args.batch_size - 1) // args.batch_size
        eta = total_batches * args.batch_pause
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始增量拉取 {args.period}："
            f"{len(symbols)} 只 / {total_batches} 批，预计约 {eta / 60:.0f} 分钟"
        )

        bars_by_symbol, failed = fetch_bars_batch(
            symbols,
            period=args.period,
            days=400,  # 分钟周期忽略此参数，用满数据源的 30 天窗口
            batch_size=args.batch_size,
            batch_pause=args.batch_pause,
        )

        saved_bars = 0
        for symbol, bars in bars_by_symbol.items():
            if not bars:
                continue
            store.save_bars(bars, period=args.period)
            saved_bars += len(bars)

        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 完成："
            f"{len(bars_by_symbol)} 只成功，存入 {saved_bars} 根 {args.period}，"
            f"失败 {len(failed)} 只"
        )
        # 注意：这不是「补齐历史」，只是把每天的新数据留下来。
        # p100 的次级别验证因此只对**今天之后**的二买有效。
        print(
            "  注：分钟数据源只回约 10 个交易日，本脚本只能让未来的二买具备验证条件，"
            "无法补回历史。"
        )
        return 0 if bars_by_symbol else 1
    except MarketDataError as exc:
        print(f"取数失败：{exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
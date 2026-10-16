from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

from .core import (
    ChanConfig,
    ChanEngine,
    SignalKind,
    WatchItem,
    WatchlistManager,
    dumps_json,
    load_bars_csv,
)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return run_scan(args)
        if args.command == "watch":
            return run_watch(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary should report clean errors.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chanpicker", description="Chan theory first-buy watchlist and second-buy alert scanner.")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="scan one or more OHLCV CSV files")
    add_common_options(scan)
    scan.add_argument("csv", nargs="+", help="CSV path(s), columns: date,open,high,low,close,volume[,amount,symbol]")
    scan.add_argument("--symbol", help="override symbol; only valid when one CSV file is provided")
    scan.add_argument("--output", help="write JSON scan result to file")

    watch = sub.add_parser("watch", help="scan CSV files and update watchlist JSON")
    add_common_options(watch)
    watch.add_argument("csv", nargs="+", help="CSV path(s), columns: date,open,high,low,close,volume[,amount,symbol]")
    watch.add_argument("--watchlist", help="existing watchlist JSON file")
    watch.add_argument("--output", required=True, help="write updated watchlist JSON to file")
    return parser


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--period",
        default="day",
        choices=["day", "m30", "m15"],
        help="周期标签：minute CSV 传入 m30/m15，信号与 K 线会带上该周期（默认 day）",
    )
    parser.add_argument("--min-stroke-gap", type=int, default=4, help="minimum bar gap between two fractals to form a stroke")
    parser.add_argument("--min-divergence-score", type=float, default=0.05, help="minimum first-buy divergence score")
    parser.add_argument("--min-second-buy-score", type=float, default=0.0, help="minimum second-buy pullback weakening score")
    parser.add_argument("--min-third-buy-score", type=float, default=0.0, help="minimum third-buy pullback weakening score")
    parser.add_argument("--min-top-divergence-score", type=float, default=0.05, help="minimum first-sell top divergence score")
    parser.add_argument("--min-second-sell-score", type=float, default=0.0, help="minimum second-sell rebound weakening score")
    parser.add_argument("--min-macd-area", type=float, default=1e-9, help="MACD area below this value falls back to price effort")
    parser.add_argument("--no-zhongshu-required", action="store_true", help="allow first-buy signals without a prior center; useful for small samples")
    parser.add_argument("--max-wait-bars", type=int, default=30, help="bars to wait before downgrading a watch item")
    parser.add_argument("--cooldown-bars", type=int, default=60, help="cooldown bars after terminal watch states")


def config_from_args(args: argparse.Namespace) -> ChanConfig:
    return ChanConfig(
        min_stroke_gap=args.min_stroke_gap,
        min_divergence_score=args.min_divergence_score,
        min_second_buy_score=args.min_second_buy_score,
        min_third_buy_score=args.min_third_buy_score,
        min_top_divergence_score=args.min_top_divergence_score,
        min_second_sell_score=args.min_second_sell_score,
        min_macd_area=args.min_macd_area,
        require_zhongshu_for_first_buy=not args.no_zhongshu_required,
        max_wait_bars=args.max_wait_bars,
        cooldown_bars=args.cooldown_bars,
    )


def run_scan(args: argparse.Namespace) -> int:
    if args.symbol and len(args.csv) != 1:
        raise ValueError("--symbol can only be used with one CSV file")
    engine = ChanEngine(config_from_args(args))
    results = []
    for csv_path in args.csv:
        bars = load_bars_csv(csv_path, symbol=args.symbol)
        if args.period != "day":
            bars = [replace(bar, period=args.period) for bar in bars]
        result = engine.scan(bars)
        results.append(result.to_dict())
    payload = {"results": results}
    emit_payload(payload, args.output)
    return 0


def run_watch(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    engine = ChanEngine(config)
    manager = WatchlistManager(config)
    existing = load_watchlist(args.watchlist)
    updated: list[WatchItem] = [item for item in existing]
    scans = []
    for csv_path in args.csv:
        bars = load_bars_csv(csv_path)
        if args.period != "day":
            bars = [replace(bar, period=args.period) for bar in bars]
        result = engine.scan(bars)
        scans.append(result.to_dict())
        symbol_items = [item for item in updated if item.symbol == result.symbol]
        non_symbol_items = [item for item in updated if item.symbol != result.symbol]
        updated = non_symbol_items + manager.update(bars=bars, signals=result.signals, existing=symbol_items)
    payload = {"watchlist": [item.to_dict() for item in updated], "results": scans}
    emit_payload(payload, args.output)
    return 0


def load_watchlist(path: Optional[str]) -> list[WatchItem]:
    if not path:
        return []
    watch_path = Path(path)
    if not watch_path.exists():
        return []
    data = json.loads(watch_path.read_text(encoding="utf-8"))
    items = data.get("watchlist", data if isinstance(data, list) else [])
    return [WatchItem.from_dict(item) for item in items]


def emit_payload(payload: dict, output: Optional[str]) -> None:
    text = dumps_json(payload)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    raise SystemExit(main())

"""Business orchestration: single-symbol analysis and market scan tasks."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import date
from typing import Dict, List, Optional

from chanpicker.core import (
    BUY_KINDS,
    Bar,
    ChanConfig,
    ChanEngine,
    Signal,
    SignalKind,
    WatchlistManager,
    load_bars_csv,
)

BUY_KIND_VALUES = {kind.value for kind in BUY_KINDS}


def signal_score(signal: Signal) -> float:
    for key in ("divergence_score", "second_buy_score", "third_buy_score"):
        if key in signal.meta:
            return float(signal.meta[key])
    return 0.0

from .provider import MarketDataError, fetch_daily_bars, fetch_quote_name, fetch_universe_symbols
from .store import Store

DEFAULT_ANALYSIS_DAYS = 400


class ScanService:
    def __init__(self, store: Store, config: Optional[ChanConfig] = None) -> None:
        self.store = store
        self.config = config or ChanConfig()
        self.engine = ChanEngine(self.config)
        self.manager = WatchlistManager(self.config)
        self._threads: dict[str, threading.Thread] = {}
        self._thread_lock = threading.Lock()

    def analyze_symbol(
        self,
        symbol: str,
        days: int = DEFAULT_ANALYSIS_DAYS,
        use_cache: bool = True,
        bars: Optional[List[Bar]] = None,
    ) -> dict:
        symbol = symbol.strip()
        if not symbol:
            raise ValueError("symbol 不能为空")

        if bars is None:
            bars = self._load_bars(symbol, days, use_cache)
        if len(bars) < 30:
            raise MarketDataError(f"{symbol} 有效K线不足（{len(bars)} 根，需 ≥30），无法进行日线级别分析")

        result = self.engine.scan(bars)
        watch_items = self.manager.update(bars=bars, signals=result.signals, existing=self.store.load_watch_items(symbol))
        self.store.save_signals(result.signals)
        self.store.save_watch_items(watch_items)
        name = self.store.symbol_name(symbol)
        if name == symbol and bars is not None:
            try:
                name = fetch_quote_name(symbol)
                self.store.upsert_symbol(symbol, name)
            except MarketDataError:
                name = symbol
        return self._build_analysis_payload(symbol, name, bars, result.to_dict(), watch_items)

    def _load_bars(self, symbol: str, days: int, use_cache: bool) -> list[Bar]:
        cached = self.store.load_bars(symbol, limit=days) if use_cache else []
        today = date.today().isoformat()
        if cached and cached[-1].date == today:
            return cached[-days:]
        try:
            fresh = fetch_daily_bars(symbol, days=days)
        except MarketDataError:
            if cached:
                return cached[-days:]
            raise
        self.store.save_bars(fresh)
        return fresh[-days:]

    def _build_analysis_payload(self, symbol: str, name: str, bars: list[Bar], result: dict, watch_items: list) -> dict:
        watch_payload = [item.to_dict() for item in watch_items]
        latest_signals = [sig for sig in result["signals"] if sig["kind"] in {kind.value for kind in BUY_KINDS}]
        qualifies = bool(latest_signals)
        if not qualifies:
            watch = next((item for item in watch_payload if item["state"] in {"WAIT_SECOND_BUY", "WEAK_TRACK", "OPPORTUNITY"}), None)
            qualifies = watch is not None
        return {
            "symbol": symbol,
            "name": name,
            "bars_count": len(bars),
            "date_range": {"start": bars[0].date, "end": bars[-1].date},
            "qualifies": qualifies,
            "bars": [
                {
                    "date": bar.date,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "amount": bar.amount,
                }
                for bar in bars
            ],
            "analysis": result,
            "watch": watch_payload,
        }

    def start_market_scan(self, scope: str = "hs300") -> dict:
        with self._thread_lock:
            for thread in self._threads.values():
                if thread.is_alive():
                    raise RuntimeError("已有一个全市场扫描任务在运行，请等待完成后再启动")
        try:
            universe = fetch_universe_symbols()
        except MarketDataError:
            universe = [(row["symbol"], row["name"]) for row in self.store.all_symbols()]
            if not universe:
                raise
        run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        for symbol, name in universe:
            self.store.upsert_symbol(symbol, name)
        self.store.create_run(run_id, scope=scope, total=len(universe))
        thread = threading.Thread(target=self._run_market_scan, args=(run_id, universe), daemon=True)
        with self._thread_lock:
            self._threads[run_id] = thread
        thread.start()
        return {"run_id": run_id, "total": len(universe), "scope": scope}

    def _run_market_scan(self, run_id: str, universe: list[tuple[str, str]]) -> None:
        from .provider import fetch_daily_bars_batch

        hits: list[dict] = []
        errors: list[str] = []
        processed = 0
        batch_size = 20
        symbols = [symbol for symbol, _ in universe]
        name_by_symbol = dict(universe)

        for offset in range(0, len(symbols), batch_size):
            batch_symbols = symbols[offset : offset + batch_size]
            bars_by_symbol: dict[str, list[Bar]] = {}
            # 当日已有缓存的股票直接复用，避免重复调用数据源（定时扫描因此可秒级完成）
            need_fetch: list[str] = []
            today = date.today().isoformat()
            for symbol in batch_symbols:
                cached = self.store.load_bars(symbol, limit=DEFAULT_ANALYSIS_DAYS) if self.store.latest_bar_date(symbol) == today else []
                if len(cached) >= 30:
                    bars_by_symbol[symbol] = cached
                else:
                    need_fetch.append(symbol)
            failed: list[str] = []
            if need_fetch:
                try:
                    fetched, failed = fetch_daily_bars_batch(
                        need_fetch, DEFAULT_ANALYSIS_DAYS, batch_size=batch_size, batch_pause=2.0
                    )
                    bars_by_symbol.update(fetched)
                except MarketDataError as exc:
                    errors.append(f"batch@{offset}: {exc}")
                    failed = list(need_fetch)
            for symbol in failed:
                try:
                    bars_by_symbol[symbol] = self._load_bars(symbol, DEFAULT_ANALYSIS_DAYS, use_cache=True)
                except Exception as exc:  # noqa: BLE001 - per-symbol fallback failure
                    errors.append(f"{symbol}: {exc}")
            for symbol, bars in bars_by_symbol.items():
                self.store.save_bars(bars)

            for symbol in batch_symbols:
                bars = bars_by_symbol.get(symbol) or []
                if len(bars) >= 30:
                    result = self.engine.scan(bars)
                    if result.signals:
                        self.store.save_signals(result.signals)
                        watch_items = self.manager.update(
                            bars=bars, signals=result.signals, existing=self.store.load_watch_items(symbol)
                        )
                        self.store.save_watch_items(watch_items)
                        for signal in result.signals:
                            if signal.kind in BUY_KINDS:
                                hits.append(
                                    {
                                        "symbol": symbol,
                                        "name": name_by_symbol.get(symbol, symbol),
                                        "kind": signal.kind.value,
                                        "date": signal.date,
                                        "price": signal.price,
                                        "score": signal_score(signal),
                                        "message": signal.message,
                                    }
                                )
                processed += 1
            self.store.update_run(run_id, processed=processed, hits=len(hits))
            self.store.save_hits(run_id, hits)

        self.store.save_hits(run_id, hits)
        message = f"完成：{len(hits)} 个信号" + (f"；{len(errors)} 只获取失败" if errors else "")
        self.store.finish_run(run_id, status="finished", message=message)

    def scan_status(self, run_id: Optional[str] = None) -> dict:
        run = self.store.get_run(run_id) if run_id else self.store.latest_run()
        if run is None:
            return {"status": "idle", "message": "尚未执行过全市场扫描"}
        hits = self.hits_with_names(self.store.hits_for_run(run["run_id"]))
        run = dict(run)
        run["hits"] = hits
        return run

    def hits_with_names(self, hits: list[dict]) -> list[dict]:
        for hit in hits:
            if not hit.get("name"):
                hit["name"] = self.store.symbol_name(hit["symbol"])
        return hits

    def candidates(self, recent_days: int = 30) -> dict:
        from datetime import timedelta

        cutoff = (date.today() - timedelta(days=recent_days)).isoformat()
        run = self.store.latest_run()
        payload = {
            "last_run": None,
            "cutoff": cutoff,
            "recent_days": recent_days,
            "first_buy": [],
            "second_buy": [],
            "third_buy": [],
        }
        if run:
            hits = self.hits_with_names(self.store.hits_for_run(run["run_id"]))
            recent_hits = [hit for hit in hits if str(hit.get("date", "")) >= cutoff]
            payload["last_run"] = {
                "run_id": run["run_id"],
                "status": run["status"],
                "processed": run["processed"],
                "total": run["total"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "message": run["message"],
            }
            payload["first_buy"] = [hit for hit in recent_hits if hit["kind"] == SignalKind.FIRST_BUY.value]
            payload["second_buy"] = [hit for hit in recent_hits if hit["kind"] == SignalKind.SECOND_BUY.value]
            payload["third_buy"] = [hit for hit in recent_hits if hit["kind"] == SignalKind.THIRD_BUY.value]
        return payload

    def pools(self, days: int = 3) -> dict:
        """三日买点池：最近 N 个交易日内触发的一买 / 二买 / 三买。

        口径（明确定义，避免歧义）：
        - 「N 日」= 最近 N 个**交易日**（取自本地 K 线缓存的日期集合），非自然日；
        - 数据源为 signals 表（全部已扫描标的的历史信号），不依赖最近一次扫描任务；
        - 同一标的在同一买点类型下只保留最近一条记录。
        """

        trading_dates = self.store.recent_trading_dates(days)
        cutoff = trading_dates[-1] if trading_dates else ""
        rows = self.store.buy_signals_since(cutoff) if cutoff else []
        grouped: dict[str, list[dict]] = {
            SignalKind.FIRST_BUY.value: [],
            SignalKind.SECOND_BUY.value: [],
            SignalKind.THIRD_BUY.value: [],
        }
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (row["symbol"], row["kind"])
            if key in seen:
                continue
            seen.add(key)
            row["name"] = self.store.symbol_name(row["symbol"])
            grouped.setdefault(row["kind"], []).append(row)
        for items in grouped.values():
            items.sort(key=lambda item: (str(item.get("date", "")), item.get("score", 0)), reverse=True)
        return {
            "days": days,
            "cutoff": cutoff,
            "trading_dates": list(reversed(trading_dates)),
            "first_buy": grouped[SignalKind.FIRST_BUY.value],
            "second_buy": grouped[SignalKind.SECOND_BUY.value],
            "third_buy": grouped[SignalKind.THIRD_BUY.value],
        }

    def watchlist(self) -> list[dict]:
        items = []
        for item in self.store.load_watch_items():
            payload = item.to_dict()
            payload["name"] = self.store.symbol_name(item.symbol)
            items.append(payload)
        return items

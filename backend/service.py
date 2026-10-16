"""Business orchestration: single-symbol analysis and market scan tasks."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import date
from typing import Any, Dict, List, Optional

from chanpicker.masystem import confirm_by_sublevel_first_buy
from chanpicker.core import (
    BUY_KINDS,
    CLOSED_STATES,
    SELL_KINDS,
    TRACKING_STATES,
    WATCH_STATE_LABELS,
    Bar,
    ChanConfig,
    ChanEngine,
    Signal,
    SignalKind,
    WatchlistManager,
    WatchState,
    load_bars_csv,
    signal_score_of,
)

BUY_KIND_VALUES = {kind.value for kind in BUY_KINDS}
SELL_KIND_VALUES = {kind.value for kind in SELL_KINDS}

def signal_score(signal: Signal) -> float:
    """信号强度：统一走 core.signal_score_of，避免各处口径漂移。"""

    return signal_score_of(signal.meta)


def _pct(success: int, sample: int) -> str:
    """样本为 0 时给「—」而不是 0%，避免把「没数据」显示成「成功率 0%」。"""

    if not sample:
        return "—"
    return f"{success / sample * 100:.1f}%"


def _success_rate_message(first_closed: int, second_confirmed: int) -> str:
    if not first_closed and not second_confirmed:
        return "本地关注圈还没有已终结的记录，先执行一次市场扫描沉淀历史"
    parts = []
    if first_closed:
        parts.append(f"一买样本 {first_closed} 笔")
    if second_confirmed:
        parts.append(f"二买样本 {second_confirmed} 笔")
    return "统计自本地关注圈历史（" + "，".join(parts) + "），非实时行情推导"


# 个股级成功率的最小样本量：历史少于这么多次不给百分比。
# 1 次成功记成 100%、1 次失败记成 0%，都会被误读成可靠结论。
SYMBOL_RATE_MIN_SAMPLE = 3

from .provider import (
    DEFAULT_PERIOD,
    LAUNCH_PERIODS,
    UNIVERSE_SCOPE_LABELS,
    MarketDataError,
    fetch_bars,
    fetch_quote_name,
    fetch_universe_symbols,
    normalize_period,
    normalize_scope,
    period_max_bars,
)
from .store import Store

DEFAULT_ANALYSIS_DAYS = 400
# 缠论最小可用 K 线数：低于此值分型/笔/中枢都无法成立
MIN_BARS = 30

PERIOD_LABELS = {"day": "日线", "m30": "30分钟", "m15": "15分钟"}

KIND_LABELS = {
    "FIRST_BUY": "一买（入圈）",
    "SECOND_BUY": "二买（机会）",
    "THIRD_BUY": "三买（趋势延续）",
    "FIRST_SELL": "一卖（顶背驰）",
    "SECOND_SELL": "二卖（反弹不创新高）",
}

# 扫描范围文案（键与 provider.UNIVERSE_SCOPES 一致）
SCOPE_LABELS = dict(UNIVERSE_SCOPE_LABELS)


class ScanService:
    def __init__(self, store: Store, config: Optional[ChanConfig] = None) -> None:
        self.store = store
        self.config = config or ChanConfig()
        self.engine = ChanEngine(self.config)
        self.manager = WatchlistManager(self.config)
        self._threads: dict[str, threading.Thread] = {}
        self._thread_lock = threading.Lock()
        # 进程是被 kill 掉的（手动停服务、OOM、部署重启）时，scan_runs 里会残留
        # status='running' 的行，前端会永远轮询一个永远不会结束的任务。
        # 启动时一次性把这些残留行判为 interrupted，页面立刻回到可再次扫描的状态。
        self.store.mark_interrupted_runs()

    def analyze_symbol(
        self,
        symbol: str,
        days: int = DEFAULT_ANALYSIS_DAYS,
        use_cache: bool = True,
        bars: Optional[List[Bar]] = None,
        period: str = DEFAULT_PERIOD,
    ) -> dict:
        symbol = symbol.strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        period = normalize_period(period)

        if bars is None:
            bars = self._load_bars(symbol, days, use_cache, period)
        if len(bars) < MIN_BARS:
            label = PERIOD_LABELS.get(period, period)
            raise MarketDataError(
                f"{symbol} 有效K线不足（{len(bars)} 根，需 ≥{MIN_BARS}），无法进行{label}级别分析"
            )

        result = self.engine.scan(bars)
        self.store.save_signals(result.signals)

        # 关注圈状态机只由日线买点驱动：分钟级别是日内择时，不应改写日线关注状态。
        watch_items = []
        if period == DEFAULT_PERIOD:
            watch_items = self.manager.update(
                bars=bars, signals=result.signals, existing=self.store.load_watch_items(symbol)
            )
            self.store.save_watch_items(watch_items)

        name = self.store.symbol_name(symbol)
        if name == symbol:
            try:
                name = fetch_quote_name(symbol)
                self.store.upsert_symbol(symbol, name)
            except MarketDataError:
                name = symbol
        payload = self._build_analysis_payload(symbol, name, bars, result.to_dict(), watch_items, period)
        if period != DEFAULT_PERIOD:
            payload["daily_context"] = self.daily_context(symbol)
        # 该股自身的历史一买/二买成功率（仅日线：分钟级别不参与选圈统计）
        if period == DEFAULT_PERIOD:
            try:
                payload["symbol_success_rate"] = self.symbol_success_rate(symbol, period=period)
            except Exception:  # noqa: BLE001 - 统计失败不该拖垮整个分析
                payload["symbol_success_rate"] = {
                    "available": False,
                    "symbol": symbol,
                    "message": "该股历史成功率暂不可用",
                }
        return payload

    def daily_context(self, symbol: str) -> dict:
        """日线背景信息，供分钟级别做「级别联立」参考。

        为什么不做严格的跨周期「时序共振」：数据源只保留约 9 个交易日的分钟数据，
        而日线信号往往落在数月之前，两者在时间轴上几乎不可能重叠，硬做只会得到一个
        永远为空的功能。这里退一步给出**同向性参考**——把日线最新信号直接摆出来，
        由使用者判断分钟级别是否与日线同向，这信息是真实可得的。

        口径：取本地 signals 表中该股**日线**信号里 bar_index 最大的一条（bar_index
        在周期间不可比，所以必须先按 period 过滤再比较）。
        """

        rows = self.store.signals_for_symbol(symbol, limit=50, period=DEFAULT_PERIOD)
        if not rows:
            return {
                "available": False,
                "message": "本地暂无该股日线信号缓存，先按日线分析一次或执行市场扫描即可补齐",
            }
        latest = max(rows, key=lambda row: row["bar_index"])
        kind = latest["kind"]
        return {
            "available": True,
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "date": latest["date"],
            "price": latest["price"],
            "score": latest["score"],
            "is_buy": kind in BUY_KIND_VALUES,
            "is_sell": kind in SELL_KIND_VALUES,
            "message": latest["message"],
        }

    def _load_bars(self, symbol: str, days: int, use_cache: bool, period: str = DEFAULT_PERIOD) -> list[Bar]:
        limit = period_max_bars(period)
        cached = self.store.load_bars(symbol, limit=limit, period=period) if use_cache else []
        today = date.today().isoformat()

        if period == DEFAULT_PERIOD:
            # 日线：当日已有数据即可复用（收盘后不再变化）
            if cached and cached[-1].date == today:
                return cached[-days:]
        # 分钟级别：缓存只作为兜底。分钟 K 在盘中持续变化，远端数据也仅保留约 1 个月，
        # 若直接用当日缓存会导致「日内择时」基于上午的行情做判断。
        try:
            fresh = fetch_bars(symbol, period=period, days=days)
        except MarketDataError:
            if cached:
                return cached
            raise
        self.store.save_bars(fresh, period=period)
        return fresh[-limit:]

    def _build_analysis_payload(
        self,
        symbol: str,
        name: str,
        bars: list[Bar],
        result: dict,
        watch_items: list,
        period: str = DEFAULT_PERIOD,
    ) -> dict:
        watch_payload = [item.to_dict() for item in watch_items]
        all_signals = result["signals"]
        buy_signals = [sig for sig in all_signals if sig["kind"] in BUY_KIND_VALUES]
        risk_signals = [sig for sig in all_signals if sig["kind"] in SELL_KIND_VALUES]

        # 买点决定「是否入选」；卖点只做风险提示，两者语义互斥，不能混合。
        qualifies = bool(buy_signals)
        if not qualifies:
            watch = next(
                (item for item in watch_payload if item["state"] in TRACKING_STATES),
                None,
            )
            qualifies = watch is not None

        # 风险是否「当前仍然有效」：看最后一根 K 上确认的最新信号是不是卖点。
        last_signal = max(all_signals, key=lambda sig: (sig["bar_index"], sig["kind"])) if all_signals else None
        risk_active = bool(last_signal and last_signal["kind"] in SELL_KIND_VALUES)

        return {
            "symbol": symbol,
            "name": name,
            "period": period,
            "period_label": PERIOD_LABELS.get(period, period),
            "bars_count": len(bars),
            "date_range": {"start": bars[0].date, "end": bars[-1].date},
            "qualifies": qualifies,
            "risk_active": risk_active,
            "risk_signals": risk_signals,
            "buy_signals": buy_signals,
            "last_signal": last_signal,
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

    def start_market_scan(self, scope: str = "all", force: bool = False) -> dict:
        key = normalize_scope(scope)
        with self._thread_lock:
            for thread in self._threads.values():
                if thread.is_alive():
                    raise RuntimeError("已有一个扫描任务在运行，请等待完成后再启动")
        try:
            universe, universe_stats = fetch_universe_symbols(key)
        except MarketDataError:
            # 数据源拿不到清单时退回库内已有标的，保证服务可用（范围会明显变小，故写入 message）
            universe = [(row["symbol"], row["name"]) for row in self.store.all_symbols()]
            universe_stats = {"total": len(universe), "excluded_bj": 0, "excluded_st": 0, "kept": len(universe)}
            if not universe:
                raise
        run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        for symbol, name in universe:
            self.store.upsert_symbol(symbol, name)
        self.store.create_run(run_id, scope=key, total=len(universe))
        thread = threading.Thread(
            target=self._run_market_scan, args=(run_id, universe, force), daemon=True
        )
        with self._thread_lock:
            self._threads[run_id] = thread
        thread.start()
        return {
            "run_id": run_id,
            "total": len(universe),
            "scope": key,
            "scope_label": SCOPE_LABELS.get(key, key),
            "force": force,
            "universe": universe_stats,
        }

    def _run_market_scan(self, run_id: str, universe: list[tuple[str, str]], force: bool = False) -> None:
        from .provider import fetch_daily_bars_batch

        hits: list[dict] = []
        errors: list[str] = []
        processed = 0
        reused = 0
        batch_size = 20
        symbols = [symbol for symbol, _ in universe]
        name_by_symbol = dict(universe)
        started_at = time.time()
        # 缓存基准 = 本地库里的最新交易日，而不是 date.today()。
        # 用 today 会导致非交易日（周末/节假日）扫描时判定全部缓存失效、全量重取，
        # 全市场下等于每次都要付 30~50 分钟。
        market_dates = self.store.recent_trading_dates(1)
        base_date = market_dates[0] if market_dates else date.today().isoformat()

        try:
            for offset in range(0, len(symbols), batch_size):
                batch_symbols = symbols[offset : offset + batch_size]
                bars_by_symbol: dict[str, list[Bar]] = {}
                # 已有最新交易日缓存的股票直接复用，避免重复调用数据源（定时扫描因此可秒级完成）
                need_fetch: list[str] = []
                for symbol in batch_symbols:
                    cached: list[Bar] = []
                    if not force and self.store.latest_bar_date(symbol) == base_date:
                        cached = self.store.load_bars(symbol, limit=DEFAULT_ANALYSIS_DAYS)
                    if len(cached) >= MIN_BARS:
                        bars_by_symbol[symbol] = cached
                        reused += 1
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
                    if len(bars) >= MIN_BARS:
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
                self.store.update_run(
                    run_id,
                    processed=processed,
                    hits=len(hits),
                    message=self._eta_message(processed, len(symbols), started_at, reused),
                )
                self.store.save_hits(run_id, hits)
        except Exception as exc:  # noqa: BLE001 - 中断也要落状态，便于前端提示可续跑
            self.store.save_hits(run_id, hits)
            self.store.finish_run(
                run_id,
                status="interrupted",
                message=f"已中断在 {processed}/{len(symbols)}：{exc}（重新扫描会跳过已取数据，从断点继续）",
            )
            return

        self.store.save_hits(run_id, hits)
        message = f"完成：{len(hits)} 个信号（复用缓存 {reused} 只，实取 {len(symbols) - reused} 只）"
        if errors:
            message += f"；{len(errors)} 只获取失败"
        self.store.finish_run(run_id, status="finished", message=message)

    def _eta_message(self, processed: int, total: int, started_at: float, reused: int) -> str:
        """按已耗时线性外推剩余时间；前 20 只样本太少，不给估计值。"""

        elapsed = time.time() - started_at
        if processed < 20 or processed >= total:
            return f"已处理 {processed}/{total}"
        remain = elapsed / processed * (total - processed)
        minutes = remain / 60.0
        eta = f"{minutes:.0f} 分钟" if minutes >= 1 else f"{max(remain, 0):.0f} 秒"
        return f"已处理 {processed}/{total} · 预计剩余 {eta} · 复用缓存 {reused} 只"

    def scan_status(self, run_id: Optional[str] = None) -> dict:
        run = self.store.get_run(run_id) if run_id else self.store.latest_run()
        if run is None:
            return {"status": "idle", "message": "尚未执行过扫描"}
        hits = self.hits_with_names(self.store.hits_for_run(run["run_id"]))
        run = dict(run)
        run["hits"] = hits
        run["scope"] = run.get("scope") or "all"
        run["scope_label"] = SCOPE_LABELS.get(run["scope"], run["scope"])
        return run

    def hits_with_names(self, hits: list[dict]) -> list[dict]:
        """批量补名。逐行 symbol_name() 是 N+1，扫描一轮就是几千次查询。"""

        names = self.store.symbol_names(hit["symbol"] for hit in hits)
        for hit in hits:
            if not hit.get("name"):
                hit["name"] = names.get(hit["symbol"], hit["symbol"])
        return hits

    def candidates(self, recent_days: int = 30) -> dict:
        from datetime import timedelta

        cutoff = (date.today() - timedelta(days=recent_days)).isoformat()
        run = self.store.latest_run()
        scope = (run.get("scope") if run else None) or "all"
        payload = {
            "last_run": None,
            "cutoff": cutoff,
            "recent_days": recent_days,
            "scope": scope,
            "scope_label": SCOPE_LABELS.get(scope, scope),
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
                "scope": scope,
                "scope_label": SCOPE_LABELS.get(scope, scope),
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

    def _group_recent(self, rows: list[dict], kinds: tuple) -> dict:
        """按信号类型分组 + 同标的去重（保留同一类型下最近一条）。"""

        grouped: dict[str, list[dict]] = {kind.value: [] for kind in kinds}
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for row in rows:
            key = (row["symbol"], row["kind"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        # 批量补名，而不是每行一次查询
        names = self.store.symbol_names(row["symbol"] for row in deduped)
        for row in deduped:
            row["name"] = names.get(row["symbol"], row["symbol"])
            grouped.setdefault(row["kind"], []).append(row)
        for items in grouped.values():
            items.sort(key=lambda item: (str(item.get("date", "")), item.get("score", 0)), reverse=True)
        return grouped

    def pools(self, days: int = 3, period: str = DEFAULT_PERIOD) -> dict:
        """N 日买点池：最近 N 个交易日内触发的一买 / 二买 / 三买。

        口径（明确定义，避免歧义）：
        - 「N 日」= 最近 N 个**交易日**（取自本地日线缓存的日期集合），非自然日；
        - 数据源为 signals 表（全部已扫描标的的历史信号），不依赖最近一次扫描任务；
        - 同一标的在同一买点类型下只保留最近一条记录；
        - 默认只统计日线信号；分钟级别需要显式传 period。
        """

        period = normalize_period(period)
        trading_dates = self.store.recent_trading_dates(days)
        cutoff = trading_dates[-1] if trading_dates else ""
        rows = self.store.buy_signals_since(cutoff, period=period) if cutoff else []
        grouped = self._group_recent(rows, BUY_KINDS)
        return {
            "days": days,
            "period": period,
            "cutoff": cutoff,
            "trading_dates": list(reversed(trading_dates)),
            "first_buy": grouped[SignalKind.FIRST_BUY.value],
            "second_buy": grouped[SignalKind.SECOND_BUY.value],
            "third_buy": grouped[SignalKind.THIRD_BUY.value],
        }

    def risk_pool(self, days: int = 3, period: str = DEFAULT_PERIOD) -> dict:
        """N 日卖点风险池：最近 N 个交易日内触发的一卖 / 二卖。

        与买点池是并列关系而非同一张表：买点用于选股，卖点用于持仓风险提示。
        """

        period = normalize_period(period)
        trading_dates = self.store.recent_trading_dates(days)
        cutoff = trading_dates[-1] if trading_dates else ""
        rows = self.store.sell_signals_since(cutoff, period=period) if cutoff else []
        grouped = self._group_recent(rows, SELL_KINDS)
        return {
            "days": days,
            "period": period,
            "cutoff": cutoff,
            "trading_dates": list(reversed(trading_dates)),
            "first_sell": grouped[SignalKind.FIRST_SELL.value],
            "second_sell": grouped[SignalKind.SECOND_SELL.value],
        }

    def sublevel_second_buy_confirmations(self, limit: Optional[int] = None) -> dict:
        """按 p100 买点定律，为关注圈内的二买做次级别一买验证。

        原文（p100/p101）：
        > 「**大级别的第二类买点由次一级别相应走势的第一类买点构成。**
        > 有了这个缠中说禅买点定律，所有的买点都可以归结到第一类买点。」

        **为什么只对关注圈做**：数据源对分钟周期只保留约 9 个交易日
        （实测 m30 仅 144 根 / 2 只），全市场 5028 只逐只拉次级别数据既不现实
        也会触发限流。关注圈是「已经出现过买点」的小集合，在它上面做次级别
        验证才可行。

        `limit` 限制处理数量，防止关注圈过大时单次请求拖垮服务
        （次级别数据要现取，有网络开销）。
        """

        cfg = self.config
        sublevel_period = "m30"
        engine = ChanEngine(cfg)
        cutoff = limit if limit and limit > 0 else None

        items = [
            item for item in self.store.load_watch_items()
            if item.second_buy_date
        ]
        if cutoff:
            items = items[:cutoff]

        results: List[dict] = []
        verified = 0
        skipped = 0
        for item in items:
            sub_bars = self.store.load_bars(item.symbol, limit=200, period=sublevel_period)
            if not sub_bars:
                skipped += 1
                continue
            confirmation = confirm_by_sublevel_first_buy(
                symbol=item.symbol,
                second_buy_date=item.second_buy_date,
                sublevel_bars=sub_bars,
                sublevel_period=sublevel_period,
                engine=engine,
            )
            if confirmation.matched_date:
                verified += 1
            results.append(
                {
                    "symbol": item.symbol,
                    "name": self.store.symbol_name(item.symbol),
                    "second_buy_date": item.second_buy_date,
                    **confirmation.to_meta()["sublevel_rule"],
                }
            )

        return {
            "period": sublevel_period,
            "checked": len(results),
            "skipped_no_data": skipped,
            "verified": verified,
            "items": results,
            "message": (
                f"p100 买点定律：「大级别的第二类买点由次一级别相应走势的第一类买点构成」。"
                f"本次检查 {len(results)} 只关注圈标的，其中 {verified} 只在"
                f"{sublevel_period} 找到对应的一买（{skipped} 只无次级别数据跳过）。"
                "注意：次级别数据源仅保留约 9 个交易日，验证范围因此受限。"
            ),
        }

    def symbol_success_rate(self, symbol: str, period: str = DEFAULT_PERIOD) -> dict:
        """**单只个股**的历史一买/二买成功率（区别于 watchlist_stats 的全市场口径）。

        为什么需要单独一套：`watchlist` 表按 symbol 唯一，只保留最新一条关注记录，
        表达不了「历史上第 1 次一买成功、第 2 次失败」。个股级成功率必须从
        `signals` 表按时间线**重新配对**——每条信号都是一次独立尝试。

        口径（与全市场口径保持一致，避免两处数字打架）：
        - **一买成功** = 该次一买之后 `max_wait_bars` 根 K 线内出现二买
          （只认之后的二买，之前的不能配对——那是未来函数）
        - **二买成功** = 该次二买之后，收盘未跌破二买低点（`anchor_price`）
        - **样本 < 3 次不给百分比**：1 次成功记成 100% 会严重误导

        数据边界：本函数只反映**本地信号表**里已有历史的个股。没有历史信号的
        股票返回 `available=False`，而不是给一个假的 0%。
        """

        period = normalize_period(period)
        symbol = symbol.strip()
        rows = self.store.signals_for_symbol(symbol, limit=1000, period=period)
        first_buys = [r for r in rows if r["kind"] == SignalKind.FIRST_BUY.value]
        second_buys = [r for r in rows if r["kind"] == SignalKind.SECOND_BUY.value]
        if not first_buys and not second_buys:
            return {
                "available": False,
                "symbol": symbol,
                "period": period,
                "period_label": PERIOD_LABELS.get(period, period),
                "name": self.store.symbol_name(symbol),
                "message": "本地暂无该股历史信号，首次分析或扫描后才会积累",
            }

        # 按 bar_index 升序：bar_index 是 K 线窗口内的相对位置，
        # 同一只票的多次扫描会有偏移，用 trade_date 兜底排序更稳。
        first_buys.sort(key=lambda r: (str(r["date"]), r["bar_index"]))
        second_buys.sort(key=lambda r: (str(r["date"]), r["bar_index"]))

        # 二买的结局要查真实 K 线（收盘是否跌破二买低点），信号表里没有价格序列；
        # 一买↔二买的间隔也要换算成交易日序，不能直接相减 signals.bar_index
        bars = self.store.load_bars(symbol, limit=DEFAULT_ANALYSIS_DAYS, period=period)
        bars_by_date = {bar.date: bar for bar in bars}
        bar_pos = {bar.date: index for index, bar in enumerate(bars)}

        first_detail = [self._judge_first_buy(fb, second_buys, bar_pos) for fb in first_buys]
        second_detail = [self._judge_second_buy(sb, bars_by_date) for sb in second_buys]

        return {
            "available": True,
            "symbol": symbol,
            "name": self.store.symbol_name(symbol),
            "period": period,
            "period_label": PERIOD_LABELS.get(period, period),
            "first_buy": self._summarize(first_detail, "引出二买"),
            "second_buy": self._summarize(second_detail, "守住二买低点"),
            "wait_bars": self.config.max_wait_bars,
            "min_sample": SYMBOL_RATE_MIN_SAMPLE,
            "message": f"统计自本地信号表该股 {len(first_buys)} 次一买 / {len(second_buys)} 次二买的历史表现",
        }

    def _judge_first_buy(self, first_buy: dict, second_buys: list[dict], bar_pos: dict) -> dict:
        """一次一买的结局：之后 `max_wait_bars` 个交易日内是否出现二买。

        间隔必须用**交易日序**（信号日在 K 线序列里的位置），不能直接相减
        signals.bar_index：bar_index 是「K 线窗口内的相对位置」，窗口长度一变
        （用户传 days=600 与 days=400）同一信号的下标就整体平移。两次不同扫描
        写下的信号相减会算出完全错误的间距——实测 sh600000 的 2026-02-27 一买
        与 2026-08-19 二买 bar_index 相差 118，实际只隔了 20 多个交易日，
        结果是本该成功的一次配对被误判成「未引出二买」。
        """

        fb_date = str(first_buy["date"])
        fb_pos = bar_pos.get(fb_date)
        base = {"date": fb_date, "price": first_buy["price"]}
        if fb_pos is None:
            # 信号日不在当前 K 线窗口内（历史太久被截掉），无法判定间隔
            return {**base, "success": None, "result": "out_of_window", "result_label": "超出K线窗口"}
        matched = None
        for sb in second_buys:
            sb_pos = bar_pos.get(str(sb["date"]))
            if sb_pos is None or sb_pos <= fb_pos:
                continue  # 只认之后的二买；拿之前的配对就是未来函数
            if sb_pos - fb_pos <= self.config.max_wait_bars:
                matched = sb
                break
        if matched is None:
            return {**base, "success": False, "result": "no_second_buy", "result_label": "未引出二买"}
        return {
            **base,
            "success": True,
            "result": "second_buy",
            "result_label": f"→ 二买 {matched['date']}",
            "second_buy_date": matched["date"],
            "second_buy_price": matched["price"],
        }

    def _judge_second_buy(self, second_buy: dict, bars_by_date: dict) -> dict:
        """一次二买的结局：之后有没有跌破二买低点。

        判据与 `WatchlistManager` 的二买失败转移保持一致：收盘跌破该次二买的
        回调低点（`anchor_price`）即失败，窗口同样是 `max_wait_bars`。

        必须真的去查 K 线——不能用「后面还有没有信号」来近似，那既不准确，
        也会把「还没走出窗口」误判成失败。
        """

        sb_date = str(second_buy["date"])
        low = second_buy["anchor_price"]
        later = [bar for date, bar in bars_by_date.items() if date > sb_date]
        later.sort(key=lambda bar: bar.date)
        window = later[: self.config.max_wait_bars]

        base = {
            "date": sb_date,
            "price": second_buy["price"],
            "second_buy_low": low,
        }
        if not window:
            # 没有后续 K 线说明数据只到信号日那天，不算失败也不算成功
            return {
                **base,
                "success": None,
                "result": "no_followup",
                "result_label": "暂无后续K线",
            }
        broke = next((bar for bar in window if bar.close < low), None)
        if broke is None:
            return {**base, "success": True, "result": "held", "result_label": "守住二买低点"}
        return {
            **base,
            "success": False,
            "result": "broke_low",
            "result_label": f"{broke.date} 跌破二买低点",
            "break_date": broke.date,
            "break_close": broke.close,
        }

    @staticmethod
    def _summarize(detail: list[dict], success_label: str) -> dict:
        """把逐次判定汇总成成功率。样本不足时不返回 percentage。"""

        closed = [d for d in detail if d["success"] is not None]
        success = sum(1 for d in closed if d["success"])
        sample = len(closed)
        insufficient = sample < SYMBOL_RATE_MIN_SAMPLE
        return {
            "total": len(detail),
            "closed": sample,
            "success": success,
            "failed": sample - success,
            "rate": None if insufficient or not sample else round(success / sample, 4),
            "rate_label": "样本不足" if insufficient or not sample else _pct(success, sample),
            "insufficient": insufficient,
            "success_label": success_label,
            "detail": list(reversed(detail)),  # 最近的排在前面
        }

    def watchlist(self) -> list[dict]:
        """关注圈：只返回仍在跟踪中的状态（待二买 / 弱跟踪 / 机会已触发）。

        一买失败与二买失败属于「已出圈」，记录仍留在库里供成功率统计，
        但不再占据关注圈首屏——否则 2000+ 条历史会把当期标的冲掉。
        """

        items = []
        watch_items = self.store.load_watch_items()
        names = self.store.symbol_names(item.symbol for item in watch_items)
        for item in watch_items:
            if item.state not in TRACKING_STATES:
                continue
            payload = item.to_dict()
            payload["name"] = names.get(item.symbol, item.symbol)
            items.append(payload)
        return items

    def watchlist_stats(self) -> dict:
        """基于本地关注圈历史，统计一买 / 二买成功率与出圈计数。

        口径（写死在这里，避免前后端各算一套）：
        - **一买**：一买入圈后最终走到「二买确认」记为成功（无论二买后来是否失败，
          一买的目的就是引出二买）；「跌破一买低点」与「超时弱跟踪」记为失败；
          仍在「待二买」的记录尚未终结，不计入分母，单列为进行中。
        - **二买**：只统计已确认过二买的记录。考验窗口（max_wait_bars 根）内
          守住二买低点记为成功，窗口内跌破记为失败；窗口外才破位不计失败。
        """

        items = self.store.load_watch_items()
        states: dict[str, int] = {}
        for item in items:
            key = item.state.value
            states[key] = states.get(key, 0) + 1

        opportunity = states.get(WatchState.OPPORTUNITY.value, 0)
        second_failed = states.get(WatchState.SECOND_BUY_FAILED.value, 0)
        first_failed = states.get(WatchState.FAILED.value, 0)
        weak = states.get(WatchState.WEAK_TRACK.value, 0)
        pending = states.get(WatchState.WAIT_SECOND_BUY.value, 0)

        first_success = opportunity + second_failed
        first_closed = first_success + first_failed + weak
        second_confirmed = opportunity + second_failed

        def _rate(success: int, sample: int) -> Optional[float]:
            return round(success / sample, 4) if sample else None

        removed = {
            WatchState.FAILED.value: first_failed,
            WatchState.SECOND_BUY_FAILED.value: second_failed,
        }
        return {
            "first_buy": {
                "success": first_success,
                "failed": first_failed,
                "weak": weak,
                "pending": pending,
                "closed": first_closed,
                "rate": _rate(first_success, first_closed),
                "rate_label": _pct(first_success, first_closed),
            },
            "second_buy": {
                "success": opportunity,
                "failed": second_failed,
                "confirmed": second_confirmed,
                "rate": _rate(opportunity, second_confirmed),
                "rate_label": _pct(opportunity, second_confirmed),
            },
            "watchlist": {
                "active": sum(states.get(state.value, 0) for state in TRACKING_STATES),
                "removed": removed,
                "removed_total": sum(removed.values()),
                "states": {state.value: states.get(state.value, 0) for state in WatchState},
                "labels": {state.value: WATCH_STATE_LABELS[state] for state in WatchState},
            },
            "wait_bars": self.config.max_wait_bars,
            "message": _success_rate_message(first_closed, second_confirmed),
        }

    def refresh_watchlist(self) -> dict:
        """用本地缓存的日线 K 线重跑关注圈（不联网）。

        存在的意义：二买失败要在「后续 K 线」上才能观测到，而关注圈里的记录是历次
        扫描沉淀下来的，新增交易日之后必须主动重评一次才会翻状态。全程走本地缓存，
        实测全量重评约几秒，因此做成同步接口。
        """

        symbols = [
            item.symbol
            for item in self.store.load_watch_items()
            if item.state in TRACKING_STATES
        ]
        result: Dict[str, Any] = {
            "requested": len(symbols),
            "evaluated": 0,
            "skipped": 0,
            "changed": 0,
            "states": {},
        }
        for symbol in symbols:
            bars = self.store.load_bars(symbol, limit=DEFAULT_ANALYSIS_DAYS)
            if len(bars) < MIN_BARS:
                result["skipped"] += 1
                continue
            before = {item.state for item in self.store.load_watch_items(symbol)}
            scan_result = self.engine.scan(bars)
            # 信号一并落库：老库里的信号没有 previous_segment / current_segment，
            # 重评时顺手补齐，避免「接口能画背驰段、库里却没有」的不一致。
            # ON CONFLICT DO UPDATE 只覆盖日期/价格/meta，确定性引擎下其余列完全相同。
            if scan_result.signals:
                self.store.save_signals(scan_result.signals)
            items = self.manager.update(
                bars=bars, signals=scan_result.signals, existing=self.store.load_watch_items(symbol)
            )
            self.store.save_watch_items(items)
            after = {item.state for item in items}
            if before != after:
                result["changed"] += 1
            for state in after:
                key = state.value
                result["states"][key] = result["states"].get(key, 0) + 1
            result["evaluated"] += 1
        result["message"] = (
            f"已重评 {result['evaluated']} 只（跳过 {result['skipped']} 只无本地K线），"
            f"{result['changed']} 只状态发生变化"
        )
        return result

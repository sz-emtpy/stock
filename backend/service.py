"""Business orchestration: single-symbol analysis and market scan tasks."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace
from datetime import date
from typing import Any, Dict, List, Optional

from chanpicker.effort import macd_state, measure_effort
from chanpicker.indicators import kdj, returns_std, rsi, sma
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
    MARKET_INDEX_CODE,
    MARKET_INDEX_NAME,
    PERIODS,
    UNIVERSE_SCOPE_LABELS,
    MarketDataError,
    fetch_bars,
    fetch_quote_name,
    fetch_stock_sectors,
    fetch_universe_symbols,
    normalize_period,
    normalize_scope,
    period_max_bars,
)
from .store import Store
from .trading_system import (
    DEFAULT_WIN_THRESHOLD,
    SYSTEM_FEATURES,
    base_win_rate,
    feature_gains,
    score_signal,
    structure_buy_cards,
    validate_by_regime,
    validate_time_split,
)

DEFAULT_ANALYSIS_DAYS = 400
# 缠论最小可用 K 线数：低于此值分型/笔/中枢都无法成立
MIN_BARS = 30

# 板块 / 指数 K 线在 bars 表里用的专用 fq 标记。
# 个股是 qfq 前复权，板块指数没有复权概念；用独立标记存，两条路径互不干扰，
# 也不会被任何按 fq='qfq' 查询的地方误读。
SECTOR_REF_FQ = "ref"
# 同期对比所需的最少共同交易日：太少的话涨跌幅和力度都没有统计意义
COMPARISON_MIN_BARS = 30

PERIOD_LABELS = {"day": "日线", "m30": "30分钟", "m15": "15分钟"}

# 区间套：日线买点日前后多少日历日内出现的 30 分钟买点算「同位置」。
# 日线买点的确认 K 线在分型后一根，m30 同位置买点可能落在当日或邻近一两天，
# 给 ±3 天余量既能接住这种偏差，又不会把毫不相干的远端信号拉来充数。
INTERVAL_NESTING_WINDOW_DAYS = 3

# 同一只票的 K 线在多少秒内不重复联网抓取。
# 日线一天只变一次，设成 6 小时足够：同一天内反复点详情、左右切换都不会再抓。
BARS_COOLDOWN_SECONDS = 6 * 3600

# 分钟级别必须用**短冷却**：分钟 K 盘中一直在变，用日线那套 6 小时冷却会让
# 「日内择时」拿着上午的行情做下午的判断（原实现是每次都抓，正是为了这个）。
# 5 分钟足够挡掉「反复点分析」的重复请求，又不至于给出过时数据。
MINUTE_BARS_COOLDOWN_SECONDS = 5 * 60

# 板块反查失败后的冷却期。反查一次要 1 次产业链 + 最多 3 次板块搜索（间隔 3 秒），
# 实测 5.7 秒——失败后若每次进来都重试，详情页会一直被拖住。
SECTOR_MISS_COOLDOWN_SECONDS = 6 * 3600

# 买点结局的持有窗口（交易日）：信号后多少根日线内统计「最大涨幅 / 期末收益 / 最大回撤」。
# 20 个交易日 ≈ 一个月，既给足「结构是否拉起来」的时间，又不至于把窗口拉太长、
# 让「结局」混入太多无关的后续走势。
OUTCOME_WINDOW_BARS = 20

# ------------------------------------------------------------------
# 体系 v2 · 规则化出场（2026-10-18 体检报告 P0）
#
# 体检结论：旧标签 max_return 优化的是「结构冲没冲高」，不是「拿到手的钱」。
# v2 的标签 = 规则化出场收益（净、扣成本、相对大盘）：
#   1. 信号之后 EXIT_WINDOW_BARS 个交易日内出现**任何一个缠论卖点**
#      （一卖/二卖）→ 以卖点信号价出场——卖点体系本身就是被验证过的出场信号
#      （体检数据：卖点出场 平均+12.65%/胜率73.9% 完胜固定持有 +3.55%/57.3%）。
#   2. 窗口内没出现卖点 → 窗口最后一根收盘价强平（纪律出场，不留侥幸）。
#   3. 净收益扣往返成本 TRADE_COST_ROUNDTRIP（佣金+印花税+过户费，略保守）。
#   4. alpha = 净收益 − 同期上证指数同窗口涨跌（跑赢大盘才算数，剔牛市 beta）。
# 出场窗口比持有窗口长：缠论卖点平均在信号后 10~30 根出现，20 根会砍掉一半出场机会。
EXIT_WINDOW_BARS = 60
# 往返交易成本：佣金万2.5×2 + 印花税千0.5（卖出）+ 过户费万0.1×2 ≈ 0.155%，取 0.16% 略保守。
TRADE_COST_ROUNDTRIP = 0.0016
# alpha 基准指数（fq='ref' 存在 bars 表里）
ALPHA_BENCHMARK = "sh000001"

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
        # 清除「过时信号」：盘中扫描用未收盘的盘中价产生信号，收盘后数据更新后
        # 可能不复现（例如盘中价突破中枢、收盘后回调跌破中枢上沿）。这些残留信号
        # 会让「体系选股（读 signals 表）有信号、详情 K 线图（实时 scan）没有」。
        # 每次 analyze 时清除窗口内不复现的旧信号，保持 signals 表是当前数据的快照。
        self._prune_stale_signals(symbol, bars, result.signals)

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
            return payload

        # 该股自身的历史一买/二买成功率（纯本地查询，几十毫秒，留在主链路）
        try:
            payload["symbol_success_rate"] = self.symbol_success_rate(
                symbol, period=period, bars=bars
            )
        except Exception:  # noqa: BLE001 - 统计失败不该拖垮整个分析
            payload["symbol_success_rate"] = {
                "available": False,
                "symbol": symbol,
                "message": "该股历史成功率暂不可用",
            }
        # 板块 / 同期对比**不在主链路同步取数**：板块反查一次 5.7 秒（1 次产业链 +
        # 最多 3 次板块搜索 × 3 秒间隔），放在这里会让详情页首屏卡 6 秒。
        # 改由 `comparison_payload()` 走独立接口异步补（前端先出图，再填对比条）。
        if self.store.load_stock_sectors(symbol):
            # 已有板块缓存 → 本地就能算完（无网络），直接带上，前端无需二次请求
            try:
                payload["sectors"] = self._sectors_for(symbol)
                payload["comparison"] = self._comparison(symbol, bars, payload["sectors"], period=period)
            except Exception:  # noqa: BLE001
                payload["sectors"] = []
                payload["comparison"] = {"available": False, "message": "同期对比暂不可用"}
        else:
            payload["sectors"] = []
            payload["comparison"] = {"available": False, "lazy": True, "message": "正在加载板块与对比…"}
        # 体系匹配分（自成长体系 M3）：用「当前体系」的增益给日线买点打分。
        # 分数越高越像历史上能拉升的结构。体系尚未构建（没跑过重建）时静默降级，
        # 绝不能因为一个辅助分拖垮主分析。
        try:
            payload["system_match"] = self._system_match_for_signals(symbol, bars, result.signals)
        except Exception:  # noqa: BLE001
            payload["system_match"] = {"available": False, "message": "体系匹配暂不可用"}
        return payload

    def _prune_stale_signals(self, symbol: str, bars: list[Bar], signals: list) -> int:
        """清除 signals 表里「日期在 bars 窗口内、但实时 scan 不复现」的旧信号。

        盘中扫描用未收盘的盘中价产生信号，收盘后数据更新，这些信号可能不复现
        （例如盘中价突破中枢上沿、收盘后回调跌破中枢上沿，三买反而不成立），残留
        在 signals 表成为「过时信号」——体系选股（读 signals 表）有它，详情页
        （实时 scan）没有它，造成不一致。

        每次 analyze 时清除窗口内的过时信号，保持 signals 表是当前数据的权威快照。
        只清窗口内（trade_date >= bars[0].date）：窗口外的历史信号不碰，因为窗口
        裁剪本身就会导致信号不复现，删了会丢历史。
        """

        window_start = bars[0].date
        current = {(s.kind.value, s.date) for s in signals}
        removed = 0
        for hist in self.store.signals_for_symbol(symbol, limit=1000, period=DEFAULT_PERIOD):
            if hist["date"] < window_start:
                continue
            if (hist["kind"], hist["date"]) in current:
                continue
            removed += self.store.delete_signals(symbol, hist["kind"], hist["date"], DEFAULT_PERIOD)
            # 同步删结局样本，体系选股（读 signal_outcomes）也立即干净，不等下次 rebuild
            self.store.delete_outcomes(symbol, hist["kind"], hist["date"], DEFAULT_PERIOD)
        return removed

    def _system_match_for_signals(self, symbol: str, bars: list[Bar], signals: list) -> list[dict]:
        """给**所有**日线买点信号提取特征并用「当前体系」打分，按日期降序返回。

        前端把「当前仍有效的买点」突出显示，其余折叠成「历史」。有效性口径：
        买点之后到最新一根 K 线之间**没有出现任何卖点**——一旦出了一卖/二卖，
        这轮买点行情已经走完（卖点语义就是「持仓风险提示」），必须归入历史，
        否则几个月前的失效买点还顶着「最新」徽章会误导成当前机会。
        """

        dates = [bar.date for bar in bars]
        date_set = set(dates)
        ind = self._indicator_cache(bars)
        # 该股全部信号里，每个买点日期之后是否跟随着卖点（失效判定）
        sell_dates = [s.date for s in signals if s.kind.value in SELL_KIND_VALUES]
        matches: List[dict] = []
        for sig in signals:
            if sig.kind.value not in BUY_KIND_VALUES:
                continue
            sig_dict = {"date": sig.date, "price": sig.price, "meta": sig.meta}
            features = self._extract_features(sig_dict, bars, dates, date_set, ind)
            score = self.match_score(symbol, features)
            is_active = not any(sd > sig.date for sd in sell_dates)
            matches.append(
                {
                    "kind": sig.kind.value,
                    "date": sig.date,
                    "price": sig.price,
                    "features": features,
                    "available": score.get("available", False),
                    "score": score.get("score", 0.0),
                    "hits": score.get("hits", []),
                    "message": score.get("message", ""),
                    "is_active": is_active,
                }
            )
        # 最新在前，前端好切「最新 vs 历史」
        matches.sort(key=lambda m: str(m["date"]), reverse=True)
        return matches

    def comparison_payload(self, symbol: str, period: str = DEFAULT_PERIOD) -> dict:
        """板块 + 同期对比的**独立接口**：详情页先出图，这块异步补。

        为什么要拆：板块反查（`fetch_stock_sectors`）实测 5.7 秒，是详情页最贵的一步，
        而它只是「辅助参考」，不该阻塞 K 线与买卖点的首屏渲染。

        「第一次抓取时把需要的信息都保存下来」在这里体现为：
        - 板块映射 → `stock_sectors` 表；
        - 板块/大盘 K 线 → `bars` 表（`fq='ref'` 标记，与个股 qfq 互不干扰）；
        - 两者都带冷却戳，第二次进来全部命中本地，实测 3ms。
        """

        symbol = symbol.strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        period = normalize_period(period)
        sectors = self._sectors_for(symbol)
        result: dict = {"symbol": symbol, "period": period, "sectors": sectors}
        try:
            bars = self.store.load_bars(symbol, limit=DEFAULT_ANALYSIS_DAYS, period=period)
            result["comparison"] = self._comparison(symbol, bars, sectors, period=period)
        except Exception:  # noqa: BLE001 - 辅助信息失败绝不拖垮页面
            result["comparison"] = {"available": False, "message": "同期对比暂不可用"}
        return result


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

    def interval_nesting(
        self,
        symbol: str,
        anchor_date: Optional[str] = None,
        anchor_kind: Optional[str] = None,
        m30_bars: Optional[List[Bar]] = None,
        period: str = DEFAULT_PERIOD,
    ) -> dict:
        """区间套确认：用 30 分钟同位置结构确认日线买点（缠论「区间套」+ p100 买点定律的泛化）。

        缠论「区间套」= 大级别买点处，向次级别（本系统用 30 分钟）逐层缩小，在日线买点
        的**同位置**找到次级别买点，既确认大级别信号、又精确定位入场。p100 买点定律
        （大级别二买由次级别一买构成）是它在大级别二买上的特例。

        数据现实（实测）：westock 的 m30 只保留约 9-10 个交易日，所以只有落在最近窗口内
        的日线买点才做得了区间套；更早的日线信号 m30 早已无留存。窗口外的日线买点
        `available=True` 但 `confirmed=False` 并如实说明——**绝不**用无关远端信号假确认。

        参数
        ----
        anchor_date: 要确认的日线买点信号日（YYYY-MM-DD）。省略则从本地日线信号取最近一次买点。
        anchor_kind: 该买点类型（FIRST_BUY/SECOND_BUY/THIRD_BUY），用于结论措辞；省略则推断。
        m30_bars: 直接传入 30 分钟 K 线（测试用）；省略则从本地缓存/远端拉取并落本地。
        """

        symbol = symbol.strip()
        if not symbol:
            raise ValueError("symbol 不能为空")

        # 1) 解析要确认的日线买点（anchor）
        if anchor_date is None:
            rows = self.store.signals_for_symbol(symbol, limit=50, period=DEFAULT_PERIOD)
            day_buys = [r for r in rows if r["kind"] in BUY_KIND_VALUES]
            if not day_buys:
                return {
                    "available": False,
                    "symbol": symbol,
                    "message": "本地暂无该股日线买点缓存，请先按日线分析一次或执行市场扫描",
                }
            anchor = max(day_buys, key=lambda r: (str(r["date"]), r["bar_index"]))
            anchor_date = str(anchor["date"])[:10]
            anchor_kind = anchor["kind"]

        # 2) 取 30 分钟 K 线（优先本地缓存，不足则远端拉取并落本地）
        if m30_bars is None:
            cached = self.store.load_bars(symbol, limit=period_max_bars("m30"), period="m30")
            m30_bars = cached
            if len(m30_bars) < MIN_BARS:
                try:
                    m30_bars = fetch_bars(symbol, period="m30", days=int(PERIODS["m30"]["span_days"]))
                    self.store.save_bars(m30_bars)
                except MarketDataError:
                    m30_bars = cached
        if len(m30_bars) < MIN_BARS:
            return {
                "available": False,
                "symbol": symbol,
                "anchor_date": anchor_date,
                "anchor_kind": anchor_kind,
                "message": "本地无 30 分钟数据，区间套验证暂不可用",
            }

        m30_sorted = sorted(m30_bars, key=lambda b: b.date)
        window_start = m30_sorted[0].date[:10]
        window_end = m30_sorted[-1].date[:10]
        m30_days = {b.date[:10] for b in m30_bars}

        # 3) m30 数据窗口是否覆盖日线买点日（做不到就如实说明，不假确认）
        if anchor_date not in m30_days:
            engine = ChanEngine(self.config)
            structure = self._m30_structure(m30_bars, engine.scan(m30_bars))
            return {
                "available": True,
                "symbol": symbol,
                "anchor_date": anchor_date,
                "anchor_kind": anchor_kind,
                "m30_period": "m30",
                "m30_window": {"start": window_start, "end": window_end},
                "confirmed": False,
                "matched": None,
                "m30_buys": [],
                "m30_structure": structure,
                "message": (
                    f"日线买点日 {anchor_date} 不在 30 分钟数据窗口"
                    f"（仅保留 {window_start} ~ {window_end}，约 10 个交易日），无法做区间套"
                ),
            }

        # 4) 在 m30 上跑缠论，找同位置的买点（日历日落在 anchor 附近窗口内）
        engine = ChanEngine(self.config)
        result = engine.scan(m30_bars)
        m30_buy_signals = [s for s in result.signals if s.kind.value in BUY_KIND_VALUES]
        candidates = []
        for s in m30_buy_signals:
            d = str(s.date)[:10]
            try:
                gap = abs((date.fromisoformat(d) - date.fromisoformat(anchor_date)).days)
            except ValueError:
                gap = 999
            if gap <= INTERVAL_NESTING_WINDOW_DAYS:
                candidates.append((gap, s))
        # 优先离 anchor 最近；同日时优先一买（区间套常以次级别一买确认大级别买点）
        candidates.sort(
            key=lambda pair: (pair[0], 0 if pair[1].kind.value == "FIRST_BUY" else 1)
        )
        matched = candidates[0][1] if candidates else None

        return {
            "available": True,
            "symbol": symbol,
            "anchor_date": anchor_date,
            "anchor_kind": anchor_kind,
            "m30_period": "m30",
            "m30_window": {"start": window_start, "end": window_end},
            "confirmed": matched is not None,
            "matched": (
                {
                    "kind": matched.kind.value,
                    "kind_label": KIND_LABELS.get(matched.kind.value, matched.kind.value),
                    "date": str(matched.date),
                    "price": matched.price,
                    "anchor_price": matched.anchor_price,
                }
                if matched
                else None
            ),
            "m30_buys": [
                {"kind": s.kind.value, "date": str(s.date), "price": s.price}
                for s in sorted(m30_buy_signals, key=lambda s: s.date)
            ],
            "m30_structure": self._m30_structure(m30_bars, result),
            "message": self._interval_nesting_message(anchor_date, anchor_kind, matched, window_start, window_end),
        }

    @staticmethod
    def _m30_structure(m30_bars: List[Bar], result=None) -> dict:
        """30 分钟级别的缩略结构（供前端展示「同位置」的次级别面貌）。"""

        if result is None:
            result = ChanEngine(ChanConfig()).scan(m30_bars)
        buys = [s for s in result.signals if s.kind.value in BUY_KIND_VALUES]
        return {
            "bars": len(m30_bars),
            "zhongshu": len(result.zhongshu),
            "signals": len(result.signals),
            "buys": len(buys),
        }

    @staticmethod
    def _interval_nesting_message(
        anchor_date: str, anchor_kind: Optional[str], matched, window_start: str, window_end: str
    ) -> str:
        ak = KIND_LABELS.get(anchor_kind, anchor_kind or "买点")
        if matched is None:
            return (
                f"日线{ak}（{anchor_date}）未在 30 分钟同位置（±{INTERVAL_NESTING_WINDOW_DAYS} 日）"
                f"找到对应买点，区间套未确认；m30 窗口 {window_start} ~ {window_end}"
            )
        mk = KIND_LABELS.get(matched.kind.value, matched.kind.value)
        return (
            f"区间套确认：日线{ak}（{anchor_date}）→ 30 分钟于 {matched.date} 出现{mk}"
            f"（{matched.price:.2f}），可据此精确定位入场"
        )

    def _load_bars(self, symbol: str, days: int, use_cache: bool, period: str = DEFAULT_PERIOD) -> list[Bar]:
        """加载 K 线：**本地优先，没有（或过期）才联网，抓一次就落库**。

        这是详情页速度的第一道关卡。原实现的日线判断是
        `cached[-1].date == today`——周末、节假日、以及当天数据已更新之后，
        这个等式永远不成立，于是**每次进详情都联网重抓 400 根**（实测 514ms）。
        现在改为三重判断，按序短路：

        1. 缓存新鲜（最后一根 >= 本地已知最新交易日）→ 直接用，零联网；
        2. 今天已经抓过这只（冷却戳内）→ 用缓存，不再重复抓；
        3. 以上都不满足才联网，抓完落库 + 记戳。

        第 1 条用「本地全库最近交易日」而不是 `today`：全市场扫描/日线抓取每天都在
        推进这个日期，它才是「已知数据边界」。停牌股永远追不上，由第 2 条兜住。
        """

        limit = period_max_bars(period)
        cached = self.store.load_bars(symbol, limit=limit, period=period) if use_cache else []
        cooldown = (
            BARS_COOLDOWN_SECONDS if period == DEFAULT_PERIOD else MINUTE_BARS_COOLDOWN_SECONDS
        )
        if cached and self._bars_fresh(cached, period):
            return cached[-days:] if period == DEFAULT_PERIOD else cached
        if cached and self.store.fetched_within("bars", f"{symbol}:{period}", cooldown):
            # 冷却期内刚抓过还是不够新（停牌 / 数据源未更新）→ 别再抓，直接用缓存
            return cached[-days:] if period == DEFAULT_PERIOD else cached
        try:
            fresh = fetch_bars(symbol, period=period, days=days)
        except MarketDataError:
            if cached:
                self.store.mark_fetch("bars", f"{symbol}:{period}")  # 失败也记戳，避免反复重试
                return cached
            raise
        self.store.save_bars(fresh, period=period)
        self.store.mark_fetch("bars", f"{symbol}:{period}")
        return fresh[-limit:] if period != DEFAULT_PERIOD else fresh[-days:]

    def _bars_fresh(self, cached: list[Bar], period: str = DEFAULT_PERIOD) -> bool:
        """本地缓存的最后一根是否已经覆盖「已知最新交易日」。"""

        if not cached:
            return False
        latest_cached = cached[-1].date
        if period != DEFAULT_PERIOD:
            # 分钟 K 带时间戳且盘中持续变化，不能按「交易日」判新鲜；
            # 一律交给上面的短冷却（默认 5 分钟）决定是否重抓。
            return False
        known = self.store.recent_trading_dates(1, period=DEFAULT_PERIOD)
        if known:
            return latest_cached >= known[0]
        # 库里没有任何日线（新库）→ 只能按自然日判断
        return latest_cached >= date.today().isoformat()


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

    # ------------------------------------------------------------------
    # 个股所属板块 + 同期个股 vs 板块 vs 大盘 力度对比
    #
    # 这套是「辅助参考」，不改变任何选股/买点判定（买点判定仍只看缠论原文之道）。
    # 板块与指数 K 线经由 provider 取数，存在 bars 表里用 SECTOR_REF_FQ 标记，
    # 与个股 qfq 数据互不干扰。任何一步取数失败都静默降级——辅助信息不能拖垮主分析。
    # ------------------------------------------------------------------

    def _sectors_for(self, symbol: str) -> list[dict]:
        """个股所属板块：优先用库内缓存，没有才联网反查。

        反查极贵（1 次产业链 + 最多 3 次板块搜索，间隔 3 秒，实测 **5.7 秒**），
        所以：命中缓存直接返回；**查空或失败也记冷却戳**，冷却期内不再重试——
        否则每点一次这只票就要等 5.7 秒，而结果还是空。
        """

        cached = self.store.load_stock_sectors(symbol)
        if cached:
            return cached
        if self.store.fetched_within("sectors", symbol, SECTOR_MISS_COOLDOWN_SECONDS):
            return []  # 冷却期内已知查不到，不再联网
        try:
            fetched = fetch_stock_sectors(symbol)
        except MarketDataError:
            self.store.mark_fetch("sectors", symbol)
            return []
        # 无论抓到与否都记戳：抓空也说明「这个代码没有板块映射」，重试同样白等
        self.store.mark_fetch("sectors", symbol)
        if fetched:
            self.store.save_stock_sectors(symbol, fetched)
            return self.store.load_stock_sectors(symbol)
        return []


    def _reference_bars(self, code: str, period: str = DEFAULT_PERIOD) -> list[Bar]:
        """板块 / 指数 K 线（带缓存 + 失败冷却）。取数失败返回空列表。

        板块/指数 K 线首次抓取实测 ~560ms，失败时若每次都重试会拖慢对比条；
        用冷却戳保证冷却期内只试一次。
        """

        cached = self.store.load_bars(code, fq=SECTOR_REF_FQ, limit=DEFAULT_ANALYSIS_DAYS, period=period)
        if cached:
            return cached
        stamp_key = f"{code}:{period}"
        if self.store.fetched_within("ref_bars", stamp_key, BARS_COOLDOWN_SECONDS):
            return []
        try:
            fresh = fetch_bars(code, period=period, days=DEFAULT_ANALYSIS_DAYS)
        except MarketDataError:
            self.store.mark_fetch("ref_bars", stamp_key)
            return []
        self.store.mark_fetch("ref_bars", stamp_key)
        self.store.save_bars(fresh, fq=SECTOR_REF_FQ, period=period)
        return fresh


    @staticmethod
    def _comparison_metrics(closes_by_date: Dict[str, float], dates: list[str]) -> dict:
        """对一段对齐后的收盘价序列，算涨跌幅与归一化力度。

        归一化口径（关键，做错会误导）：板块点位 36000、个股 1300，MACD 面积
        直接比没意义。先把价格归一到「起点 = 100」，再算 MACD 面积，三者在
        同一量纲下才可比。涨跌幅同样用起点基准。
        """

        closes = [closes_by_date[d] for d in dates]
        base = closes[0] or 1.0
        change_pct = (closes[-1] - closes[0]) / base * 100.0
        norm = [c / base * 100.0 for c in closes]
        macd = macd_state(norm)
        direction = "up" if closes[-1] >= closes[0] else "down"
        effort = measure_effort(macd, 0, len(norm) - 1, direction, closes=norm)
        return {
            "change_pct": round(change_pct, 2),
            "bars": len(closes),
            "effort": round(effort.macd_area, 4),
            "effort_per_bar": round(effort.average_effort, 4),
        }

    def _comparison(self, symbol: str, stock_bars: list[Bar], sectors: list[dict], period: str = DEFAULT_PERIOD) -> dict:
        """同期个股 vs 板块 vs 大盘 力度对比。

        时间窗以个股 K 线的起止为准，三者取交集对齐；板块/大盘缺失的交易日跳过。
        分钟级别不参与（没有板块/指数的分钟数据意义），直接返回 unavailable。
        """

        if period != DEFAULT_PERIOD:
            return {"available": False, "period": period, "message": "同期对比仅日线口径"}
        if not stock_bars:
            return {"available": False, "message": "无个股K线"}

        stock_map = {bar.date: bar.close for bar in stock_bars}
        sector = sectors[0] if sectors else None
        sector_bars = self._reference_bars(sector["code"]) if sector else []
        sector_map = {bar.date: bar.close for bar in sector_bars}
        market_bars = self._reference_bars(MARKET_INDEX_CODE)
        market_map = {bar.date: bar.close for bar in market_bars}

        dates = sorted(set(stock_map))
        if sector_map:
            dates = [d for d in dates if d in sector_map]
        if market_map:
            dates = [d for d in dates if d in market_map]
        if len(dates) < COMPARISON_MIN_BARS:
            return {
                "available": False,
                "message": f"共同交易日仅 {len(dates)} 天，不足以对比",
            }

        payload = {
            "available": True,
            "window": {
                "start": dates[0],
                "end": dates[-1],
                "bars": len(dates),
            },
            "sector": {
                "code": sector["code"],
                "name": sector["name"],
                "level": sector["level"],
            }
            if sector
            else None,
            "market_name": MARKET_INDEX_NAME,
            "stock": self._comparison_metrics(stock_map, dates),
        }
        if sector_map:
            payload["sector_metrics"] = self._comparison_metrics(sector_map, dates)
        if market_map:
            payload["market"] = self._comparison_metrics(market_map, dates)
        # 相对强弱：个股涨跌幅 - 参照涨跌幅（百分点）
        if sector_map:
            payload["vs_sector"] = round(payload["stock"]["change_pct"] - payload["sector_metrics"]["change_pct"], 2)
        if market_map:
            payload["vs_market"] = round(payload["stock"]["change_pct"] - payload["market"]["change_pct"], 2)
        return payload

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

    def incremental_scan_targets(self, scope: str = "all") -> dict:
        """增量扫描的目标清单：只挑「本地日线落后于最新交易日」的标的。

        为什么值得做：全量扫描 30–50 分钟里，绝大多数标的的前 399 根 K 线完全没变，
        只是多了 1 根；但重扫要重算整条序列（分型/笔/中枢/背驰全都是全量的）。
        这里只筛出真正需要更新的标的，让人能快速跑一次当日增量。

        判定：该标的本地最后一根日线日期 < 全库最新交易日 → 需要更新。
        本地完全没有数据的也一并纳入（首次补齐）。
        """

        key = normalize_scope(scope)
        try:
            universe, _stats = fetch_universe_symbols(key)
        except MarketDataError:
            universe = [(row["symbol"], row["name"]) for row in self.store.all_symbols()]
        latest_local = self.store.recent_trading_dates(1, period=DEFAULT_PERIOD)
        latest = latest_local[0] if latest_local else ""
        last_dates = self.store.last_bar_dates(period=DEFAULT_PERIOD)
        targets = [
            (symbol, name)
            for symbol, name in universe
            if last_dates.get(symbol, "") < latest
        ]
        return {
            "scope": key,
            "latest_trade_date": latest,
            "universe": len(universe),
            "targets": targets,
            "count": len(targets),
            "skipped": len(universe) - len(targets),
            "message": (
                f"最新交易日 {latest}：{len(targets)} 只需更新，"
                f"{len(universe) - len(targets)} 只已是最新可跳过"
                if targets
                else f"全部 {len(universe)} 只的日线都已是 {latest}，无需增量"
            ),
        }

    def start_incremental_scan(self, scope: str = "all") -> dict:
        """增量扫描：只扫需要更新的标的。没有可扫的就返回明确提示而不是空跑 30 分钟。"""

        plan = self.incremental_scan_targets(scope)
        if not plan["count"]:
            return {
                "run_id": None,
                "started": False,
                "count": 0,
                "message": plan["message"],
            }
        target = plan["targets"]
        with self._thread_lock:
            for thread in self._threads.values():
                if thread.is_alive():
                    raise RuntimeError("已有一个扫描任务在运行，请等待完成后再启动")
        run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        for symbol, name in target:
            self.store.upsert_symbol(symbol, name)
        self.store.create_run(run_id, scope=plan["scope"], total=len(target))
        thread = threading.Thread(target=self._run_market_scan, args=(run_id, target, False), daemon=True)
        with self._thread_lock:
            self._threads[run_id] = thread
        thread.start()
        return {
            "run_id": run_id,
            "started": True,
            "count": len(target),
            "scope": plan["scope"],
            "message": f"增量扫描已启动，共 {len(target)} 只（跳过 {plan['skipped']} 只已是最新）",
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

    def candidates(self, recent_days: int = 30, period: str = DEFAULT_PERIOD) -> dict:
        """市场扫描候选：与「三日买卖点池」**完全同源**（signals 表），只是窗口更长。

        为什么不再读 scan_hits（2026-10-17 口径统一）：
        1. `scan_hits` 的唯一键是 `(run_id, symbol, kind)`，每只股票每类买点只存下
           **一条**（后写的覆盖先写的），历史买点全被吃掉。实测同一 20 交易日窗口：
           signals 表一买 1404 条 vs scan_hits 只有 145 条，差近 10 倍。
        2. `scan_hits` 是扫描任务的冻结快照，不随 `_prune_stale_signals` 清过时信号同步，
           会出现「市场扫描里有、点进详情却找不到」。
        3. 窗口用自然日（30 天）与 pools 的交易日（3 天）语义不同，横向对比必然对不上。

        现在两条口径只差窗口长度：`candidates(days=N)` 与 `pools(days=N)` 数字完全一致
        （同一套 `buy_signals_since` + `_group_recent`）。`last_run` 仅作扫描任务状态展示。
        """

        period = normalize_period(period)
        trading_dates = self.store.recent_trading_dates(recent_days, period)
        cutoff = trading_dates[-1] if trading_dates else ""
        rows = self.store.buy_signals_since(cutoff, period=period) if cutoff else []
        grouped = self._group_recent(rows, BUY_KINDS)
        # 与 pools 完全相同的两个挂载：胜率徽章 + 体系分。
        # 候选表也要体系分——前端 `/api/market/candidates` 与 `/api/market/pools`
        # 用的是同一个渲染函数，列头一致才不会错位。
        self._attach_pool_rates(grouped, period)
        self._attach_pool_scores(grouped, cutoff, period)

        run = self.store.latest_run()
        scope = (run.get("scope") if run else None) or "all"
        payload = {
            "last_run": None,
            "cutoff": cutoff,
            "recent_days": recent_days,
            "period": period,
            "trading_dates": list(reversed(trading_dates)),
            "source": "signals",
            "dedup": "same_symbol_kind_latest",
            "scope": scope,
            "scope_label": SCOPE_LABELS.get(scope, scope),
            "first_buy": grouped[SignalKind.FIRST_BUY.value],
            "second_buy": grouped[SignalKind.SECOND_BUY.value],
            "third_buy": grouped[SignalKind.THIRD_BUY.value],
        }
        if run:
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
        trading_dates = self.store.recent_trading_dates(days, period)
        cutoff = trading_dates[-1] if trading_dates else ""
        rows = self.store.buy_signals_since(cutoff, period=period) if cutoff else []
        grouped = self._group_recent(rows, BUY_KINDS)
        # 胜算徽章 + 体系分（命中特征系数）都挂在同一列。
        # 注意：`_group_recent` 已按 (symbol, kind) 只保留**最新一条**，
        # 所以每只股票在每张池表里只有一行，分数就是它最新买点的体系分。
        self._attach_pool_rates(grouped, period)
        self._attach_pool_scores(grouped, cutoff, period)
        return {
            "days": days,
            "period": period,
            "cutoff": cutoff,
            "trading_dates": list(reversed(trading_dates)),
            "first_buy": grouped[SignalKind.FIRST_BUY.value],
            "second_buy": grouped[SignalKind.SECOND_BUY.value],
            "third_buy": grouped[SignalKind.THIRD_BUY.value],
        }

    def _attach_pool_scores(self, grouped: dict, cutoff: str, period: str) -> None:
        """给买卖点池每一行挂上「体系分」+ **止损价** + **期望出场**（体检 P0）。

        体系分：直接读 `signal_outcomes` 的特征快照，与「体系选股」同一引擎同一快照。

        止损价（stop_price）：用信号的 anchor_price（结构关键低点）——一买的锚是
        背驰低点、二买是回调低点、三买是中枢上沿。跌破锚 = 该买点的结构前提失效，
        这正是缠论原文的止损逻辑（买点不成立就离场），比任意百分比止损有依据。

        期望出场（expected_exit）：用当前体系分档的历史出场收益中位数——高分档
        历史上按卖点出场能拿多少，就是这个价位的期望含义。分档阈值与体检一致
        （<0 / 0~0.15 / 0.15~0.3 / ≥0.3），样本 <30 的档不给数（防小样本噪声）。
        """

        snapshot = self.store.latest_snapshot()
        gain = (snapshot or {}).get("gain") or {}
        feature_index: dict[tuple, dict] = {}
        outcome_index: dict[tuple, dict] = {}
        if gain and cutoff:
            # 一次取窗口内全部样本建索引，禁止逐行查库（池子几百行 × 一次查询）
            for outcome in self.store.recent_outcomes(cutoff, period=period):
                key = (outcome["symbol"], outcome["kind"], outcome["signal_date"])
                feature_index[key] = outcome["features"]
                outcome_index[key] = outcome

        # 各体系分档的历史出场收益中位数（从全量样本统计，一次查询）
        exit_medians = self._exit_medians_by_score(period=period)

        for rows in grouped.values():
            for row in rows:
                key = (row["symbol"], row["kind"], str(row.get("date")))
                features = feature_index.get(key)
                # 止损价 = 结构锚点（任何买点都带 anchor_price）
                anchor = row.get("anchor_price")
                row["stop_price"] = round(float(anchor), 2) if anchor else None
                if not gain or features is None:
                    row["system_score"] = None
                    row["system_hits"] = []
                    row["system_available"] = False
                    # 排序标量：空值恒排最后
                    row["system_score_value"] = -999.0
                    row["expected_exit"] = None
                    continue
                score, hits = score_signal(features, gain)
                row["system_score"] = score
                row["system_hits"] = hits
                row["system_available"] = True
                row["system_score_value"] = score
                # 期望出场：体系分档 → 该档历史出场收益中位数
                outcome = outcome_index.get(key) or {}
                row["expected_exit"] = self._expected_exit_for(score, exit_medians)
                # 该样本自身的规则化出场口径也带上（大多数与档位中位数一致，
                # 但样本里已有出场结果的可以直接展示真实历史）
                row["exit_kind_hint"] = outcome.get("exit_kind")

    def _exit_medians_by_score(self, period: str) -> dict:
        """按体系分档统计历史出场收益中位数（+胜率），一次全量查询。

        分档阈值与体检报告一致：<0 / 0~0.15 / 0.15~0.3 / ≥0.3。
        每档样本 <30 不给数（防小样本噪声）——期望出场显示「—」。
        """

        snapshot = self.store.latest_snapshot()
        gain = (snapshot or {}).get("gain") or {}
        if not gain:
            return {}
        buckets: Dict[str, list] = {"<0": [], "0-0.15": [], "0.15-0.3": [], ">=0.3": []}
        for outcome in self.store.load_all_outcomes(period=period):
            if outcome.get("net_exit_return") is None:
                continue
            score, _ = score_signal(outcome.get("features", {}), gain)
            b = "<0" if score < 0 else "0-0.15" if score < 0.15 else "0.15-0.3" if score < 0.3 else ">=0.3"
            buckets[b].append((score, outcome["net_exit_return"]))
        stats: Dict[str, dict] = {}
        for b, items in buckets.items():
            if len(items) < 30:
                continue
            exits = sorted(x[1] for x in items)
            mid = exits[len(exits) // 2]
            win = sum(1 for x in exits if x > 0)
            stats[b] = {
                "n": len(items),
                "median": round(mid, 4),
                "win_rate": round(win / len(items), 4),
            }
        return stats

    @staticmethod
    def _expected_exit_for(score: float, medians: dict) -> Optional[dict]:
        """按分数取对应档位的历史出场统计。"""

        b = "<0" if score < 0 else "0-0.15" if score < 0.15 else "0.15-0.3" if score < 0.3 else ">=0.3"
        return medians.get(b)

    def risk_pool(self, days: int = 3, period: str = DEFAULT_PERIOD) -> dict:
        """N 日卖点风险池：最近 N 个交易日内触发的一卖 / 二卖。

        与买点池是并列关系而非同一张表：买点用于选股，卖点用于持仓风险提示。
        """

        period = normalize_period(period)
        trading_dates = self.store.recent_trading_dates(days)
        cutoff = trading_dates[-1] if trading_dates else ""
        rows = self.store.sell_signals_since(cutoff, period=period) if cutoff else []
        grouped = self._group_recent(rows, SELL_KINDS)
        self._attach_pool_rates(grouped, period)
        # 只返回卖点键：grouped 按 SELL_KINDS 初始化，响应里混买点键必然 KeyError
        # （批次8 从 pools() 复制响应时引入的真实 500，前端也只消费 first/second_sell）。
        return {
            "days": days,
            "period": period,
            "cutoff": cutoff,
            "trading_dates": list(reversed(trading_dates)),
            "first_sell": grouped[SignalKind.FIRST_SELL.value],
            "second_sell": grouped[SignalKind.SECOND_SELL.value],
        }

    def _attach_pool_rates(self, grouped: dict, period: str) -> None:
        """给买卖点池的每一行挂上「胜算」徽章。

        混合口径（用户确认）：
        - 一买池看 `first_buy` 胜率、二买池看 `second_buy` 胜率；
        - 个股已终结样本 ≥ 3 → 显示个股百分比，标 `source='symbol'`；
        - 否则退回 `watchlist_stats()` 的全市场基准，标 `source='market'`，
          前端用灰色 + 提示说明「个股样本不足」；
        - 三买 / 卖点没有成功率口径，给 `source='none'`（前端显示「—」）。
        """

        symbols = {row["symbol"] for rows in grouped.values() for row in rows}
        rates = self.symbol_success_rates(symbols, period=period)
        baseline = self.watchlist_stats()
        for kind_key, rows in grouped.items():
            stat_key = {
                SignalKind.FIRST_BUY.value: "first_buy",
                SignalKind.SECOND_BUY.value: "second_buy",
            }.get(kind_key)
            for row in rows:
                badge = self._rate_badge(rates.get(row["symbol"], {}), stat_key, baseline)
                row["rate"] = badge
                # 排序用标量：徽章里的 text 是字符串（含 %/汉字），不能直接比；
                # 数字 value（-1 表示无口径/样本不足）空值恒排最后。
                row["rate_value"] = badge.get("value", -1)

    @staticmethod
    def _rate_badge(rate_payload: dict, stat_key: Optional[str], baseline: dict) -> dict:
        """把个股成功率 / 全市场基准折成一个前端可直接渲染的徽章。

        返回 `{text, detail, value, source}`：
        - `value` 是数值（用于排序，空值恒排最后用 -1）；
        - `source` ∈ {symbol, market, none} 决定配色与 tooltip。
        """

        if not stat_key:
            return {"text": "—", "detail": "", "value": -1, "source": "none"}
        stat = (rate_payload.get(stat_key) or {}) if rate_payload.get("available") else {}
        if stat.get("closed", 0) >= SYMBOL_RATE_MIN_SAMPLE and stat.get("rate") is not None:
            value = stat["rate"]
            return {
                "text": _pct(stat["success"], stat["closed"]),
                "detail": f'个股 {stat["success"]}/{stat["closed"]}',
                "value": value,
                "source": "symbol",
            }
        # 个股样本不足 → 退回全市场基准
        base_stat = (baseline.get(stat_key) or {})
        base_rate = base_stat.get("rate")
        base_closed = base_stat.get("closed", 0)
        if base_rate is not None and base_closed > 0:
            return {
                "text": _pct(round(base_rate * base_closed), base_closed),
                "detail": f"市场基准 {base_stat.get('closed', 0)} 次（个股样本不足）",
                "value": base_rate,
                "source": "market",
            }
        return {"text": "样本不足", "detail": "个股与全市场样本均不足", "value": -1, "source": "market"}

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

    def symbol_success_rate(
        self, symbol: str, period: str = DEFAULT_PERIOD, bars: Optional[List[Bar]] = None
    ) -> dict:
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

        `bars`：调用方若已加载过该股日线，直接传进来可省掉一次 400 根的查询
        （详情页 analyze 路径就是这样复用的）。
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

        # 二买的结局要查真实 K 线（收盘是否跌破二买低点），信号表里没有价格序列；
        # 一买↔二买的间隔也要换算成交易日序，不能直接相减 signals.bar_index
        if bars is None:
            bars = self.store.load_bars(symbol, limit=DEFAULT_ANALYSIS_DAYS, period=period)
        closes_by_date = {bar.date: bar.close for bar in bars}
        return self._success_rate_payload(
            symbol, first_buys, second_buys, closes_by_date, period
        )

    def symbol_success_rates(self, symbols: Iterable[str], period: str = DEFAULT_PERIOD) -> Dict[str, dict]:
        """批量版 `symbol_success_rate`：一次请求给一整池标的算胜率。

        判定逻辑与单只版本**完全共用** `_success_rate_payload` / `_judge_*`，
        只是把取数从「逐只查」换成「`symbol IN (...)` 批量查」——
        实测 241 只：逐只 6206 ms → 批量 62 ms（+K线 100 ms）。

        买卖点池要在一页里给每行显示胜算，不批量化这个接口会慢到不可用。
        """

        period = normalize_period(period)
        unique = sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()})
        if not unique:
            return {}
        # 一次性批量取名字（N+1 的教训）：先拿到名字再传入各判定，
        # 避免 `_success_rate_payload` / 无信号分支里逐只调 `symbol_name`。
        names = self.store.symbol_names(unique)
        by_symbol = self.store.signals_for_symbols(unique, limit=1000, period=period)
        close_series = self.store.load_close_series(unique, period=period)

        payloads: Dict[str, dict] = {}
        for symbol in unique:
            rows = by_symbol.get(symbol, [])
            first_buys = [r for r in rows if r["kind"] == SignalKind.FIRST_BUY.value]
            second_buys = [r for r in rows if r["kind"] == SignalKind.SECOND_BUY.value]
            name = names.get(symbol, symbol)
            if not first_buys and not second_buys:
                payloads[symbol] = {
                    "available": False,
                    "symbol": symbol,
                    "period": period,
                    "period_label": PERIOD_LABELS.get(period, period),
                    "name": name,
                    "message": "本地暂无该股历史信号，首次分析或扫描后才会积累",
                }
                continue
            payloads[symbol] = self._success_rate_payload(
                symbol, first_buys, second_buys, dict(close_series.get(symbol, [])), period, name=name
            )
        return payloads

    def _success_rate_payload(
        self,
        symbol: str,
        first_buys: list[dict],
        second_buys: list[dict],
        closes_by_date: Dict[str, float],
        period: str,
        name: Optional[str] = None,
    ) -> dict:
        """个股成功率的核心计算，单只与批量两条路径共用。

        共用是硬要求：分两套实现必然漂移，最后同一只票在两个接口里给出不同百分比。
        """

        # 按 bar_index 升序：bar_index 是 K 线窗口内的相对位置，
        # 同一只票的多次扫描会有偏移，用 trade_date 兜底排序更稳。
        first_buys = sorted(first_buys, key=lambda r: (str(r["date"]), r["bar_index"]))
        second_buys = sorted(second_buys, key=lambda r: (str(r["date"]), r["bar_index"]))
        bar_pos = {date: index for index, date in enumerate(sorted(closes_by_date))}

        first_detail = [self._judge_first_buy(fb, second_buys, bar_pos) for fb in first_buys]
        second_detail = [self._judge_second_buy(sb, closes_by_date) for sb in second_buys]

        return {
            "available": True,
            "symbol": symbol,
            "name": name or self.store.symbol_name(symbol),
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

    def _judge_second_buy(self, second_buy: dict, closes_by_date: Dict[str, float]) -> dict:
        """一次二买的结局：之后有没有跌破二买低点。

        判据与 `WatchlistManager` 的二买失败转移保持一致：收盘跌破该次二买的
        回调低点（`anchor_price`）即失败，窗口同样是 `max_wait_bars`。

        必须真的去查 K 线——不能用「后面还有没有信号」来近似，那既不准确，
        也会把「还没走出窗口」误判成失败。

        入参改成 `{date: close}` 而非 Bar 对象：批量路径下只取了收盘价，
        用字典既省内存也省「构造 Bar」的开销；逻辑完全等价。
        """

        sb_date = str(second_buy["date"])
        low = second_buy["anchor_price"]
        later = sorted(date for date in closes_by_date if date > sb_date)
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
        broke = next((date for date in window if closes_by_date[date] < low), None)
        if broke is None:
            return {**base, "success": True, "result": "held", "result_label": "守住二买低点"}
        return {
            **base,
            "success": False,
            "result": "broke_low",
            "result_label": f"{broke} 跌破二买低点",
            "break_date": broke,
            "break_close": closes_by_date[broke],
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
        second_ok = states.get(WatchState.SECOND_BUY_OK.value, 0)
        second_failed = states.get(WatchState.SECOND_BUY_FAILED.value, 0)
        first_failed = states.get(WatchState.FAILED.value, 0)
        weak = states.get(WatchState.WEAK_TRACK.value, 0)
        pending = states.get(WatchState.WAIT_SECOND_BUY.value, 0)

        # 一买成功 = 走到二买确认（含之后守住或失败两种结局）
        first_success = opportunity + second_ok + second_failed
        first_closed = first_success + first_failed + weak
        # 二买成功 = 考验期满守住（SECOND_BUY_OK）+ 仍在考验期内未破（OPPORTUNITY）
        second_success = opportunity + second_ok
        second_confirmed = second_success + second_failed

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
                "success": second_success,
                "failed": second_failed,
                "confirmed": second_confirmed,
                "ok": second_ok,
                "rate": _rate(second_success, second_confirmed),
                "rate_label": _pct(second_success, second_confirmed),
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

    # ------------------------------------------------------------------
    # 自选 / 持仓
    # ------------------------------------------------------------------

    def favorites(self) -> dict:
        """自选列表：带上名称、最新收盘、成本价浮盈，以及当前是否有卖点风险。

        为什么需要它：卖点的语义是「**持仓**风险提示」，但 `risk_pool` 是全市场的
        近三日卖点池，用户无法从中回答「我手上这几只今天有没有卖点」。
        这张表把卖点落到自选股上，卖点功能才真正可用。
        """

        rows = self.store.list_favorites()
        symbols = [row["symbol"] for row in rows]
        names = self.store.symbol_names(symbols)
        # 一段时间的卖点（默认近 3 日）用于风险提示；不是全市场池，只留自选相关
        risk = self.risk_pool(days=3)
        risk_by_symbol: Dict[str, list[dict]] = {}
        for key in ("first_sell", "second_sell"):
            for item in risk.get(key, []):
                risk_by_symbol.setdefault(item["symbol"], []).append(item)

        items: List[dict] = []
        for row in rows:
            symbol = row["symbol"]
            bars = self.store.load_bars(symbol, limit=DEFAULT_ANALYSIS_DAYS, period=DEFAULT_PERIOD)
            last_close = bars[-1].close if bars else None
            last_date = bars[-1].date if bars else None
            cost = row.get("cost_price")
            pnl_pct = None
            if cost and last_close and cost > 0:
                pnl_pct = round((last_close - cost) / cost * 100.0, 2)
            sells = risk_by_symbol.get(symbol, [])
            items.append(
                {
                    "symbol": symbol,
                    "name": names.get(symbol, symbol),
                    "note": row.get("note") or "",
                    "cost_price": cost,
                    "last_close": last_close,
                    "last_date": last_date,
                    "pnl_pct": pnl_pct,
                    "risk_signals": sells,
                    "risk_count": len(sells),
                }
            )
        risk_total = sum(1 for item in items if item["risk_count"])
        return {
            "count": len(items),
            "risk_count": risk_total,
            "items": items,
            "message": (
                f"自选 {len(items)} 只，其中 {risk_total} 只近 3 个交易日出现卖点（持仓风险提示）"
                if items
                else "自选为空：在个股查询页点「加入自选」即可"
            ),
        }

    def add_favorite(
        self, symbol: str, note: str = "", cost_price: Optional[float] = None
    ) -> dict:
        symbol = self._require_symbol(symbol)
        self.store.upsert_favorite(symbol, note=note, cost_price=cost_price)
        return {"symbol": symbol, "added": True, "message": f"{symbol} 已加入自选"}

    def remove_favorite(self, symbol: str) -> dict:
        symbol = self._require_symbol(symbol)
        removed = self.store.delete_favorite(symbol)
        return {
            "symbol": symbol,
            "removed": removed,
            "message": f"{symbol} 已移出自选" if removed else f"{symbol} 本来就不在自选里",
        }

    @staticmethod
    def _require_symbol(symbol: str) -> str:
        cleaned = (symbol or "").strip()
        if not cleaned:
            raise ValueError("symbol 不能为空")
        return cleaned

    # ------------------------------------------------------------------
    # 批量区间套
    # ------------------------------------------------------------------

    def batch_interval_nesting(
        self, symbols: Optional[Iterable[str]] = None, limit: int = 60, period: str = "m30"
    ) -> dict:
        """一次对多只标的做区间套，返回汇总。

        用户场景：关注圈里有一批二买落在 m30 数据窗口内的标的，逐个点按钮不现实。
        数据边界：m30 仅保留约 10 个交易日，窗口外的日线买点必然 `confirmed=False`——
        这里是**如实汇总**，不做任何假确认。

        `symbols` 省略时自动取「本地有 m30 数据」的自选 + 关注圈标的。
        """

        if symbols is None:
            candidates: List[str] = []
            candidates.extend(self.store.favorite_symbols())
            candidates.extend(
                item.symbol
                for item in self.store.load_watch_items()
                if item.state in TRACKING_STATES
            )
            seen = set()
            symbols = [s for s in candidates if not (s in seen or seen.add(s))]
        targets = [s.strip() for s in symbols if (s or "").strip()][: max(1, limit)]

        rows: List[dict] = []
        confirmed = 0
        no_data = 0
        out_of_window = 0
        for symbol in targets:
            try:
                result = self.interval_nesting(symbol, period=period)
            except (ValueError, MarketDataError) as exc:
                rows.append({"symbol": symbol, "status": "error", "message": str(exc)})
                continue
            if not result.get("available"):
                no_data += 1
                status = "no_data"
            elif result.get("confirmed"):
                confirmed += 1
                status = "confirmed"
            else:
                # available 但未确认：九成是 anchor 落在 m30 窗口外
                out_of_window += 1
                status = "unconfirmed"
            rows.append(
                {
                    "symbol": symbol,
                    "status": status,
                    "anchor_date": result.get("anchor_date"),
                    "anchor_kind": result.get("anchor_kind"),
                    "confirmed": bool(result.get("confirmed")),
                    "matched": result.get("matched"),
                    "m30_window": result.get("m30_window"),
                    "message": result.get("message"),
                }
            )

        return {
            "checked": len(rows),
            "confirmed": confirmed,
            "unconfirmed": out_of_window,
            "no_data": no_data,
            "items": rows,
            "message": (
                f"区间套检查 {len(rows)} 只：{confirmed} 只确认、{out_of_window} 只未确认"
                f"（多在 30 分钟数据窗口外）、{no_data} 只无 m30 数据"
                if rows
                else "没有可检查的标的：自选与关注圈均为空"
            ),
        }

    # ------------------------------------------------------------------
    # 板块聚合热度
    # ------------------------------------------------------------------

    def sector_heat(self, days: int = 3, period: str = DEFAULT_PERIOD) -> dict:
        """把近 N 交易日买点按板块聚合计数，看信号集中在哪些板块。

        实现上刻意**只统计已有板块缓存的个股**（`stock_sectors` 表）：
        反查一只新股票的板块要 5~6 秒，为了一个聚合视图去联网反查几百只是不可接受的。
        没缓存的记为「板块未知」，如实反映覆盖率而不是假装全都有。
        """

        period = normalize_period(period)
        trading_dates = self.store.recent_trading_dates(days)
        cutoff = trading_dates[-1] if trading_dates else ""
        rows = self.store.buy_signals_since(cutoff, period=period) if cutoff else []

        # 同标的同类型去重（与买点池口径一致），避免同一信号反复计入
        seen = set()
        deduped = []
        for row in rows:
            key = (row["symbol"], row["kind"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)

        buckets: Dict[str, dict] = {}
        unknown = {"count": 0, "first_buy": 0, "second_buy": 0, "third_buy": 0, "symbols": []}
        covered = 0
        for row in deduped:
            sectors = self.store.load_stock_sectors(row["symbol"])
            if sectors:
                covered += 1
            kind_key = {
                SignalKind.FIRST_BUY.value: "first_buy",
                SignalKind.SECOND_BUY.value: "second_buy",
                SignalKind.THIRD_BUY.value: "third_buy",
            }.get(row["kind"])
            if not kind_key:
                continue
            if not sectors:
                unknown["count"] += 1
                unknown[kind_key] += 1
                if len(unknown["symbols"]) < 50:
                    unknown["symbols"].append(row["symbol"])
                continue
            # 一只票可能属于多个板块，每个板块各计一次（这是「热度」而非「归属」）
            for sector in sectors[:3]:
                key = sector["code"]
                bucket = buckets.setdefault(
                    key,
                    {
                        "code": key,
                        "name": sector.get("name", key),
                        "level": sector.get("level", ""),
                        "count": 0,
                        "first_buy": 0,
                        "second_buy": 0,
                        "third_buy": 0,
                        "symbols": [],
                    },
                )
                bucket["count"] += 1
                bucket[kind_key] += 1
                if len(bucket["symbols"]) < 30:
                    bucket["symbols"].append(row["symbol"])

        ranked = sorted(buckets.values(), key=lambda b: (-b["count"], b["name"]))
        return {
            "days": days,
            "period": period,
            "cutoff": cutoff,
            "signals": len(deduped),
            "covered": covered,
            "sectors": ranked,
            "unknown": unknown,
            "message": (
                f"近 {days} 个交易日 {len(deduped)} 个买点，其中 {covered} 只已缓存板块映射；"
                f"{unknown['count']} 只板块未知（板块反查成本高，只统计已缓存部分）"
            ),
        }

    # ------------------------------------------------------------------
    # 扫描历史 + 复盘总结
    # ------------------------------------------------------------------

    def scan_history(self, limit: int = 20) -> dict:
        """扫描历史列表 + 每个任务的复盘总结。"""

        runs = self.store.list_runs(limit=limit)
        names: Dict[str, str] = {}
        for run in runs:
            names[run["scope"]] = UNIVERSE_SCOPE_LABELS.get(run["scope"], run["scope"])
        items = []
        for run in runs:
            hits = self.store.hit_counts(run["run_id"])
            items.append(
                {
                    "run_id": run["run_id"],
                    "scope": run["scope"],
                    "scope_label": names.get(run["scope"], run["scope"]),
                    "status": run["status"],
                    "total": run["total"],
                    "processed": run["processed"],
                    "hits": run["hits"],
                    "message": run["message"],
                    "started_at": run["started_at"],
                    "finished_at": run["finished_at"],
                    "elapsed": round((run["finished_at"] or 0) - run["started_at"], 1)
                    if run["finished_at"]
                    else None,
                    "by_kind": hits,
                }
            )
        return {
            "count": len(items),
            "items": items,
            "message": f"共 {len(items)} 条扫描记录" if items else "还没有扫描记录",
        }

    # ------------------------------------------------------------------
    # 参数敏感性回测
    # ------------------------------------------------------------------

    def param_sensitivity(
        self, symbol: str, period: str = DEFAULT_PERIOD, days: int = DEFAULT_ANALYSIS_DAYS
    ) -> dict:
        """对关键参数取若干档位重跑引擎，看信号数如何变化（纯本地，不联网）。

        用途：判断「这只票的信号是不是靠放宽阈值硬凑出来的」。
        如果 `min_divergence_score` 从 0.25 降到 0.15 信号数暴涨，
        说明当前参数下检出的信号对阈值高度敏感，可信度要打折。

        只对**本地已有 K 线**做，不触发取数——回测不该带来网络副作用。
        """

        symbol = self._require_symbol(symbol)
        period = normalize_period(period)
        bars = self.store.load_bars(symbol, limit=days, period=period)
        if len(bars) < MIN_BARS:
            return {
                "available": False,
                "symbol": symbol,
                "period": period,
                "message": f"本地 {PERIOD_LABELS.get(period, period)} K 线不足（{len(bars)} 根），无法回测",
            }

        base_score = self.config.min_divergence_score
        base_wait = self.config.max_wait_bars
        base_kisses = self.config.ma_min_male_kisses
        buys = BUY_KIND_VALUES

        def run_with(**overrides) -> dict:
            config = replace(self.config, **overrides)
            result = ChanEngine(config).scan(bars)
            counts = {kind: 0 for kind in ("FIRST_BUY", "SECOND_BUY", "THIRD_BUY", "FIRST_SELL", "SECOND_SELL")}
            for signal in result.signals:
                if signal.kind.value in counts:
                    counts[signal.kind.value] += 1
            return {
                "total": len(result.signals),
                "buys": sum(counts[k] for k in ("FIRST_BUY", "SECOND_BUY", "THIRD_BUY")),
                "sells": sum(counts[k] for k in ("FIRST_SELL", "SECOND_SELL")),
                "by_kind": counts,
                "zhongshu": len(result.zhongshu),
            }

        baseline = run_with()
        sweeps = []

        score_grid = sorted({round(base_score * f, 4) for f in (0.6, 0.8, 1.0, 1.25, 1.6)})
        for value in score_grid:
            row = run_with(min_divergence_score=value)
            row["value"] = value
            row["current"] = abs(value - base_score) < 1e-9
            sweeps.append(
                {
                    "param": "min_divergence_score",
                    "label": "背驰分数阈值",
                    "value": value,
                    "current": row["current"],
                    "buys": row["buys"],
                    "sells": row["sells"],
                    "total": row["total"],
                    **row["by_kind"],
                }
            )

        wait_grid = sorted({max(5, int(base_wait * f)) for f in (0.5, 0.75, 1.0, 1.5, 2.0)})
        for value in wait_grid:
            row = run_with(max_wait_bars=value)
            sweeps.append(
                {
                    "param": "max_wait_bars",
                    "label": "二买等待窗口",
                    "value": value,
                    "current": value == base_wait,
                    "buys": row["buys"],
                    "sells": row["sells"],
                    "total": row["total"],
                    **row["by_kind"],
                }
            )

        kiss_grid = sorted({0, 1, 2, 3, max(3, base_kisses)})
        for value in kiss_grid:
            row = run_with(ma_min_male_kisses=value)
            sweeps.append(
                {
                    "param": "ma_min_male_kisses",
                    "label": "男上位最少吻数",
                    "value": value,
                    "current": value == base_kisses,
                    "buys": row["buys"],
                    "sells": row["sells"],
                    "total": row["total"],
                    **row["by_kind"],
                }
            )

        # 敏感度：当前档位的买点数相对全网格极差的比例。极差为 0 说明参数无关。
        buy_values = [row["buys"] for row in sweeps]
        spread = max(buy_values) - min(buy_values) if buy_values else 0
        return {
            "available": True,
            "symbol": symbol,
            "name": self.store.symbol_name(symbol),
            "period": period,
            "period_label": PERIOD_LABELS.get(period, period),
            "bars": len(bars),
            "baseline": baseline,
            "sweeps": sweeps,
            "buy_spread": spread,
            "sensitive": spread > max(2, baseline["buys"]),
            "message": (
                f"{symbol} 在 {len(bars)} 根{PERIOD_LABELS.get(period, period)}K线上，"
                f"基线 {baseline['total']} 个信号（买 {baseline['buys']} / 卖 {baseline['sells']}）；"
                f"参数网格下买点数极差 {spread}"
                + ("（对参数敏感，信号可信度需打折）" if spread > max(2, baseline["buys"]) else "（对参数不敏感，结论较稳）")
            ),
        }



    # ------------------------------------------------------------------
    # 自成长体系 · 数据地基（M1）：买点结局 + 特征
    #
    # 「赚钱」不能靠感觉，必须可回测。这里给每个历史买点标注三重结局：
    #   max_return   信号后 OUTCOME_WINDOW_BARS 根日线内的最大涨幅（主标签，
    #                回答「结构有没有拉起来」，A 股常先冲高再回落，期末收益会低估）
    #   hold_return  期末收益（回答「涨了拿不拿得住」）
    #   max_drawdown 期间最大回撤（回答「拉升前有没有把你洗出去」）
    # 特征一律只取「信号时刻及之前」可得的数据——防未来函数是项目铁律。
    # ------------------------------------------------------------------

    def rebuild_outcomes(self, period: str = DEFAULT_PERIOD) -> dict:
        """离线重建样本库：遍历全部日线买点，算结局 + 特征，幂等 upsert。

        结果表是 signal_outcomes；每次调用都是全量重算（幂等，新交易日自动补全）。
        这是「自成长」的底座——M2 的增益分析、M3 的匹配打分都建立在它之上。
        """

        period = normalize_period(period)
        if period != DEFAULT_PERIOD:
            raise ValueError("结局样本库目前只对日线买点有意义（分钟数据窗口太短）")
        # 市场情绪特征（宽度/涨停/指数）依赖全市场 bars，冷却期内不重算（约 14 秒）。
        self._ensure_market_breadth()
        signals = self.store.all_signals_by_kinds(BUY_KINDS, period=period)
        if not signals:
            return {
                "signals": 0,
                "outcomes": 0,
                "message": "本地还没有日线买点信号：先执行一次市场扫描",
            }

        # 按 symbol 分组，每只票只取一次 bars（避免逐信号 load_bars 的 N+1）
        by_symbol: Dict[str, list[dict]] = {}
        for sig in signals:
            by_symbol.setdefault(sig["symbol"], []).append(sig)

        # v2 出场计算的全局底料（各一次查询，绝不进信号循环）：
        # - 全市场卖点序列（按 symbol 分组）——出场规则「信号后第一个卖点」
        sells_by_symbol: Dict[str, List[tuple]] = {}
        all_sells = self.store.all_signals_by_kinds(SELL_KINDS, period=period)
        for row in all_sells:
            sells_by_symbol.setdefault(row["symbol"], []).append((row["date"], row["price"]))
        for seq in sells_by_symbol.values():
            seq.sort(key=lambda t: t[0])
        # - 基准指数日线（fq='ref'）——alpha 分母
        benchmark_seq: List[tuple] = []
        try:
            bench_bars = self.store.load_bars(ALPHA_BENCHMARK, limit=1000, period=period, fq="ref")
            benchmark_seq = [(b.date, b.close) for b in bench_bars]
        except Exception:  # noqa: BLE001 - 指数缺失只影响 alpha_exit，不该拖垮重建
            benchmark_seq = []
        # - m30 数据窗口起点：判断「信号时刻次级别数据是否可得」用
        m30_earliest: Optional[str] = None
        m30_meta = self.store.period_earliest_date("m30")
        if m30_meta:
            m30_earliest = m30_meta

        outcomes: List[dict] = []
        skipped = 0
        weekly_cache: dict = {}
        for symbol, sigs in by_symbol.items():
            bars = self.store.load_bars(symbol, limit=DEFAULT_ANALYSIS_DAYS, period=period)
            if not bars:
                skipped += len(sigs)
                continue
            dates = [bar.date for bar in bars]
            date_set = set(dates)
            ind = self._indicator_cache(bars)
            # 跨信号特征：该股信号日之前的「一买成功率」（防未来函数，时序累计）。
            # sigs 已按 trade_date 升序（all_signals_by_kinds 的 ORDER BY），
            # 所以「已处理的一买」天然都在当前信号之前。
            bar_pos = {date: i for i, date in enumerate(dates)}
            second_buy_dates = [s["date"] for s in sigs if s["kind"] == SignalKind.SECOND_BUY.value]
            sell_seq = sells_by_symbol.get(symbol, [])
            prior_first_buys: List[bool] = []
            for sig in sigs:
                prior_win_rate = None
                if prior_first_buys:
                    prior_win_rate = round(sum(prior_first_buys) / len(prior_first_buys), 4)
                outcomes.append(
                    self._compute_outcome(
                        symbol, sig, bars, dates, date_set, ind, prior_win_rate,
                        sell_dates=sell_seq, benchmark_seq=benchmark_seq,
                        m30_earliest_date=m30_earliest, weekly_cache=weekly_cache,
                    )
                )
                if sig["kind"] == SignalKind.FIRST_BUY.value:
                    success = self._first_buy_succeeded(sig["date"], second_buy_dates, bar_pos)
                    if success is not None:
                        prior_first_buys.append(success)

        self.store.save_outcomes(outcomes)
        stats = self.store.outcome_stats(period=period)
        # 重建后顺带做一次增益分析并存快照，让「当前体系」始终与最新样本对齐。
        snapshot = self.system_snapshot(period=period)
        # v2 出场口径的覆盖率：有出场结果的样本占比（无后续 K 线的最近信号天然为空）
        with_exit = sum(1 for o in outcomes if o.get("exit_return") is not None)
        with_alpha = sum(1 for o in outcomes if o.get("alpha_exit") is not None)
        return {
            "signals": len(signals),
            "outcomes": len(outcomes),
            "skipped_no_bars": skipped,
            "with_exit": with_exit,
            "with_alpha": with_alpha,
            "exit_window_bars": EXIT_WINDOW_BARS,
            "trade_cost": TRADE_COST_ROUNDTRIP,
            "stats": stats,
            "snapshot_id": snapshot.get("id"),
            "message": (
                f"已重建 {len(outcomes)} 条买点结局样本（跳过 {skipped} 条无本地K线；"
                f"规则化出场 {with_exit} 条、含alpha {with_alpha} 条）"
            ),
        }

    def _ensure_market_breadth(self) -> None:
        """确保市场情绪表已算（冷却 6 小时，数据每天收盘才变一次）。"""

        if not self.store.market_breadth_count():
            self.store.compute_market_breadth()
            self.store.mark_fetch("market_breadth", "all")
            return
        if not self.store.fetched_within("market_breadth", "all", 6 * 3600):
            self.store.compute_market_breadth()
            self.store.mark_fetch("market_breadth", "all")

    def _first_buy_succeeded(
        self, fb_date: str, second_buy_dates: list[str], bar_pos: dict
    ) -> Optional[bool]:
        """一次一买是否成功（max_wait_bars 个交易日内引出二买）。

        与 `_judge_first_buy` 口径一致，只是简化成布尔（供时序累计用）。
        bar_pos 是「交易日序」，不能相减 bar_index（见既有注释）。
        """

        fb_pos = bar_pos.get(fb_date)
        if fb_pos is None:
            return None
        for sb_date in second_buy_dates:
            sb_pos = bar_pos.get(sb_date)
            if sb_pos is not None and sb_pos > fb_pos and sb_pos - fb_pos <= self.config.max_wait_bars:
                return True
        return False

    @staticmethod
    def _indicator_cache(bars: list[Bar]) -> dict:
        """每只票只算一次的指标序列（对齐 bars 长度），供所有信号复用。

        这是性能关键：25692 个信号如果每个都重算整段 MACD/KDJ/RSI，
        复杂度就是 O(signals × bars)，会慢到不可用。预计算一次 O(bars)，
        每个信号只做 O(1) 的下标取值。
        """

        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        vols = [b.volume for b in bars]
        macd = macd_state(closes)
        return {
            "closes": closes,
            "vols": vols,
            "macd_hist": macd.hist,
            "macd_dif": macd.dif,
            "rsi14": rsi(closes, 14),
            "kdj": kdj(highs, lows, closes, 9),
            "ma5": sma(closes, 5),
            "ma10": sma(closes, 10),
            "ma20": sma(closes, 20),
            "ma60": sma(closes, 60),
            "volatility20": returns_std(closes, 20),
        }

    def _compute_outcome(
        self, symbol: str, sig: dict, bars: list[Bar], dates: list[str], date_set: set,
        ind: dict, prior_win_rate: Optional[float] = None,
        sell_dates: Optional[List[tuple]] = None,
        benchmark_seq: Optional[List[tuple]] = None,
        m30_bars: Optional[List[Bar]] = None,
        m30_earliest_date: Optional[str] = None,
        weekly_cache: Optional[dict] = None,
    ) -> dict:
        """算单个买点的结局 + 特征（含 v2 规则化出场）。

        买入基准用信号价 `price`（分型后下一根 K 线的确认价，与成功率统计口径一致）。
        结局只看 `signal_date` 之后（不含当天）的 K 线——当天信号是收盘后才确认的。

        规则化出场（v2 标签，见模块常量 EXIT_WINDOW_BARS 注释）：
        - sell_dates: 该股全部卖点 [(trade_date, price)] 升序——信号后 EXIT_WINDOW_BARS
          内第一个卖点即出场；没有则窗口末收盘强平。
        - benchmark_seq: 上证指数 [(trade_date, close)] 升序——算同期 alpha。
        """

        signal_date = sig["date"]
        price = float(sig["price"])
        future = [bar for bar in bars if bar.date > signal_date]
        window = future[:OUTCOME_WINDOW_BARS]
        exit_window = future[:EXIT_WINDOW_BARS]

        outcome = {
            "symbol": symbol,
            "kind": sig["kind"],
            "period": sig["period"],
            "signal_date": signal_date,
            "price": price,
            "anchor_price": sig["anchor_price"],
            "window_size": len(window),
            "features": self._extract_features(
                sig, bars, dates, date_set, ind, prior_win_rate,
                m30_earliest_date=m30_earliest_date,
            ),
        }
        if not window:
            return outcome  # 无后续 K 线 → 结局留空（max_return 等为 NULL）

        # ---- 周线中枢约束（高级别递归 v1，防未来函数：只用信号日前的周线）----
        if weekly_cache is not None:
            try:
                weekly_feats = self._weekly_zs_features(symbol, signal_date, price, bars, weekly_cache)
                if weekly_feats:
                    outcome["features"].update(weekly_feats)
            except Exception:  # noqa: BLE001 - 周线特征失败不拖垮结局重建
                pass

        outcome["max_return"] = round(max(b.close for b in window) / price - 1.0, 4)
        outcome["hold_return"] = round(window[-1].close / price - 1.0, 4)
        # 用 low 计回撤（更保守：盘中最深的浮亏，哪怕收盘拉回来也记）
        outcome["max_drawdown"] = round(min(b.low for b in window) / price - 1.0, 4)
        peak_idx = max(range(len(window)), key=lambda i: window[i].close)
        outcome["days_to_peak"] = peak_idx + 1

        # ---- v2 规则化出场：卖点优先，否则强平；净收益扣成本；alpha 对基准 ----
        if exit_window:
            exit_price: Optional[float] = None
            exit_date: Optional[str] = None
            exit_kind: Optional[str] = None
            # 出场候选只看窗口内的 K 线——窗口外（更晚）的卖点与本笔交易无关，
            # 那是「纪律强平之后」行情又涨了多少，不能算进这笔的收益。
            window_end = exit_window[-1].date
            if sell_dates:
                for sd, sp in sell_dates:
                    if signal_date < sd <= window_end:
                        # 卖点信号价即出场价（与买点同口径：分型后下一根确认价）
                        exit_price, exit_date, exit_kind = float(sp), sd, "SELL"
                        break
            if exit_price is None:
                # 窗口内无卖点 → 末根收盘强平（纪律出场）
                last = exit_window[-1]
                exit_price, exit_date, exit_kind = last.close, last.date, "FLAT_CLOSE"
            if exit_price and exit_price > 0:
                raw = exit_price / price - 1.0
                outcome["exit_return"] = round(raw, 4)
                outcome["exit_date"] = exit_date
                outcome["exit_days"] = len([b for b in exit_window if b.date <= exit_date])
                outcome["exit_kind"] = exit_kind
                outcome["net_exit_return"] = round(raw - TRADE_COST_ROUNDTRIP, 4)
                if benchmark_seq:
                    bench = self._benchmark_return(benchmark_seq, signal_date, exit_date)
                    if bench is not None:
                        outcome["alpha_exit"] = round(outcome["net_exit_return"] - bench, 4)
        return outcome

    @staticmethod
    def _sublevel_confirmability(sig: dict, m30_earliest_date: Optional[str]) -> Optional[int]:
        """该二买在信号时刻，次级别（m30）数据是否**可得**（防未来函数的合法特征）。

        体检方向之一是「区间套确认与否」——但「确认成功」是信号日之后才能
        确定的事实，直接当特征就是未来函数（v2 开发中真实踩过：注释里给自己
        开脱的那版实现）。真正合法的信号时刻特征只有「可得性」：
        m30 数据窗口覆盖到信号日（= 数据源留存期内），该二买才**可能**被
        次级别确认；窗口外的二买连验证的机会都没有。
        这个特征刻画「数据完备性」，与买点质量无直接因果，但对**理解
        验证覆盖率**有价值；是否保留由统计说话（增益≈0 会自然沉底）。
        """

        if sig.get("kind") != SignalKind.SECOND_BUY.value:
            return None
        if not m30_earliest_date:
            return None
        return 1 if str(sig.get("date", "")) >= m30_earliest_date else 0

    def _weekly_zs_features(
        self, symbol: str, signal_date: str, price: float, daily_bars: list[Bar],
        cache: dict,
    ) -> dict:
        """周线中枢约束特征（高级别递归 v1）。

        缠论多级别联动：日线买点若处在周线中枢的有利位置，胜率与幅度都不同——
        周线下方的一买（大级别背驰）与周线中枢内部的一买（盘整里的小波动）
        完全是两种行情。

        特征（全部只用信号日**及之前**的周线，防未来函数）：
        - weekly_zs_position：信号日收盘在**最近一个已完成**周线中枢的归一位置
          （<0 下方 / 0-1 内部 / >1 上方；没有周线中枢时为 None）
        - weekly_trend_20：周线 20 周均线趋势（高级别方向）

        `cache` 按 symbol 缓存重采样+引擎结果（每只票在 rebuild 循环里只算一次）。
        """

        if symbol not in cache:
            weekly = self._resample_weekly(daily_bars)
            # 只用信号日及之前的周线由调用方保证？不——周线由**全部日线**重采样，
            # 这里必须先按 signal_date 截断再喂引擎，否则未来数据会漏进中枢。
            result = self.engine.scan(weekly) if len(weekly) >= 30 else None
            cache[symbol] = (weekly, result)
        weekly, result = cache[symbol]
        features: dict = {}
        if not weekly or result is None:
            return features

        # 信号日及之前的周线（周线 date 是周内最后一天；用「周结束日 >= 信号日」
        # 会包含当周——当周还没走完。严格防未来：只取周结束日 < 信号日的周线；
        # 信号日当周的部分信息（周一到信号日）不足以构成一个周线分型，舍弃是
        # 保守且合法的。
        past_weekly = [w for w in weekly if w.date < signal_date]
        if len(past_weekly) >= 20:
            closes = [w.close for w in past_weekly]
            ma20_prev = sum(closes[-40:-20]) / 20 if len(closes) >= 40 else None
            ma20_now = sum(closes[-20:]) / 20
            if ma20_prev:
                features["weekly_trend_20"] = round(ma20_now / ma20_prev - 1.0, 4)

        if len(past_weekly) >= 30:
            # 中枢只认「结束日在信号日之前」的——已完成的周线中枢
            zs_list = [z for z in result.zhongshu if weekly[z.end_index].date < signal_date]
            if zs_list:
                latest = zs_list[-1]
                zd, zg = latest.zd, latest.zg
                if zg > zd:
                    features["weekly_zs_position"] = round((price - zd) / (zg - zd), 4)
        return features

    @staticmethod
    def _benchmark_return(seq: List[tuple], start_date: str, end_date: str) -> Optional[float]:
        """基准指数在 [start_date, end_date] 区间的涨跌（两端都按实际交易日对齐）。"""

        if not seq:
            return None
        dates = [d for d, _ in seq]
        try:
            i = next(k for k, d in enumerate(dates) if d >= start_date)
            j = next(k for k, d in enumerate(dates) if d >= end_date)
        except StopIteration:
            return None
        base = seq[i][1]
        return seq[j][1] / base - 1.0 if base else None

    @staticmethod
    def _resample_weekly(bars: List[Bar]) -> List[Bar]:
        """日线重采样为周线（ISO 周，周一为一周起点；最后一周可能是残周）。

        高级别递归 v1：缠论多级别联动里，日线买点最好在周线中枢的有利位置。
        周线 OHLC 直接聚合（high 取周内最高、low 取周内最低、volume/amount 求和）。
        """

        from datetime import datetime as _dt

        weekly: List[Bar] = []
        current_key: Optional[str] = None
        open_ = high_ = low_ = close_ = None
        volume_ = amount_ = 0.0
        date_ = symbol_ = None
        for bar in bars:
            try:
                d = _dt.strptime(bar.date[:10], "%Y-%m-%d")
            except ValueError:
                continue
            key = f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
            if key != current_key:
                if current_key is not None and close_ is not None:
                    weekly.append(
                        Bar(
                            symbol=symbol_,
                            date=date_,
                            open=open_,
                            high=high_,
                            low=low_,
                            close=close_,
                            volume=volume_,
                            amount=amount_,
                            period="week",
                        )
                    )
                current_key = key
                open_, high_, low_ = bar.open, bar.high, bar.low
                close_, volume_, amount_ = bar.close, bar.volume, bar.amount
                date_, symbol_ = bar.date, bar.symbol
            else:
                high_ = max(high_, bar.high)
                low_ = min(low_, bar.low)
                close_ = bar.close
                volume_ += bar.volume
                amount_ += bar.amount
                date_ = bar.date  # 周内最后一天（信号对齐用）
        if current_key is not None and close_ is not None:
            weekly.append(
                Bar(
                    symbol=symbol_,
                    date=date_,
                    open=open_,
                    high=high_,
                    low=low_,
                    close=close_,
                    volume=volume_,
                    amount=amount_,
                    period="week",
                )
            )
        return weekly

    def _extract_features(
        self, sig: dict, bars: list[Bar], dates: list[str], date_set: set,
        ind: dict, prior_win_rate: Optional[float] = None,
        m30_earliest_date: Optional[str] = None,
    ) -> dict:
        """提取「信号时刻可得」的特征（防未来函数：只用 idx 及之前的数据）。

        每个特征独立计算、缺数据就留 None 或跳过，绝不臆造。指标序列已在
        `_indicator_cache` 预计算，这里只做下标取值。
        """

        signal_date = sig["date"]
        price = float(sig["price"])
        features: Dict[str, Any] = {}

        # 信号日在 bars 里的位置；不在（K 线窗口被裁剪）就只留结构/情绪/历史特征
        idx = dates.index(signal_date) if signal_date in date_set else None
        if idx is not None:
            closes = ind["closes"]
            vols = ind["vols"]

            # ---- 量价 ----
            # 量比（20 日）：信号日量 / 前 20 日均量
            day_vol = vols[idx]
            prev_vol = vols[max(0, idx - 20):idx]
            if prev_vol:
                avg_vol = sum(prev_vol) / len(prev_vol)
                if avg_vol > 0:
                    features["vol_ratio"] = round(day_vol / avg_vol, 4)
            # 短期量比（5 日）：更敏感的放量信号
            prev_vol5 = vols[max(0, idx - 5):idx]
            if prev_vol5:
                avg_vol5 = sum(prev_vol5) / len(prev_vol5)
                if avg_vol5 > 0:
                    features["vol_ratio_5"] = round(day_vol / avg_vol5, 4)

            # ---- 位置 ----
            lookback = bars[max(0, idx - 60):idx + 1]
            if lookback:
                hi = max(b.high for b in lookback)
                if hi > 0:
                    features["retrace_from_high"] = round((hi - price) / hi, 4)
            # 相对 MA60 的位置（站上/跌破中期均线）
            if ind["ma60"][idx]:
                features["price_vs_ma60"] = round(price / ind["ma60"][idx] - 1.0, 4)

            # ---- 均线 ----
            if idx >= 19:
                features["above_ma20"] = 1 if price >= ind["ma20"][idx] else 0
            # 多头排列：MA5 > MA10 > MA20
            if idx >= 19:
                features["ma_bullish"] = 1 if (
                    ind["ma5"][idx] > ind["ma10"][idx] > ind["ma20"][idx]
                ) else 0

            # ---- 动量指标 ----
            hist = ind["macd_hist"]
            if idx < len(hist):
                features["macd_hist_sign"] = 1 if hist[idx] >= 0 else 0
            dif = ind["macd_dif"]
            if idx < len(dif):
                features["dif_above_zero"] = 1 if dif[idx] >= 0 else 0
            if idx < len(ind["rsi14"]) and ind["rsi14"][idx] is not None:
                features["rsi14"] = round(ind["rsi14"][idx], 2)
            kdj_k = ind["kdj"][idx]["k"]
            features["kdj_k"] = round(kdj_k, 2)

            # ---- 股性（波动率）----
            if idx < len(ind["volatility20"]) and ind["volatility20"][idx] is not None:
                features["volatility_20"] = round(ind["volatility20"][idx], 4)

            # ---- 超跌程度：信号前连续下跌 K 线数 ----
            down = 0
            for i in range(idx, 0, -1):
                if closes[i] < closes[i - 1]:
                    down += 1
                else:
                    break
            features["consecutive_down"] = down

        # ---- 市场情绪（跨符号，从 market_breadth 表取，信号日收盘后可得）----
        breadth = self.store.load_market_breadth(signal_date)
        if breadth and breadth.get("total"):
            features["market_breadth"] = round(breadth["up_count"] / breadth["total"], 4)
            features["market_limit_up"] = breadth["limit_up"]
            if breadth.get("avg_change") is not None:
                features["market_avg_change"] = round(breadth["avg_change"], 4)
            if breadth.get("index_change") is not None:
                features["market_index_change"] = round(breadth["index_change"], 4)

        # ---- 个股历史成功率（跨信号，信号日之前，防未来函数）----
        if prior_win_rate is not None:
            features["symbol_prior_win_rate"] = prior_win_rate

        # 结构特征：直接从信号 meta 取（引擎已算好，非未来数据）
        meta = sig.get("meta") or {}
        if "divergence_score" in meta:
            features["divergence_score"] = meta["divergence_score"]

        # ---- 缠论结构特征（v3，2026-10-18）：meta 里已有中枢/两段数据，
        # 指标类特征信息量太小，结构才是缠论体系的本行。全部只用信号时刻数据。----
        zd = meta.get("zhongshu_zd")
        zg = meta.get("zhongshu_zg")
        if zd is not None and zg is not None and price > 0:
            zd, zg = float(zd), float(zg)
            if zg > zd:
                # 中枢高度相对买点价：中枢规模（越大=整理越充分）
                features["zs_height_pct"] = round((zg - zd) / price, 4)
                # 买点相对中枢的位置（有符号）：正=上方（三买区），负=下方（一买区）
                # 归一中枢高度：(price - zd) / (zg - zd)，0=贴下沿、1=贴上沿、<0=下方、>1=上方
                features["zs_position"] = round((price - zd) / (zg - zd), 4)
        # 两段力度比：背驰「衰减了多少」的连续量化（divergence_score 是分箱判定，
        # 这个是原始比值，信息更细）
        prev_eff = meta.get("previous_effort")
        cur_eff = meta.get("current_effort")
        if prev_eff is not None and cur_eff is not None and float(prev_eff) > 0:
            features["seg_effort_ratio"] = round(float(cur_eff) / float(prev_eff), 4)
        # 当前段振幅相对买点价：下跌段（一买前）/回调段（二三买前）的深度
        cur_seg = meta.get("current_segment")
        if cur_seg and price > 0:
            high = cur_seg.get("high")
            low = cur_seg.get("low")
            if high is not None and low is not None and float(high) > 0:
                features["seg_range_pct"] = round((float(high) - float(low)) / price, 4)
        # 三买的突破高点：突破前高 vs 中枢上沿的幅度（越大=突破越有力）
        breakout = meta.get("breakout_high")
        if breakout is not None and zg is not None and price > 0:
            features["breakout_strength"] = round((float(breakout) - zg) / price, 4)
        # 结构级别：段（leg）级买点 vs 笔（pen）级买点——段级中枢更可靠
        level = meta.get("structure_level")
        if level:
            features["structure_level"] = level

        # ---- 次级别数据可得性（防未来函数的合法特征，只对二买有意义）----
        confirmability = self._sublevel_confirmability(sig, m30_earliest_date)
        if confirmability is not None:
            features["sublevel_available"] = confirmability

        return features

    def outcome_summary(self, kind: Optional[str] = None, limit: int = 100, period: str = DEFAULT_PERIOD) -> dict:
        """样本库概览：聚合统计 + 最近样本。

        这是「体系成长」的第一个可见产出——即使还没做增益分析，也能看出
        「哪类买点平均最大涨幅更高 / 回撤更深」。
        """

        period = normalize_period(period)
        stats = self.store.outcome_stats(period=period)
        items = self.store.load_outcomes(kind=kind, period=period, limit=limit)
        total = self.store.outcome_count(period=period)
        return {
            "period": period,
            "period_label": PERIOD_LABELS.get(period, period),
            "total": total,
            "window_bars": OUTCOME_WINDOW_BARS,
            "stats": stats,
            "items": items,
            "message": (
                f"共 {total} 条买点结局样本（持有窗口 {OUTCOME_WINDOW_BARS} 个交易日）"
                if total
                else "样本库为空：先执行一次市场扫描，再调用重建结局"
            ),
        }

    # ------------------------------------------------------------------
    # 自成长体系 · 增益分析 + 打分 + 快照（M2/M3/M4）
    # ------------------------------------------------------------------

    def system_gain(self, threshold: float = DEFAULT_WIN_THRESHOLD, period: str = DEFAULT_PERIOD) -> dict:
        """单特征增益 + 结构-买点-胜率卡（回答「什么结构容易在什么买点拉升」）。"""

        period = normalize_period(period)
        rows = self.store.load_all_outcomes(period=period)
        if not rows:
            return {"available": False, "message": "样本库为空：先重建结局样本"}
        gain = feature_gains(rows, SYSTEM_FEATURES, threshold)
        cards = structure_buy_cards(rows, SYSTEM_FEATURES, threshold)
        return {
            "available": True,
            "period": period,
            "period_label": PERIOD_LABELS.get(period, period),
            "samples": len(rows),
            "closed": sum(1 for r in rows if r.get("max_return") is not None),
            "threshold": threshold,
            "base_win_rate": base_win_rate(rows, threshold),
            "features": SYSTEM_FEATURES,
            "gain": gain,
            "cards": cards,
            "message": (
                f"共 {len(rows)} 条样本，基准「拉升成功」率 {base_win_rate(rows, threshold):.1%}"
                f"（20 日内最大涨幅 ≥ {threshold:.0%}）。增益为正的结构更容易拉升。"
            ),
        }

    def system_validate(self, threshold: float = DEFAULT_WIN_THRESHOLD, period: str = DEFAULT_PERIOD) -> dict:
        """时间分割验证 + 分市场状态验证（体检盲区 6 的落地）。

        has_edge 只在时间分割上有 edge 不够——必须各市场状态都成立才算立得住。
        """

        period = normalize_period(period)
        rows = self.store.load_all_outcomes(period=period)
        result = validate_time_split(rows, SYSTEM_FEATURES, threshold)
        if result is None:
            return {
                "available": False,
                "message": "样本不足以做时间分割验证（需训练、验证两段各至少 30 条）",
            }
        by_regime = validate_by_regime(rows, SYSTEM_FEATURES, threshold)
        return {
            "available": True,
            "period": period,
            **result,
            "by_regime": by_regime,
            # 综合有效性：时间分割有 edge **且** 各可验证状态都有 edge
            "robust": bool(result.get("has_edge")) and bool(by_regime and by_regime.get("overall_has_edge")),
        }

    def system_snapshot(
        self, threshold: float = DEFAULT_WIN_THRESHOLD, period: str = DEFAULT_PERIOD
    ) -> dict:
        """把当前增益 + 胜率卡 + 验证结果存成一份快照，追踪体系如何随数据成长。"""

        period = normalize_period(period)
        gain_payload = self.system_gain(threshold, period)
        if not gain_payload.get("available"):
            return {"id": None, "available": False, "message": gain_payload["message"]}
        validate = self.system_validate(threshold, period)
        sid = self.store.save_snapshot(
            samples=gain_payload["samples"],
            threshold=threshold,
            gain=gain_payload["gain"],
            cards=gain_payload["cards"],
            validate=validate,
        )
        return {
            "id": sid,
            "available": True,
            "threshold": threshold,
            "base_win_rate": gain_payload["base_win_rate"],
            "samples": gain_payload["samples"],
            "validate": validate,
        }

    def system_history(self, limit: int = 20) -> dict:
        snapshots = self.store.list_snapshots(limit=limit)
        return {
            "count": len(snapshots),
            "items": snapshots,
            "message": f"共 {len(snapshots)} 份体系快照" if snapshots else "还没有体系快照：先重建结局样本",
        }

    def match_score(self, symbol: str, features: dict) -> dict:
        """用「当前体系」（最新快照的增益）给个股信号打分。

        返回 (总分, 命中明细)。分数越高，越像历史上能拉升的结构。
        没有快照时返回 available=False（先重建样本库）。
        """

        snapshot = self.store.latest_snapshot()
        if not snapshot or not snapshot.get("gain"):
            return {
                "available": False,
                "score": 0.0,
                "hits": [],
                "message": "体系尚未构建：先执行一次结局重建生成增益体系",
            }
        score, hits = score_signal(features, snapshot["gain"])
        return {
            "available": True,
            "score": score,
            "hits": hits,
        }

    def system_pick(
        self,
        days: int = 3,
        min_score: float = 0.0,
        limit: int = 100,
        period: str = DEFAULT_PERIOD,
    ) -> dict:
        """体系选股：给最近 N 个交易日的买点用「当前体系」打分，按分排序出股票池。

        这是体系的价值闭环——前面所有特征/增益/验证，最终落到「今天该关注哪些票」。
        打分复用 signal_outcomes 里已算好的特征（不重新 load K 线，毫秒级），
        所以选股基于「最近一次 rebuild 的样本库」，返回时标注截止日。
        """

        period = normalize_period(period)
        trading_dates = self.store.recent_trading_dates(days, period=period)
        if not trading_dates:
            return {
                "available": False,
                "items": [],
                "message": "没有交易日历：先执行一次市场扫描",
            }
        snapshot = self.store.latest_snapshot()
        if not snapshot or not snapshot.get("gain"):
            return {
                "available": False,
                "items": [],
                "message": "体系尚未构建：先重建结局样本生成增益体系",
            }
        cutoff = trading_dates[-1]  # 最近 N 个交易日里最早的那天
        rows = self.store.recent_outcomes(cutoff, period=period)
        names = self.store.symbol_names([r["symbol"] for r in rows])
        # 期望出场：体系分档 → 历史出场收益中位数（与池子同一套）
        exit_medians = self._exit_medians_by_score(period=period)

        items: List[dict] = []
        for r in rows:
            score, hits = score_signal(r["features"], snapshot["gain"])
            anchor = r.get("anchor_price")
            items.append(
                {
                    "symbol": r["symbol"],
                    "name": names.get(r["symbol"], r["symbol"]),
                    "kind": r["kind"],
                    "signal_date": r["signal_date"],
                    "price": r["price"],
                    "score": score,
                    "hits": hits,
                    "max_return": r.get("max_return"),
                    # v2：止损价 + 期望出场（体检 P0）
                    "stop_price": round(float(anchor), 2) if anchor else None,
                    "expected_exit": self._expected_exit_for(score, exit_medians),
                }
            )

        # 同股票同类型只保留**最新**一条——与「三日买卖点池」的去重口径一致，
        # 否则同一只票 10-14、10-15 各有一个一买时，选股 244 条 vs 池子 243 条，
        # 用户又会觉得「对不上」。最近日期排序已在 recent_outcomes 里做过。
        deduped: List[dict] = []
        seen_keys: set = set()
        for it in items:
            key = (it["symbol"], it["kind"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(it)
        items = deduped

        total_scored = len(items)
        items = [it for it in items if it["score"] >= min_score]
        items.sort(key=lambda it: -it["score"])
        items = items[: max(1, limit)]

        # 基准拉升率：从 gain 里取（每个特征箱的 base_rate 相同 = 全体基准）
        base_rate = None
        if snapshot.get("gain"):
            first = next(iter(snapshot["gain"].values()), None)
            if first:
                base_rate = first.get("base_rate")

        return {
            "available": True,
            "days": days,
            "cutoff": cutoff,
            "threshold": snapshot.get("threshold"),
            "base_win_rate": base_rate,
            "total_scored": total_scored,
            "count": len(items),
            "min_score": min_score,
            "items": items,
            "message": (
                f"近 {days} 个交易日共 {total_scored} 个买点，"
                f"体系分 ≥ {min_score} 的有 {len(items)} 只"
                if total_scored
                else f"近 {days} 个交易日（{cutoff} 起）没有买点样本：先重建结局样本"
            ),
        }

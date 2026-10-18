"""ChanPicker backend API (FastAPI).

Run:  .venv/bin/python -m uvicorn backend.app:app --port 8787
"""

from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from chanpicker.core import ChanConfig

from .provider import (
    LAUNCH_PERIODS,
    UNIVERSE_SCOPES,
    MarketDataError,
    normalize_period,
    resolve_westock_bin,
    run_westock,
)
from .service import PERIOD_LABELS, SCOPE_LABELS, ScanService
from .store import Store

BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"

app = FastAPI(title="ChanPicker 缠论选股服务", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

store = Store()
service = ScanService(store)


@app.middleware("http")
async def no_store_api_responses(request, call_next):
    """所有 /api/* 响应禁止浏览器缓存。

    为什么必须显式禁止：行情/信号是**强时效**数据，浏览器对没有 Cache-Control
    的 GET 会做启发式缓存，导致「刚扫完还是旧列表」「切回页签看到上一轮数据」。
    实测复现过：同一秒内两次请求 sector-heat 拿到不同结果，第二次是缓存。

    index.html 与静态资源不在此列（前端改动应正常缓存，靠版本号/刷新控制）。
    """

    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.exception_handler(MarketDataError)
async def market_data_error_handler(_request, exc: MarketDataError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/api/health")
def health() -> dict:
    """健康状态。除授权结论外，一并返回二进制路径、凭证目录与运行用户，便于定位
    「命令行能用、服务却报未授权」这类由用户/家目录不一致引起的问题。"""

    westock_bin: Optional[str] = None
    try:
        westock_bin = resolve_westock_bin()
    except MarketDataError:
        westock_installed = False
    else:
        westock_installed = True

    auth_ok = False
    auth_message = "未安装" if not westock_installed else ""
    detail = ""
    if westock_installed:
        try:
            run_westock(["quote", "sh000001"], timeout=20)
            auth_ok, auth_message = True, "已授权"
        except MarketDataError as exc:
            detail = str(exc)[:300]
            auth_message = "未授权" if "授权" in str(exc) else f"不可用: {exc}"

    try:
        service_user = getpass.getuser()
    except Exception:  # noqa: BLE001 - 容器里可能没有 passwd 条目
        service_user = f"uid={os.getuid()}"

    return {
        "status": "ok",
        "service": "chanpicker-backend",
        "westock_installed": westock_installed,
        "westock_authorized": auth_ok,
        "westock_message": auth_message,
        "westock_bin": westock_bin,
        "westock_home": os.getenv("WESTOCK_HOME") or str(Path.home()),
        "service_user": service_user,
        "westock_detail": detail,
        "periods": list(LAUNCH_PERIODS),
        "period_labels": PERIOD_LABELS,
        "scopes": list(UNIVERSE_SCOPES),
        "scope_labels": SCOPE_LABELS,
        "cached": store.period_counts(),
    }


@app.get("/api/universe")
def universe(
    q: Optional[str] = Query(None, description="模糊搜索：代码或名称，如 600519 / 茅台"),
    limit: int = Query(20, ge=1, le=200, description="不带 q 时返回数量上限；带 q 时也受此限制"),
) -> list[dict]:
    """标的清单（代码 + 名称）。

    带 `q` 时做模糊匹配，供前端「输入 600519 或 茅台 都能命中」的联想下拉。
    匹配顺序：代码前缀 > 代码包含 > 名称包含，同级按代码排序。
    不带 q 时保持旧行为（全量，供前端本地缓存做搜索）。
    """

    rows = store.all_symbols()
    if not q:
        return rows
    needle = q.strip().lower()
    if not needle:
        return rows[:limit]
    scored: list[tuple[int, str, dict]] = []
    for row in rows:
        symbol = row["symbol"].lower()
        name = (row.get("name") or "").lower()
        if symbol.startswith(needle):
            rank = 0
        elif needle in symbol:
            rank = 1
        elif needle in name:
            rank = 2
        else:
            continue
        scored.append((rank, row["symbol"], row))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored[:limit]]


@app.get("/api/favorites")
def list_favorites() -> dict:
    """自选 / 持仓列表（含浮盈与持仓风险提示）。

    卖点本来的语义是「**持仓**风险提示」，但全市场卖点池无法回答
    「我手上这几只今天有没有卖点」——所以自选表是卖点功能的落地场景。
    """

    return service.favorites()


@app.post("/api/favorites")
def add_favorite(
    symbol: str = Query(..., description="股票代码，如 sh600519"),
    note: str = Query("", description="备注"),
    cost_price: Optional[float] = Query(None, description="成本价，用于计算浮盈"),
) -> dict:
    try:
        return service.add_favorite(symbol, note=note, cost_price=cost_price)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/favorites")
def remove_favorite(symbol: str = Query(..., description="股票代码")) -> dict:
    try:
        return service.remove_favorite(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/market/interval-nesting/batch")
def batch_interval_nesting(
    limit: int = Query(60, ge=1, le=300, description="最多检查多少只"),
    period: str = Query("m30", description="次级别周期"),
) -> dict:
    """批量区间套：一次跑完自选 + 关注圈的区间套确认。

    m30 仅保留约 10 个交易日，窗口外的日线买点必然未确认——如实汇总，不做假确认。
    """

    try:
        return service.batch_interval_nesting(limit=limit, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/market/sector-heat")
def sector_heat(
    days: int = Query(3, ge=1, le=20, description="最近 N 个交易日"),
    period: str = Query("day", description="周期"),
) -> dict:
    """板块聚合热度：近 N 日买点按板块计数，看信号集中在哪些板块。

    只统计已有板块缓存的个股（板块反查单只 5~6 秒，不能为一个聚合视图去联网反查）。
    """

    try:
        return service.sector_heat(days=days, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/market/scan/history")
def scan_history(limit: int = Query(20, ge=1, le=100)) -> dict:
    """扫描历史 + 每次任务的复盘总结（按信号类型的命中数）。"""

    return service.scan_history(limit=limit)


@app.get("/api/market/scan/incremental-plan")
def incremental_plan(scope: str = Query("all", description="扫描范围")) -> dict:
    """增量扫描预演：只挑「本地日线落后于最新交易日」的标的，返回需要更新的数量。"""

    try:
        plan = service.incremental_scan_targets(scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {k: v for k, v in plan.items() if k != "targets"}


@app.post("/api/market/scan/incremental")
def start_incremental_scan(scope: str = Query("all", description="扫描范围")) -> dict:
    """启动增量扫描（只扫需要更新的标的）。没有可扫的会直接返回提示而非空跑。"""

    try:
        return service.start_incremental_scan(scope)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/backtest/sensitivity")
def param_sensitivity(
    symbol: str = Query(..., description="股票代码"),
    period: str = Query("day", description="周期"),
    days: int = Query(400, ge=60, le=1000),
) -> dict:
    """参数敏感性回测：对关键阈值取多档重跑，看信号数如何变化（纯本地，不联网）。"""

    try:
        return service.param_sensitivity(symbol, period=period, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/system/rebuild-outcomes")
def rebuild_outcomes(period: str = Query("day", description="周期，目前只支持 day")) -> dict:
    """离线重建买点结局样本库（自成长体系 M1 的地基）。

    遍历全部日线买点，回溯计算信号后 20 个交易日的三重结局
    （最大涨幅 / 期末收益 / 最大回撤）与信号时刻可得的特征，幂等 upsert。
    每次调用都是全量重算，新交易日之后自动补全。
    """

    try:
        return service.rebuild_outcomes(period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/system/outcomes")
def outcome_summary(
    kind: Optional[str] = Query(None, description="只返回某类买点，如 FIRST_BUY"),
    limit: int = Query(100, ge=1, le=1000),
    period: str = Query("day", description="周期"),
) -> dict:
    """买点结局样本库概览：按类型聚合的胜率/平均涨幅/回撤 + 最近样本。"""

    try:
        return service.outcome_summary(kind=kind, limit=limit, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/system/gain")
def system_gain(
    threshold: float = Query(0.10, ge=0.0, le=1.0, description="「拉升成功」的最大涨幅阈值"),
    period: str = Query("day", description="周期"),
) -> dict:
    """单特征增益 + 结构-买点-胜率卡（回答「什么结构容易在什么买点拉升」）。"""

    try:
        return service.system_gain(threshold=threshold, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/system/validate")
def system_validate(
    threshold: float = Query(0.10, ge=0.0, le=1.0),
    period: str = Query("day", description="周期"),
) -> dict:
    """时间分割验证：老样本算增益、新样本验证「高分档是否真的更赚钱」。"""

    try:
        return service.system_validate(threshold=threshold, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/system/snapshot")
def system_snapshot(
    threshold: float = Query(0.10, ge=0.0, le=1.0),
    period: str = Query("day", description="周期"),
) -> dict:
    """把当前增益 + 胜率卡 + 验证结果存成一份体系快照，追踪体系成长。"""

    try:
        return service.system_snapshot(threshold=threshold, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/system/history")
def system_history(limit: int = Query(20, ge=1, le=100)) -> dict:
    """体系快照历史（追踪体系如何随数据成长）。"""

    return service.system_history(limit=limit)


@app.get("/api/system/pick")
def system_pick(
    days: int = Query(3, ge=1, le=20, description="最近 N 个交易日"),
    min_score: float = Query(0.0, description="体系分下限，0 表示只留比基准更可能拉升的"),
    limit: int = Query(100, ge=1, le=500),
    period: str = Query("day", description="周期"),
) -> dict:
    """体系选股：给最近 N 个交易日的买点用「当前体系」打分，按分排序出股票池。

    这是体系的价值闭环——特征/增益/验证最终落到「今天该关注哪些票」。
    打分基于最近一次 rebuild 的样本库（复用已算特征，不联网、毫秒级）。
    """

    try:
        return service.system_pick(days=days, min_score=min_score, limit=limit, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc




@app.get("/api/analyze")
def analyze(
    symbol: str = Query(..., description="股票代码，如 sh600519"),
    days: int = Query(400, ge=60, le=1000),
    refresh: bool = Query(False, description="忽略缓存强制拉取最新行情"),
    period: str = Query("day", description=f"K线周期：{'/'.join(LAUNCH_PERIODS)}"),
) -> dict:
    try:
        return service.analyze_symbol(symbol, days=days, use_cache=not refresh, period=period)
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/analysis/comparison")
def analysis_comparison(
    symbol: str = Query(..., description="股票代码，如 sh600519"),
    period: str = Query("day", description="K线周期，同期对比仅日线有意义"),
) -> dict:
    """板块 + 同期个股/板块/大盘对比的**独立懒加载接口**。

    为什么单独一个接口：板块反查实测 5.7 秒（1 次产业链 + 最多 3 次板块搜索，
    每次间隔 3 秒），放进 /api/analyze 会让详情页首屏卡 6 秒。拆出来让前端
    先渲染 K 线与买卖点，再异步补这条对比信息——第一次抓完就落库
    （stock_sectors + bars fq='ref'），之后同样的请求 3ms。
    """

    try:
        return service.comparison_payload(symbol, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/market/scan")
def start_market_scan(
    scope: str = Query("all", description="扫描范围：all=全部A股（约5576只），hs300=沪深300成分股"),
    force: bool = Query(False, description="忽略本地缓存强制重新取数（全市场下约需 30-50 分钟）"),
) -> dict:
    try:
        return service.start_market_scan(scope=scope, force=force)
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"[INVALID_SCOPE] {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=f"[SCAN_BUSY] {exc}") from exc


@app.get("/api/market/scan/status")
def market_scan_status(run_id: Optional[str] = None) -> dict:
    return service.scan_status(run_id)


@app.get("/api/market/candidates")
def market_candidates(
    recent_days: int = Query(30, ge=1, le=500, description="统计最近 N 个交易日（与 /api/market/pools 完全同源同口径）"),
    period: str = Query("day", description="周期，默认 day"),
) -> dict:
    """市场扫描候选 = signals 表里近 N 个交易日的买点。

    与 `/api/market/pools` 用同一套取数+去重逻辑，两者只差窗口长度：
    传相同的 N 时数字必然一致。这样「扫描 → 池 → 关注圈 → 体系选股」横向对比才对得上。
    """

    return service.candidates(recent_days=recent_days, period=period)


@app.get("/api/market/success-rate")
def market_success_rate(
    symbol: Optional[str] = Query(None, description="留空返回全市场口径；传代码返回该股自身口径"),
    period: str = Query("day", description="周期，默认 day"),
) -> dict:
    """买点成功率。

    - 不传 `symbol`：全市场口径（来自关注圈历史，见 /api/watchlist/stats 的明细）
    - 传 `symbol`：**该股自身**口径（从 signals 表按时间线重建每次信号的结局）

    两者口径刻意不同：全市场看关注圈状态流转（入圈后是否走到二买），
    个股看该股历史上每一次信号的成败，用于回答「这只票我一买成功率几何」。
    """

    try:
        if symbol:
            return service.symbol_success_rate(symbol, period=period)
        return service.watchlist_stats()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/market/second-buy-confirmations")
def second_buy_confirmations(
    limit: int = Query(200, ge=1, le=2000, description="最多检查多少只关注圈标的"),
) -> dict:
    """p100 买点定律的次级别验证。

    原文：「**大级别的第二类买点由次一级别相应走势的第一类买点构成**……
    有了这个缠中说禅买点定律，**所有的买点都可以归结到第一类买点**。」

    只对关注圈内的标的做验证——次级别数据源仅保留约 9 个交易日，
    全市场逐只拉取既不现实也会触发限流。
    """

    try:
        return service.sublevel_second_buy_confirmations(limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/market/interval-nesting")
def interval_nesting(
    symbol: str = Query(..., description="股票代码，如 sh600519"),
    date: Optional[str] = Query(None, description="要确认的日线买点日 YYYY-MM-DD；省略则取本地最近一次日线买点"),
    kind: Optional[str] = Query(None, description="该买点类型 FIRST_BUY/SECOND_BUY/THIRD_BUY；省略则推断"),
) -> dict:
    """30 分钟区间套：用 m30 同位置结构确认日线买点（缠论区间套 + p100 买点定律泛化）。

    数据现实：westock 的 m30 仅保留约 9-10 个交易日，只有落在最近窗口内的日线买点
    才做得了区间套；更早的日线信号 m30 早已无留存，此时 available=True 但 confirmed=False。
    """

    try:
        return service.interval_nesting(symbol, anchor_date=date, anchor_kind=kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/market/pools")
def market_pools(
    days: int = Query(3, ge=1, le=20, description="最近 N 个交易日内触发的买点"),
    period: str = Query("day", description="周期，默认 day"),
) -> dict:
    try:
        return service.pools(days=days, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/market/risk")
def market_risk(
    days: int = Query(3, ge=1, le=20, description="最近 N 个交易日内触发的卖点"),
    period: str = Query("day", description="周期，默认 day"),
) -> dict:
    """卖点风险池：与买点池并列，供持仓风险提示使用。"""

    try:
        return service.risk_pool(days=days, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/watchlist")
def watchlist() -> list[dict]:
    """关注圈：只返回仍在跟踪中的状态，一买/二买失败已剔除出圈。"""

    return service.watchlist()


@app.get("/api/watchlist/stats")
def watchlist_stats() -> dict:
    """一买 / 二买历史成功率 + 关注圈出圈计数（全部来自本地关注圈记录）。"""

    return service.watchlist_stats()


@app.post("/api/watchlist/refresh")
def refresh_watchlist() -> dict:
    """用本地缓存的日线 K 线重评关注圈，不联网。

    二买失败只能在后续 K 线上观测到，而关注圈记录是历次扫描沉淀的，
    新增交易日后需要主动重评一次才会翻状态。
    """

    return service.refresh_watchlist()


@app.get("/api/signals/history")
def signals_history(
    kind: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    period: Optional[str] = Query(None, description="按周期过滤，缺省返回全部周期"),
) -> List[dict]:
    from chanpicker.core import SignalKind

    signal_kind = None
    if kind:
        try:
            signal_kind = SignalKind(kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"未知信号类型: {kind}") from exc
    try:
        normalized = normalize_period(period) if period else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = store.recent_signals(signal_kind, limit=limit, period=normalized)
    for row in rows:
        row["name"] = store.symbol_name(row["symbol"])
    return rows


@app.get("/")
def index():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="frontend/index.html 不存在")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

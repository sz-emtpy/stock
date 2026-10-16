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
def universe() -> list[dict]:
    return store.all_symbols()


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
def market_candidates(recent_days: int = Query(30, ge=1, le=500, description="只返回近 N 日触发的信号")) -> dict:
    return service.candidates(recent_days=recent_days)


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

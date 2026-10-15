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

from .provider import MarketDataError, resolve_westock_bin, run_westock
from .service import ScanService
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
    }


@app.get("/api/universe")
def universe() -> list[dict]:
    return store.all_symbols()


@app.get("/api/analyze")
def analyze(
    symbol: str = Query(..., description="股票代码，如 sh600519"),
    days: int = Query(400, ge=60, le=1000),
    refresh: bool = Query(False, description="忽略缓存强制拉取最新行情"),
) -> dict:
    try:
        return service.analyze_symbol(symbol, days=days, use_cache=not refresh)
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/market/scan")
def start_market_scan(scope: str = Query("hs300", description="扫描范围：hs300=沪深300成分股")) -> dict:
    try:
        return service.start_market_scan(scope=scope)
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/market/scan/status")
def market_scan_status(run_id: Optional[str] = None) -> dict:
    return service.scan_status(run_id)


@app.get("/api/market/candidates")
def market_candidates(recent_days: int = Query(30, ge=1, le=500, description="只返回近 N 日触发的信号")) -> dict:
    return service.candidates(recent_days=recent_days)


@app.get("/api/market/pools")
def market_pools(days: int = Query(3, ge=1, le=20, description="最近 N 个交易日内触发的买点")) -> dict:
    return service.pools(days=days)


@app.get("/api/watchlist")
def watchlist() -> list[dict]:
    return service.watchlist()


@app.get("/api/signals/history")
def signals_history(kind: Optional[str] = None, limit: int = Query(200, ge=1, le=1000)) -> List[dict]:
    from chanpicker.core import SignalKind

    signal_kind = None
    if kind:
        try:
            signal_kind = SignalKind(kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"未知信号类型: {kind}") from exc
    rows = store.recent_signals(signal_kind, limit=limit)
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

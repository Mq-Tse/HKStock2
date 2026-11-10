from __future__ import annotations

import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .providers import (
    AAStocksProvider,
    EastmoneyProvider,
    GelonghuiProvider,
    GreyMarketProvider,
    PhillipGreyProvider,
    ProviderError,
)
from .service import IPOService

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(os.getenv("HKSTOCK_DB", PROJECT_DIR / "data" / "hk_ipo.db"))
CODE_PATTERN = re.compile(r"\d{5}")
KLINE_TTL = {"intraday": 30, "day": 300, "week": 600, "month": 900}
KLINE_CACHE_SIZE = 300
_kline_cache: dict[str, tuple[float, dict]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(20.0, connect=10.0)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        )
    }
    async with httpx.AsyncClient(
        timeout=timeout, headers=headers, follow_redirects=True
    ) as client:
        service = IPOService(
            DB_PATH,
            EastmoneyProvider(client=client),
            GreyMarketProvider(
                AAStocksProvider(client=client),
                GreyMarketProvider(
                    GelonghuiProvider(client=client),
                    PhillipGreyProvider(client=client),
                ),
            ),
        )
        app.state.ipo_service = service
        service.start_refresh()
        try:
            yield
        finally:
            await service.shutdown()


app = FastAPI(title="港股新股收益追踪", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/ipos")
async def get_ipos(
    request: Request,
    q: str = Query(default="", max_length=80),
    year: str | None = Query(default=None, max_length=4),
    sort: str = Query(default="listing_date", max_length=30),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> dict:
    return request.app.state.ipo_service.get_ipos(q, year, sort, order)


@app.get("/api/stock/{code}")
async def get_stock(request: Request, code: str) -> dict:
    if not CODE_PATTERN.fullmatch(code):
        raise HTTPException(status_code=400, detail="无效港股代码，应为5位数字")
    item = request.app.state.ipo_service.get_stock(code)
    if item is None:
        raise HTTPException(status_code=404, detail="未找到该股票")
    return item


@app.get("/api/stock/{code}/kline")
async def get_stock_kline(
    request: Request,
    code: str,
    period: str = Query(default="day", pattern="^(intraday|day|week|month)$"),
) -> dict:
    if not CODE_PATTERN.fullmatch(code):
        raise HTTPException(status_code=400, detail="无效港股代码，应为5位数字")
    key = f"{code}:{period}"
    cached = _kline_cache.get(key)
    if cached and cached[0] > time.monotonic():
        payload = dict(cached[1])
        payload["cached"] = True
        return payload
    if cached:
        _kline_cache.pop(key, None)
    try:
        payload = await request.app.state.ipo_service.eastmoney.fetch_kline(
            code, period
        )
    except ProviderError as exc:
        raise HTTPException(
            status_code=502, detail=f"K线数据暂不可用：{exc}"
        ) from exc
    payload["cached"] = False
    if len(_kline_cache) >= KLINE_CACHE_SIZE:
        _kline_cache.pop(next(iter(_kline_cache)))
    _kline_cache[key] = (
        time.monotonic() + KLINE_TTL[period],
        dict(payload),
    )
    return payload


@app.get("/stock/{code}", include_in_schema=False)
async def stock_detail(code: str) -> FileResponse:
    if not CODE_PATTERN.fullmatch(code):
        raise HTTPException(status_code=404, detail="页面不存在")
    return FileResponse(STATIC_DIR / "stock.html")


@app.get("/compare", include_in_schema=False)
async def compare_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "compare.html")


@app.post("/api/refresh", status_code=202)
async def refresh(request: Request) -> dict:
    service: IPOService = request.app.state.ipo_service
    started = service.start_refresh(force=True)
    return {
        "started": started,
        "message": "已开始刷新" if started else "刷新正在进行中",
        "status": service.status,
    }


@app.get("/api/refresh/status")
async def refresh_status(request: Request) -> dict:
    return request.app.state.ipo_service.status

"""FastAPI application entry point: JSON API under /api, dashboard at /."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from .api import router, warm_caches
from .db import init_db
from .engine import engine

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

WEB_DIR = Path(os.getenv("JOBHUNT_WEB_DIR", Path(__file__).resolve().parent.parent / "web"))
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    for attempt in range(30):  # the database may still be starting up
        try:
            await init_db()
            break
        except (OSError, ConnectionError) as e:
            logging.getLogger(__name__).warning("database not ready (%s); retrying", e)
            await asyncio.sleep(2)
    else:
        raise RuntimeError("database unavailable")
    await engine.start()  # JOBHUNT_ROLE=api: config reload only; fetching runs in app.worker
    warm = asyncio.create_task(warm_caches())
    try:
        yield
    finally:
        warm.cancel()
        await engine.stop()


app = FastAPI(title="JobHunt API", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    else:  # tiny static files: always revalidate (ETag)
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(router)
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

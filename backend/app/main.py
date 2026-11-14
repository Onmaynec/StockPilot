"""StockPilot API v1. WMS state is committed only in PostgreSQL transactions."""
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .db import pool, request_id
from .security import client_key, rate_exceeded
from . import catalog, operations, inventory, account

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("stockpilot")
REQUESTS = Counter("stockpilot_http_requests_total", "HTTP requests", ["method", "path", "status"])
LATENCY = Histogram("stockpilot_http_duration_seconds", "Request duration", ["method", "path"])


@asynccontextmanager
async def lifespan(app):
    pool.open(wait=True)
    yield
    pool.close()


app = FastAPI(
    title="StockPilot WMS", version="0.8.0",
    description="Transactional multi-warehouse inventory API. Errors include requestId.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:8080").split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id"],
)
app.include_router(catalog.router, prefix="/api/v1")
app.include_router(account.router, prefix="/api/v1")
app.include_router(operations.router, prefix="/api/v1")
app.include_router(inventory.router, prefix="/api/v1")


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    supplied = request.headers.get("x-request-id", "")
    rid = supplied if 0 < len(supplied) <= 64 and all(
        ch.isalnum() or ch in "-_." for ch in supplied
    ) else str(uuid4())
    token = request_id.set(rid)
    begin = time.perf_counter()
    try:
        route_path = request.url.path
        source = client_key(request)
        limited = False
        if route_path.startswith("/api/"):
            # Atomic database counters work across API processes and replicas.
            from starlette.concurrency import run_in_threadpool
            if route_path == "/api/v1/auth/login":
                limited = await run_in_threadpool(rate_exceeded, "login:" + source, 30, 60)
            if not limited:
                limited = await run_in_threadpool(rate_exceeded, "api:" + source, 2000, 60)
        if limited:
            response = JSONResponse(
                {"code": "RATE_LIMIT", "message": "Превышен лимит запросов",
                 "requestId": rid, "details": None}, status_code=429,
                headers={"Retry-After": "60"}
            )
        else:
            response = await call_next(request)
    except Exception:
        logger.exception(json.dumps({"requestId": rid, "path": request.url.path, "error": "unhandled"}))
        response = JSONResponse(
            {"code": "INTERNAL", "message": "Внутренняя ошибка", "requestId": rid, "details": None},
            status_code=500,
        )
    finally:
        elapsed = time.perf_counter() - begin
        # Use route template, not raw URL, to avoid cardinality explosions.
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        status = response.status_code if "response" in locals() else 500
        REQUESTS.labels(request.method, path, str(status)).inc()
        LATENCY.labels(request.method, path).observe(elapsed)
        logger.info(json.dumps({"requestId": rid, "method": request.method, "path": path,
                                 "status": status, "durationMs": round(elapsed * 1000, 2)}))
        request_id.reset(token)
    response.headers["X-Request-Id"] = rid
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        payload = detail
    else:
        payload = {"code": "HTTP_ERROR", "message": str(detail), "details": None}
    return JSONResponse(
        {"code": payload.get("code", "HTTP_ERROR"),
         "message": payload.get("message", "Ошибка"),
         "requestId": request_id.get(), "details": payload.get("details")},
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def bad_request(request: Request, exc: RequestValidationError):
    return JSONResponse(
        {"code": "VALIDATION_ERROR", "message": "Некорректные входные данные",
         "requestId": request_id.get(), "details": [
             {"loc": list(item["loc"]), "msg": item["msg"]} for item in exc.errors()
         ]},
        status_code=400,
    )


@app.get("/health/live", tags=["System"])
def liveness():
    return {"status": "ok"}


@app.get("/health/ready", tags=["System"])
def readiness():
    with pool.connection() as c:
        c.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

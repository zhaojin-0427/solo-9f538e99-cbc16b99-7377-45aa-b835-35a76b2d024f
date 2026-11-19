"""FastAPI application: dangerous-goods custody chain API."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import config, services
from .database import connect, init_db
from .schemas import (
    AliasListResponse,
    AliasReplace,
    AliasView,
    BatchResponse,
    BatchSubmit,
    ChainResponse,
    WaybillCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    init_db()
    yield


app = FastAPI(
    title="空运危险品保管链 API",
    description=(
        "运单保管链（扫描交接 / 撤回 / 补报 / 更正）的修订裁决与可信前缀查询。"
        "同 ID 仅最高修订生效；同修订异文整批拒绝；失败保留旧链。"
        "简称字典（/api/aliases）支持一个简称对应多家承运人，整组替换后按新字典重新核链；"
        "接收候选多个时仅以紧接下一时刻的唯一扫描作旁证确认。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_conn() -> sqlite3.Connection:  # type: ignore[return]
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Error mapping: domain errors never leak 500s, and failed batches have
# already been rolled back by the service layer.
# --------------------------------------------------------------------------- #
@app.exception_handler(services.DomainConflict)
async def conflict_handler(_: Request, exc: services.DomainConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(services.DomainValidation)
async def not_found_handler(_: Request, exc: services.DomainValidation) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.exception_handler(ValidationError)
async def pydantic_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "validation_error", "detail": exc.errors()})


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/api/waybills", status_code=201, tags=["waybills"])
def create_waybill(
    body: WaybillCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    """创建运单并固定其起始保管方。"""
    return services.create_waybill(conn, body)


@app.get("/api/waybills", tags=["waybills"])
def list_waybills(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    rows = conn.execute(
        "SELECT id, origin_party, created_at FROM waybills ORDER BY id"
    ).fetchall()
    return {"waybills": [dict(r) for r in rows]}


@app.post(
    "/api/waybills/{waybill_id}/scans/batch",
    response_model=BatchResponse,
    tags=["scans"],
)
def submit_batch(
    waybill_id: str,
    batch: BatchSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> BatchResponse:
    """批量提交交接/撤回。

    * HANDOVER：scan_id、revision、ts_utc（带时区）、giver、receiver
    * WITHDRAWAL：仅 scan_id、revision（必须高于已知最高修订）
    * 同修订异文、过期修订、撤回未知 ID -> 409，整批回滚，旧链保留
    """
    return services.apply_batch(conn, waybill_id, batch)


@app.get(
    "/api/waybills/{waybill_id}/chain",
    response_model=ChainResponse,
    tags=["scans"],
)
def query_chain(
    waybill_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> ChainResponse:
    """返回可信连续交接前缀、首处断链原因与当前可确认保管方。"""
    return services.compute_chain(conn, waybill_id)


# --------------------------------------------------------------------------- #
# Abbreviation dictionary: one short custodian name may denote several carriers.
# All endpoints below are documented and tryable in /docs.
# --------------------------------------------------------------------------- #
@app.get("/api/aliases", response_model=AliasListResponse, tags=["aliases"])
def list_aliases(conn: sqlite3.Connection = Depends(get_conn)) -> AliasListResponse:
    """列出简称字典（每个简称对应的整组正式保管方）及字典修订号。"""
    return services.list_aliases(conn)


@app.get(
    "/api/aliases/{alias:path}",
    response_model=AliasView,
    tags=["aliases"],
)
def get_alias(
    alias: str, conn: sqlite3.Connection = Depends(get_conn)
) -> AliasView:
    """查询单个简称对应的正式保管方；未登记返回 404。"""
    return services.get_alias(conn, alias)


@app.put(
    "/api/aliases/{alias:path}",
    response_model=AliasView,
    tags=["aliases"],
)
def replace_alias(
    alias: str,
    body: AliasReplace,
    conn: sqlite3.Connection = Depends(get_conn),
) -> AliasView:
    """整组替换一个简称对应的正式保管方。

    空列表即注销该简称（之后该名称按未登记正式名处理）。替换在单个
    ``BEGIN IMMEDIATE`` 事务内完成，失败保留原字典与原核链结论；
    成功后的查链与批次回执均按新字典重新判定。
    """
    return services.replace_alias(conn, alias, body)


@app.delete(
    "/api/aliases/{alias:path}",
    response_model=AliasView,
    tags=["aliases"],
)
def delete_alias(
    alias: str, conn: sqlite3.Connection = Depends(get_conn)
) -> AliasView:
    """注销一个简称；未登记返回 404。"""
    return services.delete_alias(conn, alias)


# --------------------------------------------------------------------------- #
# Minimal console (the interactive /docs remains fully functional too).
# --------------------------------------------------------------------------- #
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")

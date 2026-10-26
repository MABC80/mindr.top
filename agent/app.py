"""Servidor HTTP del agente A2A Mindr.

Sirve la Agent Card, JSON-RPC message/send y GET/POST /discover.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from a2a import (
    JSONRPC_METHODS,
    build_agent_card,
    build_message,
    context_id_from,
    discovery_in_english,
    discovery_query,
    extract_text,
    format_discovery,
    jsonrpc_error,
    jsonrpc_result,
    wants_discovery,
)
from discover import DEFAULT_CACHE, USER_AGENT, discover_agents
from llm import LLM

log = logging.getLogger("mindr")

ORIGIN_REGEX = r"^https://(www\.)?mindr\.top$|^http://(localhost|127\.0\.0\.1)(:\d+)?$"


@dataclass(frozen=True)
class Settings:
    public_url: str = "https://mindr-a2a.onrender.com"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            public_url=os.environ.get("PUBLIC_URL", "https://mindr-a2a.onrender.com").strip()
            or "https://mindr-a2a.onrender.com",
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5").strip()
            or "claude-haiku-5-5",
        )


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def create_app(settings: Settings | None = None, llm: LLM | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    llm = llm or LLM(settings.anthropic_api_key, settings.anthropic_model)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain;q=0.8"},
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        try:
            yield
        finally:
            await app.state.http.aclose()

    app = FastAPI(title="Mindr A2A", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.llm = llm
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=ORIGIN_REGEX,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def root() -> Response:
        base = settings.public_url.rstrip("/")
        return Response(
            content=json_bytes(
                {
                    "name": "Mindr",
                    "agentCard": f"{base}/.well-known/agent-card.json",
                    "health": f"{base}/health",
                    "discover": f"{base}/discover",
                }
            ),
            media_type="application/json",
        )

    @app.get("/.well-known/agent-card.json")
    @app.get("/.well-known/agent.json")
    async def agent_card(request: Request) -> Response:
        body = json_bytes(build_agent_card(settings.public_url))
        etag = hashlib.sha256(body).hexdigest()[:16]
        headers = {"ETag": etag, "Cache-Control": "public, max-age=300"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content=body, media_type="application/json", headers=headers)

    @app.get("/discover")
    async def discover_get(request: Request) -> Response:
        query = request.query_params.get("q", "")
        return await _discover_response(request, query)

    @app.post("/discover")
    async def discover_post(request: Request) -> Response:
        query = request.query_params.get("q", "")
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                payload = await request.json()
            except Exception:
                return JSONResponse({"error": "JSON inválido"}, status_code=400)
            if isinstance(payload, dict) and isinstance(payload.get("q"), str):
                query = payload["q"]
        return await _discover_response(request, query)

    async def _discover_response(request: Request, query: str) -> Response:
        try:
            result = await discover_agents(
                public_url=settings.public_url,
                query=query or "",
                client=request.app.state.http,
                proposer=llm.propose_candidates if llm.configured() else None,
                cache=DEFAULT_CACHE,
            )
        except Exception:
            log.exception("discovery failed")
            return JSONResponse(
                {"agents": [], "count": 0, "error": "No se pudo completar el descubrimiento."},
                status_code=502,
            )
        return Response(
            content=json_bytes(result.as_dict()),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/")
    @app.post("/a2a")
    async def rpc(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(jsonrpc_error(None, -32700, "Parse error"))
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or "method" not in payload:
            req_id = payload.get("id") if isinstance(payload, dict) else None
            return JSONResponse(jsonrpc_error(req_id, -32600, "Invalid Request"))
        if "id" not in payload:
            return Response(status_code=204)
        req_id = payload.get("id")
        if isinstance(req_id, bool) or not isinstance(req_id, (str, int, float, type(None))):
            return JSONResponse(jsonrpc_error(None, -32600, "Invalid Request"))
        method = payload.get("method")
        if method not in JSONRPC_METHODS:
            return JSONResponse(jsonrpc_error(req_id, -32601, "Method not found"))
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        text = extract_text(params)
        if not text:
            return JSONResponse(jsonrpc_error(req_id, -32602, "Invalid params"))
        if wants_discovery(text, params):
            try:
                found = await discover_agents(
                    public_url=settings.public_url,
                    query=discovery_query(text),
                    client=request.app.state.http,
                    proposer=llm.propose_candidates if llm.configured() else None,
                    cache=DEFAULT_CACHE,
                )
            except Exception:
                log.exception("discovery failed")
                reply = "No se pudo completar el descubrimiento."
            else:
                reply = format_discovery(
                    [agent.as_dict() for agent in found.agents],
                    english=discovery_in_english(text),
                )
        else:
            reply = await llm.answer(text)
        message = build_message(reply, context_id_from(params))
        return JSONResponse(jsonrpc_result(req_id, str(method), message))

    return app


app = create_app()

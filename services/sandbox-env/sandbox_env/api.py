"""FastAPI surface: the gym REST contract (PLAN.md 2.2) with the MCP server mounted at /mcp.

Route map
    POST   /episodes                     reset  -> ResetResponse
    GET    /episodes                     ops listing (id, scenario, created_at, sandbox alive?)
    POST   /episodes/sweep               terminate episodes older than EPISODE_TTL_S
    GET    /episodes/{id}                observe -> ObserveResponse
    POST   /episodes/{id}/evaluate       grade  -> EvaluateResponse
    POST   /episodes/{id}/interruptions  the harness never got this call's response -> LedgerEntry
    DELETE /episodes/{id}                terminate the sandbox
    GET    /scenarios                    public catalogue
    GET    /health                       Health (has_provider_key MUST be false here)
    POST   /mcp                          streamable-HTTP MCP (episode via X-Faultline-Episode)

A failure of the *sandbox* (terminated, unreachable, helper crashed) answers 503 with
`{"detail": …, "code": "ESANDBOX"}` on every route, so a caller can tell "the gym is broken" from
"your episode's sandbox is gone" without parsing prose. `EINTERNAL` is reserved for bugs.

Two invariants this file is responsible for:

* **No provider secret.** This function is deployed without `anthropic-secret`; `/health` reports
  `has_provider_key` so the boundary is observable from outside, and startup logs it loudly.
* **Every request is logged once** with a request id, status and duration, in the same JSON-lines
  shape every other Faultline service uses. The request id flows into the MCP tool logs through
  `faultline_common.log.ctx_request_id`, so one id ties an HTTP call to the sandbox exec it caused.

Route handlers are plain `def` on purpose: the Modal Sandbox client and `modal.Dict` are blocking,
so FastAPI runs them in its threadpool instead of stalling the event loop for every other request.
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from faultline_common.log import ctx_episode_id, ctx_request_id, ctx_step, get_logger
from faultline_common.schemas import (
    EvaluateResponse,
    Health,
    InterruptionReport,
    LedgerEntry,
    ObserveResponse,
    ResetRequest,
    ResetResponse,
    Scenario,
)

from . import __version__, episodes, grader, mcp_tools, scenarios
from .util import new_id
from .workspace import WorkspaceError

log = get_logger("sandbox-env")

PROVIDER_KEY_ENV = "ANTHROPIC_API_KEY"
REQUEST_ID_HEADER = "X-Request-Id"
EPISODE_HEADER = "X-Faultline-Episode"
MCP_PATH = "/mcp"


class StripMcpTrailingSlash:
    """Serve `/mcp` and `/mcp/` from the same route, with no redirect either way.

    The MCP streamable-HTTP client normalises the endpoint to `/mcp/`, which FastAPI answers with a
    307 to `/mcp`. Clients follow it, so everything works — but it doubles the request count on the
    hottest path in the system (every single tool call). Rewriting the path in front of the router
    costs one string comparison and removes a whole round trip per step.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == MCP_PATH + "/":
            scope = dict(scope, path=MCP_PATH, raw_path=MCP_PATH.encode())
        await self.app(scope, receive, send)


def has_provider_key() -> bool:
    return PROVIDER_KEY_ENV in os.environ


def sandbox_unavailable(detail: str) -> JSONResponse:
    """The one 503 shape for "the sandbox itself failed" (docs/error-taxonomy.md real/sandbox).

    Carrying the code in the body matters because the harness has to classify this into an
    `ErrorClass` and decide the run's status: `ESANDBOX` means the episode is unrecoverable and the
    run is `interrupted`/`unevaluated`, not that the agent did anything wrong.
    """
    return JSONResponse({"detail": detail, "code": "ESANDBOX"}, status_code=503)


def create_app() -> FastAPI:
    # stateless_http=True: every MCP request is self-contained, so any container can serve it.
    mcp_app = mcp_tools.mcp.http_app(
        path=MCP_PATH,
        transport="http",
        stateless_http=True,
        host_origin_protection=False,  # the public URL is the Modal one; no DNS-rebinding guard
    )

    app = FastAPI(
        title="faultline-sandbox-env",
        version=__version__,
        summary="Fault-injecting gym + MCP tool server over a Modal Sandbox",
        lifespan=mcp_app.router.lifespan_context,
    )

    # ---- middleware (added innermost-first: CORS ends up outermost so preflights never 500)

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get(REQUEST_ID_HEADER) or new_id("req")
        ctx_request_id.set(rid)
        ctx_episode_id.set(request.headers.get(EPISODE_HEADER))
        ctx_step.set(None)
        t0 = time.perf_counter()
        # NB: Logger.debug/info already own the `lvl` argument — passing lvl= here is a TypeError
        # on every single request.
        log.debug("http.request", f"{request.method} {request.url.path}", method=request.method,
                  path=request.url.path)
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - log then let FastAPI's handler answer
            log.error("http.error", f"{type(exc).__name__}: {exc}", method=request.method,
                      path=request.url.path, dur_ms=int((time.perf_counter() - t0) * 1000))
            raise
        response.headers[REQUEST_ID_HEADER] = rid
        log.info("http.response", f"{request.method} {request.url.path} -> {response.status_code}",
                 method=request.method, path=request.url.path, status=response.status_code,
                 dur_ms=int((time.perf_counter() - t0) * 1000))
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # outermost: runs before routing, so /mcp/ never becomes a redirect
    app.add_middleware(StripMcpTrailingSlash)

    # ---- gym REST

    @app.get("/health", response_model=Health)
    def health() -> Health:
        return Health(
            svc="sandbox-env",
            ok=True,
            version=__version__,
            has_provider_key=has_provider_key(),
            detail={
                "scenarios": [s.id for s in scenarios.list_scenarios()],
                "tools": list(mcp_tools.TOOL_NAMES),
                "mcp_path": "/mcp",
                "episodes_dict": episodes.EPISODES_DICT,
                "episode_ttl_s": episodes.EPISODE_TTL_S,
            },
        )

    @app.get("/scenarios", response_model=list[Scenario])
    def get_scenarios() -> list[Scenario]:
        return scenarios.list_scenarios()

    # 200, not 201: PLAN.md 2.2 does not pin a status and two other services are being written
    # against this route in parallel, so the least surprising code wins.
    @app.post("/episodes", response_model=ResetResponse)
    def post_episodes(body: ResetRequest) -> Any:
        try:
            return episodes.reset(body.scenario_id, body.seed)
        except scenarios.ScenarioNotFound:
            raise HTTPException(404, f"unknown scenario {body.scenario_id!r}") from None
        except WorkspaceError as exc:
            return sandbox_unavailable(f"could not provision a sandbox: {exc}")

    # Ops route (not part of the gym contract): what is running, how old it is, and whether its
    # sandbox is still alive. Cheap answer by default is NOT an option — "alive?" is the whole
    # question — so it polls, bounded by `limit`; `?probe=false` gives the Dict view with no RPCs.
    @app.get("/episodes")
    def list_episodes(probe: bool = True, limit: int = 200) -> dict[str, Any]:
        return episodes.list_episodes(probe=probe, limit=max(1, min(limit, 500)))

    # Manual trigger for the same sweep that runs on every reset. Handy in a demo, and it makes the
    # guardrail testable from outside without waiting for someone to press Run.
    @app.post("/episodes/sweep")
    def sweep_episodes(ttl_s: int | None = None) -> dict[str, Any]:
        return episodes.sweep(ttl_s=ttl_s)

    @app.get("/episodes/{episode_id}", response_model=ObserveResponse)
    def get_episode(episode_id: str) -> Any:
        ctx_episode_id.set(episode_id)
        try:
            return episodes.observe(episode_id)
        except episodes.EpisodeNotFound:
            raise HTTPException(404, f"unknown episode {episode_id!r}") from None
        except WorkspaceError as exc:
            return sandbox_unavailable(f"sandbox unavailable: {exc}")

    @app.post("/episodes/{episode_id}/evaluate", response_model=EvaluateResponse)
    def post_evaluate(episode_id: str) -> Any:
        ctx_episode_id.set(episode_id)
        try:
            return grader.evaluate(episode_id)
        except episodes.EpisodeNotFound:
            raise HTTPException(404, f"unknown episode {episode_id!r}") from None
        except WorkspaceError as exc:
            return sandbox_unavailable(f"sandbox unavailable: {exc}")

    # The harness reporting a REAL interruption (its worker died, or it cancelled the request)
    # against a call this service may well have completed. Never touches the sandbox: it must keep
    # working after the sandbox is gone, which is precisely when it is most needed.
    @app.post("/episodes/{episode_id}/interruptions", response_model=LedgerEntry)
    def post_interruption(episode_id: str, body: InterruptionReport, response: Response) -> Any:
        ctx_episode_id.set(episode_id)
        try:
            entry, matched = episodes.report_interruption(episode_id, body)
        except episodes.EpisodeNotFound:
            raise HTTPException(404, f"unknown episode {episode_id!r}") from None
        # the body is the row itself; whether we found it or had to annotate is ops information
        response.headers["X-Faultline-Matched"] = "true" if matched else "false"
        return entry

    @app.delete("/episodes/{episode_id}")
    def delete_episode(episode_id: str) -> dict[str, Any]:
        ctx_episode_id.set(episode_id)
        found = episodes.delete(episode_id)
        if not found:
            raise HTTPException(404, f"unknown episode {episode_id!r}")
        return {"terminated": True, "episode_id": episode_id}

    @app.get("/")
    def index() -> dict[str, Any]:
        return {
            "svc": "sandbox-env",
            "version": __version__,
            "mcp": "/mcp",
            "routes": ["/health", "/scenarios", "/episodes", "/episodes/sweep", "/episodes/{id}",
                       "/episodes/{id}/evaluate", "/episodes/{id}/interruptions"],
        }

    @app.exception_handler(episodes.EpisodeNotFound)
    def _episode_missing(request: Request, exc: Exception) -> Response:  # pragma: no cover
        return JSONResponse({"detail": f"unknown episode: {exc}"}, status_code=404)

    # Backstop: any WorkspaceError that escapes a route (a sweep on the reset path, a future route)
    # must still answer with the ESANDBOX shape rather than a bare 500.
    @app.exception_handler(WorkspaceError)
    def _sandbox_gone(request: Request, exc: Exception) -> Response:  # pragma: no cover
        return sandbox_unavailable(f"sandbox unavailable: {exc}")

    # MCP last: Mount("/") matches anything the routes above did not.
    app.mount("/", mcp_app)

    log.info(
        "svc.start",
        "sandbox-env api ready",
        version=__version__,
        has_provider_key=has_provider_key(),
        scenarios=[s.id for s in scenarios.list_scenarios()],
        tools=list(mcp_tools.TOOL_NAMES),
    )
    if has_provider_key():  # should be impossible: this function is deployed without the secret
        log.error("svc.secret_leak", f"{PROVIDER_KEY_ENV} is present in sandbox-env; it must not be")

    return app

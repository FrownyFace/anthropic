"""Browser-facing API for the harness (Modal function `api`, no provider secret).

    GET    /health            includes has_provider_key (MUST be false here) + detail.store
    GET    /scenarios         proxy to sandbox-env (the browser never talks to the gym directly)
    GET    /me                upsert this browser's anonymous user -> {user_id, conversations}
    GET    /conversations     this user's conversations, newest first, archived excluded
    POST   /conversations     {scenario_id, title?} -> Conversation
    GET    /conversations/{id}   conversation + runs + the persisted transcript (messages/blocks)
    PATCH  /conversations/{id}   {title?, archived?} -> Conversation
    DELETE /conversations/{id}   soft archive -> {archived: true}
    POST   /conversations/{id}/runs  {model?, seed?, max_steps?, task_prompt?} -> {run_id, conversation_id}
    POST   /runs              validate + create a conversation + a queued run, spawn `run_episode`
    GET    /runs              last 50 runs (scoped to the caller when X-Faultline-User is sent)
    GET    /runs/{id}         the whole RunRecord (what we also save as evidence)
    GET    /runs/{id}/events  SSE tail of the event stream, resumable via Last-Event-ID

Identity (PLAN.md §2.9.1) is scoping, not authentication: `X-Faultline-User: u_<uuid4>` is validated,
upserted and used to scope conversations and runs; a mismatch is 404, never 403 (no existence
oracle). Requests with no header still work — they are attributed to one well-known anonymous user
so the CLI and the smoke scripts keep running unchanged.

Why SSE and not "run it in the request": Modal caps a web request at 150 s and then 303-redirects,
which breaks CORS. So the run happens in a spawned function and the browser tails events out of the
Store; each stream closes itself at 110 s and the client reconnects with Last-Event-ID.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, Callable, Iterable

from fastapi import FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from faultline_common.log import ctx_request_id, ctx_run_id, get_logger
from faultline_common.schemas import HarnessFault

from . import config
from .events import now_iso
from .gym_client import GymClient, GymError
from .store import ANON_USER_ID, TERMINAL_STATUSES, RunStore, valid_user_id

log = get_logger(config.SVC)

TERMINAL = TERMINAL_STATUSES
VERSION = "0.1.0"
USER_HEADER = "x-faultline-user"
# Routes that must never pay for a user upsert: health checks and the long-lived SSE tail.
NO_UPSERT_SUFFIXES = ("/health", "/scenarios", "/events")


def new_run_id() -> str:
    return f"r_{uuid.uuid4().hex[:12]}"


def default_spawn(run_id: str, req: dict[str, Any]) -> str | None:
    """Spawn the real Modal function (used when the API runs outside modal_app.py)."""
    import modal

    fn = modal.Function.from_name(
        config.APP_NAME, "run_episode", environment_name=config.modal_environment()
    )
    call = fn.spawn(run_id, req)
    return getattr(call, "object_id", None)


def sse_frame(event: dict[str, Any]) -> str:
    return f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"


def parse_last_event_id(header: str | None, after: int | None, query: str | int | None = None) -> int:
    """-1 means "send everything".

    Precedence: `Last-Event-ID` header, then `?last_event_id=`, then `?after=`. The browser cannot
    set headers on an EventSource, and apps/web re-opens the stream itself, so it sends the resume
    point as a query param under BOTH spellings; honour all three (PLAN.md §2.3, docs/web-handoff.md).
    """
    for candidate in (header, query, after):
        if candidate is None or candidate == "":
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return -1


def user_of(request: Request) -> str | None:
    """The validated caller id, or None when the request carried no usable X-Faultline-User."""
    return getattr(request.state, "user_id", None)


def owns(record: dict[str, Any] | None, user_id: str | None) -> bool:
    """Ownership check for runs. Anonymous runs (CLI, smoke scripts) stay readable by anyone."""
    if record is None:
        return False
    owner = record.get("user_id")
    if user_id is None or owner in (None, "", ANON_USER_ID):
        return True
    return owner == user_id


def create_app(
    *,
    store: RunStore | None = None,
    gym_factory: Callable[[], Any] | None = None,
    spawn: Callable[[str, dict[str, Any]], Any] | None = None,
) -> FastAPI:
    """Build the ASGI app. Everything external is injectable so tests need no Modal and no network."""
    # Secret boundary (PLAN.md §2.1/§2.4): the browser-facing function must not hold the provider
    # key. Fail loudly at startup rather than serving traffic from a process that leaked it.
    if "ANTHROPIC_API_KEY" in os.environ:
        raise RuntimeError(
            "secret boundary violated: ANTHROPIC_API_KEY is visible to the harness `api` function; "
            "attach the Anthropic secret to `run_episode` only"
        )
    runs = store or RunStore()
    make_gym = gym_factory or (lambda: GymClient())
    spawn_fn = spawn or default_spawn

    app = FastAPI(title="faultline-harness", version=VERSION)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.middleware("http")
    async def identity_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """X-Faultline-User -> validate -> upsert -> request.state.user_id (PLAN.md §2.9.1)."""
        raw = (request.headers.get(USER_HEADER) or "").strip()
        valid = valid_user_id(raw)
        request.state.user_id = raw if valid else None
        request.state.user_id_invalid = bool(raw and not valid)
        path = request.url.path
        if valid and not path.endswith(NO_UPSERT_SUFFIXES):
            try:
                await asyncio.to_thread(runs.upsert_user, raw, request.headers.get("user-agent"))
            except Exception as exc:  # noqa: BLE001 - identity is a convenience, never a gate
                log.warn("identity.upsert_failed", f"{type(exc).__name__}: {exc}", user_id=raw)
        return await call_next(request)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:8]}"
        ctx_request_id.set(rid)
        ctx_run_id.set(None)
        t0 = time.perf_counter()
        log.info("http.request", f"{request.method} {request.url.path}", method=request.method,
                 path=request.url.path, query=str(request.url.query) or None)
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - log then re-raise as a 500
            log.error("http.error", f"{type(exc).__name__}: {exc}", method=request.method,
                      path=request.url.path, dur_ms=int((time.perf_counter() - t0) * 1000))
            raise
        response.headers["X-Request-Id"] = rid
        log.info("http.response", f"{request.method} {request.url.path}", status=response.status_code,
                 path=request.url.path, dur_ms=int((time.perf_counter() - t0) * 1000))
        return response

    def need_user(request: Request) -> tuple[str | None, JSONResponse | None]:
        uid = user_of(request)
        if uid:
            return uid, None
        detail = ("X-Faultline-User is malformed (want u_<uuid4>)"
                  if getattr(request.state, "user_id_invalid", False)
                  else "X-Faultline-User header is required for this route")
        return None, JSONResponse(status_code=400, content={"error": detail})

    # ------------------------------------------------------------------ health
    @app.get("/health")
    async def health() -> dict[str, Any]:
        reachable: bool | None = None
        detail: dict[str, Any] = {}
        gym = make_gym()
        try:
            info = await asyncio.to_thread(gym.health)
            reachable = bool(info)
            detail["sandbox_env"] = {k: info.get(k) for k in ("svc", "ok", "version") if k in info}
        except Exception as exc:  # noqa: BLE001 - unreachable is a normal, reportable state
            reachable = False
            detail["sandbox_env_error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            try:
                gym.close()
            except Exception:  # noqa: BLE001 # pragma: no cover
                pass
        detail["sandbox_env_reachable"] = reachable
        try:
            detail["store"] = await asyncio.to_thread(runs.health)
        except Exception as exc:  # noqa: BLE001 - the Store being down is reportable, not fatal
            detail["store"] = {"ok": False, "last_checkpoint_at": None,
                               "error": f"{type(exc).__name__}: {exc}"[:300]}
        # The secret is attached to `run_episode` only; if this is ever true, the boundary leaked.
        has_key = "ANTHROPIC_API_KEY" in os.environ
        if has_key:
            log.error("health.secret_boundary", "ANTHROPIC_API_KEY visible to the API function")
        return {
            "svc": "harness",
            "ok": True,
            "version": VERSION,
            "has_provider_key": has_key,
            "model_default": config.model_default(),
            "sandbox_env_url": config.sandbox_env_url(),
            "detail": detail,
        }

    # ------------------------------------------------------------------ scenarios
    @app.get("/scenarios")
    async def scenarios() -> Any:
        gym = make_gym()
        try:
            items = await asyncio.to_thread(gym.scenarios)
            return {"scenarios": items}
        except GymError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": "sandbox-env unreachable", "detail": str(exc)[:500],
                         "sandbox_env_url": config.sandbox_env_url()},
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={"error": "sandbox-env unreachable", "detail": f"{type(exc).__name__}: {exc}"[:500]},
            )
        finally:
            try:
                gym.close()
            except Exception:  # noqa: BLE001 # pragma: no cover
                pass

    async def scenario_catalogue() -> list[dict[str, Any]]:
        """Live scenario list, or [] when the gym is unreachable (a blip must not 502 the Run button)."""
        gym = make_gym()
        try:
            return list(await asyncio.to_thread(gym.scenarios) or [])
        except Exception as exc:  # noqa: BLE001
            log.warn("scenarios.unavailable", f"{type(exc).__name__}: {exc}")
            return []
        finally:
            try:
                gym.close()
            except Exception:  # noqa: BLE001 # pragma: no cover
                pass

    # ------------------------------------------------------------------ identity
    @app.get("/me")
    async def me(request: Request) -> Any:
        uid, err = need_user(request)
        if err is not None:
            return err
        assert uid is not None
        user = await asyncio.to_thread(runs.upsert_user, uid, request.headers.get("user-agent"))
        return {"user_id": uid, "conversations": int((user or {}).get("conversations") or 0)}

    # ------------------------------------------------------------------ conversations
    @app.get("/conversations")
    async def list_conversations(request: Request, limit: int = Query(50, ge=1, le=200)) -> Any:
        uid, err = need_user(request)
        if err is not None:
            return err
        assert uid is not None
        return await asyncio.to_thread(runs.list_conversations, uid, limit)

    @app.post("/conversations")
    async def create_conversation(request: Request, body: dict[str, Any]) -> Any:
        uid, err = need_user(request)
        if err is not None:
            return err
        assert uid is not None
        scenario_id = str(body.get("scenario_id") or "").strip()
        if not scenario_id:
            return JSONResponse(status_code=400, content={"error": "scenario_id is required"})
        catalogue = await scenario_catalogue()
        known = {s.get("id") for s in catalogue if isinstance(s, dict)}
        if known and scenario_id not in known:
            return JSONResponse(status_code=400, content={
                "error": f"unknown scenario {scenario_id!r}", "known": sorted(k for k in known if k)})
        title = body.get("title") or _scenario_title(catalogue, scenario_id)
        return await asyncio.to_thread(
            runs.create_conversation, uid, scenario_id, title, request.headers.get("user-agent")
        )

    @app.get("/conversations/{conversation_id}")
    async def get_conversation(request: Request, conversation_id: str) -> Any:
        uid, err = need_user(request)
        if err is not None:
            return err
        detail = await asyncio.to_thread(runs.get_conversation, conversation_id, uid)
        if detail is None:
            return JSONResponse(status_code=404,
                                content={"error": f"unknown conversation {conversation_id!r}"})
        return detail

    @app.patch("/conversations/{conversation_id}")
    async def patch_conversation(request: Request, conversation_id: str, body: dict[str, Any]) -> Any:
        uid, err = need_user(request)
        if err is not None:
            return err
        title = body.get("title")
        archived = body.get("archived")
        conv = await asyncio.to_thread(
            runs.update_conversation, conversation_id, uid,
            title=str(title) if isinstance(title, str) else None,
            archived=bool(archived) if archived is not None else None,
        )
        if conv is None:
            return JSONResponse(status_code=404,
                                content={"error": f"unknown conversation {conversation_id!r}"})
        return conv

    @app.delete("/conversations/{conversation_id}")
    async def archive_conversation(request: Request, conversation_id: str) -> Any:
        uid, err = need_user(request)
        if err is not None:
            return err
        out = await asyncio.to_thread(runs.delete_conversation, conversation_id, uid)
        if out is None:
            return JSONResponse(status_code=404,
                                content={"error": f"unknown conversation {conversation_id!r}"})
        return {"archived": True}

    @app.post("/conversations/{conversation_id}/runs")
    async def start_conversation_run(request: Request, conversation_id: str,
                                     body: dict[str, Any] | None = None) -> Any:
        uid, err = need_user(request)
        if err is not None:
            return err
        detail = await asyncio.to_thread(runs.get_conversation, conversation_id, uid)
        if detail is None:
            return JSONResponse(status_code=404,
                                content={"error": f"unknown conversation {conversation_id!r}"})
        conversation = detail.get("conversation") or {}
        req = dict(body or {})
        out = await _start_run(
            scenario_id=str(conversation.get("scenario_id") or ""),
            body=req,
            user_id=uid,
            conversation_id=conversation_id,
        )
        if isinstance(out, JSONResponse):
            return out
        return JSONResponse(status_code=202, content=out)

    # ------------------------------------------------------------------ runs
    async def _start_run(*, scenario_id: str, body: dict[str, Any], user_id: str | None,
                         conversation_id: str | None) -> Any:
        if not scenario_id:
            return JSONResponse(status_code=400, content={"error": "scenario_id is required"})
        model = body.get("model") or config.model_default()
        if model not in config.MODEL_ALLOWLIST:
            return JSONResponse(
                status_code=400,
                content={"error": f"model {model!r} is not allowed", "allowed": config.MODEL_ALLOWLIST},
            )
        seed = body.get("seed")
        max_steps = body.get("max_steps")
        task_prompt: str | None = body.get("task_prompt") or None

        # Optional, additive (PLAN.md §2.11): let a caller inflict a REAL interruption on this run
        # even when the scenario declares none — `worker_crash` kills the worker mid-call,
        # `transport_abort` cancels the in-flight request. It is how `transport_abort` is proved
        # live (no bundled scenario uses it) and how an operator reproduces a resume on demand.
        # Validated against schemas.HarnessFault so a typo is a 400 here, not a dead worker later.
        harness_faults: list[dict[str, Any]] = []
        if body.get("harness_faults"):
            try:
                harness_faults = [HarnessFault.model_validate(f).model_dump()
                                  for f in body["harness_faults"]]
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(status_code=400,
                                    content={"error": "invalid harness_faults",
                                             "detail": str(exc)[:400]})

        # Validate the scenario against the live catalogue; if the gym is unreachable we let the run
        # through and let reset fail loudly inside the episode.
        catalogue = await scenario_catalogue()
        if catalogue:
            known = {s.get("id") for s in catalogue if isinstance(s, dict)}
            if scenario_id not in known:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"unknown scenario {scenario_id!r}",
                             "known": sorted(k for k in known if k)},
                )
            match = next((s for s in catalogue if s.get("id") == scenario_id), {})
            if not max_steps and isinstance(match.get("max_steps"), int):
                max_steps = match["max_steps"]
            if not task_prompt and isinstance(match.get("task_prompt"), str):
                task_prompt = match["task_prompt"]

        max_steps = int(max_steps or config.DEFAULT_MAX_STEPS)
        max_steps = max(1, min(max_steps, config.HARD_MAX_STEPS))
        owner = user_id or ANON_USER_ID

        if not conversation_id:
            conversation = await asyncio.to_thread(
                runs.create_conversation, owner, scenario_id, _scenario_title(catalogue, scenario_id)
            )
            conversation_id = str((conversation or {}).get("id") or "")

        run_id = new_run_id()
        ctx_run_id.set(run_id)
        record = {
            "run_id": run_id,
            "status": "queued",
            "scenario_id": scenario_id,
            "model": model,
            "seed": seed,
            "max_steps": max_steps,
            "episode_id": None,
            "task_prompt": task_prompt,
            "created_at": now_iso(),
            "finished_at": None,
            "events": [],
            "evaluation": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "error": None,
            "conversation_id": conversation_id,
            "user_id": owner,
        }
        await asyncio.to_thread(runs.create, record)

        req = {"scenario_id": scenario_id, "model": model, "seed": seed, "max_steps": max_steps,
               "conversation_id": conversation_id, "user_id": owner}
        if task_prompt:
            req["task_prompt"] = task_prompt
        if harness_faults:
            req["harness_faults"] = harness_faults
        try:
            call_id = await asyncio.to_thread(spawn_fn, run_id, req)
        except Exception as exc:  # noqa: BLE001 - the record exists, so fail it visibly
            log.error("spawn.failed", f"{type(exc).__name__}: {exc}", run_id=run_id)
            error = f"spawn failed: {type(exc).__name__}: {exc}"
            await asyncio.to_thread(runs.update, run_id, status="error", error=error,
                                    finished_at=now_iso())
            return JSONResponse(status_code=500, content={"error": error, "run_id": run_id})

        log.info("run.spawned", "run_episode spawned", run_id=run_id, scenario_id=scenario_id,
                 model=model, max_steps=max_steps, conversation_id=conversation_id, user_id=owner,
                 call_id=call_id if isinstance(call_id, str) else None)
        return {"run_id": run_id, "conversation_id": conversation_id, "status": "queued",
                "scenario_id": scenario_id, "model": model, "max_steps": max_steps, "seed": seed}

    @app.post("/runs")
    async def create_run(request: Request, body: dict[str, Any]) -> Any:
        out = await _start_run(
            scenario_id=str(body.get("scenario_id") or "").strip(),
            body=body,
            user_id=user_of(request),
            conversation_id=None,
        )
        return out

    @app.get("/runs")
    async def list_runs(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        # Scoped to the caller when the browser sends its id; unscoped for the CLI/evidence scripts.
        items = await asyncio.to_thread(runs.list_runs, limit, user_of(request))
        return {"runs": items}

    @app.get("/runs/{run_id}")
    async def get_run(request: Request, run_id: str) -> Any:
        ctx_run_id.set(run_id)
        record = await asyncio.to_thread(runs.get, run_id)
        if not owns(record, user_of(request)):
            return JSONResponse(status_code=404, content={"error": f"unknown run {run_id!r}"})
        return record

    @app.get("/runs/{run_id}/events")
    async def stream_events(
        request: Request,
        run_id: str,
        after: int | None = Query(None),
        last_event_id_q: str | None = Query(None, alias="last_event_id"),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> Any:
        ctx_run_id.set(run_id)
        head = await asyncio.to_thread(runs.events_after, run_id, 2**62, 1)
        if not (head or {}).get("exists"):
            return JSONResponse(status_code=404, content={"error": f"unknown run {run_id!r}"})
        uid = user_of(request)
        if uid:
            record = await asyncio.to_thread(runs.get, run_id)
            if not owns(record, uid):
                return JSONResponse(status_code=404, content={"error": f"unknown run {run_id!r}"})
        start_after = parse_last_event_id(last_event_id, after, last_event_id_q)
        log.info("sse.open", "client attached", run_id=run_id, after=start_after)
        return StreamingResponse(
            _event_stream(runs, run_id, start_after),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    app.state.store = runs
    return app


def _scenario_title(catalogue: list[dict[str, Any]], scenario_id: str) -> str:
    for item in catalogue or []:
        if isinstance(item, dict) and item.get("id") == scenario_id:
            title = item.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    return scenario_id


async def _event_stream(runs: RunStore, run_id: str, after: int) -> Iterable[str]:
    """Replay everything after `after`, then poll Store.events_after until the run ends or the
    110 s window closes (Modal kills a web request at 150 s; the client reconnects with
    Last-Event-ID and `reason: window` tells it this is NOT the end of the run)."""
    t0 = time.monotonic()
    last_id = after
    last_keepalive = t0
    sent = 0
    status: str | None = None
    yield "retry: 1000\n\n"
    try:
        while True:
            page = await asyncio.to_thread(runs.events_after, run_id, last_id)
            for event in (page or {}).get("events") or []:
                if not isinstance(event, dict) or event.get("id") is None:
                    continue
                if event["id"] > last_id:
                    last_id = event["id"]
                    sent += 1
                    yield sse_frame(event)
            status = (page or {}).get("status")
            now = time.monotonic()
            if status in TERMINAL:
                yield (
                    "event: done\ndata: "
                    + json.dumps({"reason": "finished", "status": status, "last_event_id": last_id,
                                  "run_id": run_id})
                    + "\n\n"
                )
                log.info("sse.close", "run finished", run_id=run_id, status=status, sent=sent,
                         dur_ms=int((now - t0) * 1000))
                return
            if now - t0 >= config.SSE_WINDOW_S:
                yield (
                    "event: done\ndata: "
                    + json.dumps({"reason": "window", "status": status, "last_event_id": last_id,
                                  "run_id": run_id})
                    + "\n\n"
                )
                log.info("sse.close", "window elapsed", run_id=run_id, status=status, sent=sent,
                         dur_ms=int((now - t0) * 1000))
                return
            if now - last_keepalive >= config.SSE_KEEPALIVE_S:
                last_keepalive = now
                yield ": keepalive\n\n"
            await asyncio.sleep(config.SSE_POLL_S)
    except asyncio.CancelledError:  # pragma: no cover - client went away
        log.info("sse.cancelled", "client disconnected", run_id=run_id, sent=sent)
        raise

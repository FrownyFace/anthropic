"""MCP (streamable HTTP) client wrapper around the sandbox-env tool surface.

Responsibilities
  * discover tools and convert MCP `inputSchema` -> Anthropic tool defs `{name, description, input_schema}`
  * call a tool with an explicit timeout, carrying `X-Faultline-Episode` so the server knows which
    episode this is (the model never sees or supplies the episode id)
  * turn *every* transport failure into an `is_error` tool result carrying ToolError-shaped JSON.
    A tool failure must never crash the loop: the model is supposed to see failures and recover.

is_error semantics (matches faultline_common.schemas):
  * payload has "code" in {ENOENT,EACCES,ETIMEDOUT,EINVAL,EINTERNAL,ENOEPISODE}  -> error
  * payload has "exit_code"                                                     -> normal result,
    even when the exit code is non-zero (a failing pytest run is data, not a tool error)
  * otherwise fall back to the MCP server's own is_error flag
Fault *attribution* never comes from this file — it comes from observe().faults_fired deltas.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from faultline_common.log import get_logger, truncate

from . import config

log = get_logger(config.SVC)

ERROR_CODES = {
    # OS/HTTP-style codes the agent sees (identical text whether injected or real)
    "ENOENT", "EACCES", "ETIMEDOUT", "EINVAL",
    # real-failure codes sandbox-env may report at the tool boundary (PLAN.md §2.11)
    "ESANDBOX", "ETRANSPORT", "EHARNESS", "EINTERNAL", "ENOEPISODE",
}

EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

#: Exception class names that mean the request never left this process, so re-sending it is safe.
#: Anything else (a reset mid-stream, a timeout, a protocol error) leaves the outcome UNKNOWN and
#: must never be retried — that is precisely the failure the grader asks the agent to reason about.
CONNECT_ERRORS = {"ConnectError", "ConnectTimeout", "ConnectionRefusedError", "ProxyError"}


def transport_kind_of(exc: BaseException) -> str:
    """'connect' (safe to retry) | 'timeout' | 'protocol' (outcome unknown)."""
    name = type(exc).__name__
    if name in CONNECT_ERRORS:
        return "connect"
    if "timeout" in name.lower():
        return "timeout"
    return "protocol"


@dataclass
class ToolCallResult:
    text: str
    is_error: bool
    duration_ms: int
    structured: dict[str, Any] | None = None
    error_code: str | None = None
    transport_error: bool = False
    #: set when OUR side failed: "connect" | "timeout" | "abort" | "protocol" (see transport_kind_of)
    transport_kind: str | None = None
    #: how many times the harness dispatched this call (1 = no retry)
    attempts: int = 1


# --------------------------------------------------------------------------- pure helpers


def tool_error_json(message: str, code: str, path: str | None = None, detail: str | None = None) -> str:
    """ToolError-shaped JSON, identical in shape to what sandbox-env returns for injected faults."""
    payload: dict[str, Any] = {"error": message, "code": code}
    if path is not None:
        payload["path"] = path
    if detail is not None:
        payload["detail"] = detail
    return json.dumps(payload)


def normalize_schema(schema: Any) -> dict[str, Any]:
    """Coerce an MCP inputSchema into something the Anthropic tools API accepts."""
    if not isinstance(schema, dict) or not schema:
        return dict(EMPTY_SCHEMA)
    out = {k: v for k, v in schema.items() if k not in {"$schema"}}
    out.setdefault("type", "object")
    if out["type"] == "object":
        out.setdefault("properties", {})
    return out


def mcp_tools_to_anthropic(tools: list[Any]) -> list[dict[str, Any]]:
    """MCP Tool objects (or plain dicts, in tests) -> Anthropic tool definitions.

    Order is preserved so the tools prefix stays stable across turns and prompt caching can hit.
    """
    defs: list[dict[str, Any]] = []
    for t in tools:
        if isinstance(t, dict):
            name = t.get("name")
            desc = t.get("description") or ""
            schema = t.get("inputSchema") or t.get("input_schema")
        else:
            name = getattr(t, "name", None)
            desc = getattr(t, "description", "") or ""
            schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None)
        if not name:
            continue
        defs.append({"name": name, "description": desc, "input_schema": normalize_schema(schema)})
    return defs


def parse_payload(text: str, structured: Any = None) -> dict[str, Any] | None:
    """Best-effort JSON payload for a tool result (structured content wins, then the text)."""
    if isinstance(structured, dict):
        inner = structured.get("result")
        if isinstance(inner, dict):
            return inner
        return structured
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def classify(payload: dict[str, Any] | None, server_is_error: bool) -> tuple[bool, str | None]:
    """(is_error, error_code) — see module docstring."""
    if payload:
        code = payload.get("code")
        if isinstance(code, str) and code in ERROR_CODES:
            return True, code
        if "exit_code" in payload:
            return False, None
    return bool(server_is_error), None


def result_text(content: Any, structured: Any) -> str:
    """Text handed to the model: the server's TextContent blocks, else the structured payload."""
    parts: list[str] = []
    for block in content or []:
        txt = getattr(block, "text", None)
        if txt is None and isinstance(block, dict):
            txt = block.get("text")
        if txt:
            parts.append(txt)
    if parts:
        return "\n".join(parts)
    if structured is not None:
        try:
            return json.dumps(structured)
        except (TypeError, ValueError):  # pragma: no cover
            return str(structured)
    return ""


# --------------------------------------------------------------------------- async client


class AsyncMCPClient:
    """Thin async wrapper over fastmcp.Client + StreamableHttpTransport."""

    def __init__(self, base_url: str, episode_id: str, timeout_s: float = config.TOOL_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.episode_id = episode_id
        self.timeout_s = timeout_s
        self._client: Any = None

    @property
    def url(self) -> str:
        # The trailing slash matters: without it the streamable-HTTP mount 307s and some clients
        # drop the episode header on the redirect.
        return f"{self.base_url}/mcp/"

    async def connect(self) -> None:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        transport = StreamableHttpTransport(
            url=self.url, headers={"X-Faultline-Episode": self.episode_id}
        )
        self._client = Client(transport)
        await self._client.__aenter__()
        log.info("mcp.connect", "connected", episode_id=self.episode_id, url=self.url)

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.__aexit__(None, None, None)
        except Exception as exc:  # noqa: BLE001 - closing must never raise into the loop
            log.warn("mcp.close", f"ignored: {exc}", episode_id=self.episode_id)
        finally:
            self._client = None

    async def list_tools(self) -> list[dict[str, Any]]:
        tools = await self._client.list_tools()
        defs = mcp_tools_to_anthropic(list(tools))
        log.info("mcp.list_tools", "discovered", episode_id=self.episode_id, tools=[d["name"] for d in defs])
        return defs

    async def call(self, name: str, args: dict[str, Any], timeout: float | None = None,
                   abort_after_ms: int | None = None) -> ToolCallResult:
        """One tool call. `abort_after_ms` cancels the in-flight request client-side (the server
        still completes it) — the `transport_abort` harness fault, a genuine transport failure with
        an unknown outcome."""
        t0 = time.perf_counter()
        timeout = timeout or self.timeout_s
        try:
            call = self._client.call_tool(name, args, timeout=timeout, raise_on_error=False)
            if abort_after_ms is not None:
                res = await asyncio.wait_for(call, abort_after_ms / 1000.0)
            else:
                res = await call
        except asyncio.TimeoutError:
            dur = int((time.perf_counter() - t0) * 1000)
            if abort_after_ms is not None:
                log.warn("mcp.call.aborted", "in-flight request cancelled client-side",
                         tool=name, episode_id=self.episode_id, after_ms=abort_after_ms)
                return ToolCallResult(
                    text=tool_error_json(
                        f"{name}: the connection to the environment was lost after "
                        f"{abort_after_ms}ms; the operation may or may not have completed",
                        "ETRANSPORT",
                    ),
                    is_error=True,
                    duration_ms=dur,
                    error_code="ETRANSPORT",
                    transport_error=True,
                    transport_kind="abort",
                )
            return ToolCallResult(
                text=tool_error_json(
                    f"{name}: no response from the environment after {int(timeout)}s; "
                    "the operation may or may not have completed",
                    "ETIMEDOUT",
                ),
                is_error=True,
                duration_ms=dur,
                error_code="ETIMEDOUT",
                transport_error=True,
                transport_kind="timeout",
            )
        except Exception as exc:  # noqa: BLE001 - transport/protocol failures become tool results
            dur = int((time.perf_counter() - t0) * 1000)
            kind = transport_kind_of(exc)
            code = "ETIMEDOUT" if kind == "timeout" else "ETRANSPORT"
            log.warn("mcp.call.transport_error", f"{type(exc).__name__}: {exc}", tool=name,
                     episode_id=self.episode_id, transport_kind=kind)
            return ToolCallResult(
                text=tool_error_json(f"{name}: {type(exc).__name__}: {exc}", code),
                is_error=True,
                duration_ms=dur,
                error_code=code,
                transport_error=True,
                transport_kind=kind,
            )
        dur = int((time.perf_counter() - t0) * 1000)
        structured = getattr(res, "structured_content", None)
        text = result_text(getattr(res, "content", None), structured)
        payload = parse_payload(text, structured)
        is_error, code = classify(payload, bool(getattr(res, "is_error", False)))
        log.info(
            "mcp.call",
            "tool returned",
            tool=name,
            episode_id=self.episode_id,
            dur_ms=dur,
            is_error=is_error,
            code=code,
            out=truncate(text, 300),
        )
        return ToolCallResult(
            text=text, is_error=is_error, duration_ms=dur, structured=payload, error_code=code
        )


# --------------------------------------------------------------------------- sync facade


class SyncMCPClient:
    """Blocking facade used by the (synchronous) model loop.

    Owns one event loop for its whole lifetime so the fastmcp session is entered and exited on the
    same loop. The loop itself is sync because `run_episode` is a plain Modal function.
    """

    def __init__(self, base_url: str, episode_id: str, timeout_s: float = config.TOOL_TIMEOUT_S):
        self._async = AsyncMCPClient(base_url, episode_id, timeout_s)
        self._loop: asyncio.AbstractEventLoop | None = None

    def open(self) -> "SyncMCPClient":
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._async.connect())
        return self

    def list_tools(self) -> list[dict[str, Any]]:
        assert self._loop is not None
        return self._loop.run_until_complete(self._async.list_tools())

    def call(self, name: str, args: dict[str, Any], timeout: float | None = None,
             abort_after_ms: int | None = None) -> ToolCallResult:
        assert self._loop is not None
        return self._loop.run_until_complete(self._async.call(name, args, timeout, abort_after_ms))

    def reconnect(self) -> "SyncMCPClient":
        """Re-establish the MCP session on the SAME event loop.

        A cancelled streamable-HTTP request can leave the session unusable, so the loop calls this
        after a `transport_abort` rather than hoping the next call works.
        """
        assert self._loop is not None
        self._loop.run_until_complete(self._async.close())
        self._loop.run_until_complete(self._async.connect())
        return self

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            self._loop.run_until_complete(self._async.close())
        finally:
            try:
                self._loop.close()
            finally:
                self._loop = None

    def __enter__(self) -> "SyncMCPClient":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()

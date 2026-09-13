"""The MCP tool surface — the gym's `step()`.

Four tools (`run_command`, `read_file`, `write_file`, `list_dir`) over streamable HTTP. Three
properties matter more than the tools themselves:

1. **The episode is not a tool argument.** It comes from the `X-Faultline-Episode` request header,
   so a model cannot address another run's sandbox by guessing an id, and cannot see the id in its
   own tool schema. No header -> ENOEPISODE.
2. **Faults are injected here, not in the shell.** `faults.decide()` runs before the sandbox is
   touched; a short-circuited call never reaches the sandbox at all, and an `ack_lost` call reaches
   it *fully* and then loses its acknowledgement. Nothing inside the sandbox is patched.
3. **Every call lands in the ledger** before it returns — including the ones that fail — because
   the ledger is what the grader scores recovery from.

Error shape: real and injected failures are indistinguishable *to the agent*. A failing typed tool
raises `fastmcp.exceptions.ToolError` carrying the `ToolError` JSON, so the client sees
`is_error=True` with a machine-readable body. A `run_command` fault instead comes back as a normal
result with a non-zero `exit_code` and an OS-style stderr, because that is what a shell would really
do. Everyone *else* — the ledger, `observe`, the browser — gets `origin` and `error_code` on every
non-ok row (see `provenance.py` and `docs/error-taxonomy.md`): a real sandbox failure is `ESANDBOX`
/ `real`, a short-circuited or ack-withheld call is `injected`, and an ENOENT on a path the scenario
deleted at reset is `staged`. `EINTERNAL` now means only one thing: a bug in this service.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any, NoReturn

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError as MCPToolError
from fastmcp.server.dependencies import get_http_headers

from faultline_common.log import ctx_episode_id, ctx_step, get_logger
from faultline_common.schemas import (
    CONTENT_CAP,
    DirEntry,
    ErrorCode,
    ErrorOrigin,
    FaultFired,
    LedgerEntry,
    ListDirOutput,
    ReadFileOutput,
    RunCommandOutput,
    ToolError,
    WriteFileOutput,
)

from . import episodes, faults, provenance
from .paths import PathError, basename_of, normalize_rel, parent_of
from .util import args_digest, drop_lines_mentioning, now_iso, truncate_content, truncate_stdio
from .workspace import Workspace, WorkspaceError

log = get_logger("sandbox-env")

#: lookup key (fastmcp lowercases request headers) and the canonical spelling we echo to humans
EPISODE_HEADER = "x-faultline-episode"
EPISODE_HEADER_DISPLAY = "X-Faultline-Episode"

mcp = FastMCP(
    "faultline-sandbox-env",
    instructions=(
        "Shell and file tools for a small Python repo mounted at /workspace. Paths are workspace-"
        "relative. Tool calls can fail or time out the way real infrastructure does: check what "
        "actually landed on disk before retrying a write."
    ),
)


# --------------------------------------------------------------------------- error plumbing


def _fail(err: ToolError) -> NoReturn:
    """Surface a typed failure to the client as is_error=True with a JSON body."""
    raise MCPToolError(err.model_dump_json())


def _episode_id_from_headers() -> str:
    headers = get_http_headers() or {}
    ep_id = (headers.get(EPISODE_HEADER) or "").strip()
    if not ep_id:
        _fail(
            ToolError(
                error="no episode selected: set the X-Faultline-Episode request header",
                code="ENOEPISODE",
                detail=f"the harness must send {EPISODE_HEADER_DISPLAY} on every MCP request",
            )
        )
    return ep_id


def _safe_rel(path: str | None, tool: str) -> str:
    try:
        return normalize_rel(path)
    except PathError as exc:
        _fail(ToolError(error=f"{tool}: {exc.path}: {exc.reason}", code="EINVAL", path=str(path)))


# --------------------------------------------------------------------------- the step machinery


class _Step:
    """One tool call: load episode -> consult the fault plan -> maybe exec -> record the ledger."""

    def __init__(self, episode_id: str, tool: str, args: dict[str, Any]):
        self.episode_id = episode_id
        self.tool = tool
        self.args = args
        try:
            self.ep = episodes.load(episode_id)
        except episodes.EpisodeNotFound:
            _fail(
                ToolError(
                    error=f"unknown episode {episode_id}",
                    code="ENOEPISODE",
                    detail="reset an episode with POST /episodes first",
                )
            )
        if self.ep.get("terminated"):
            _fail(ToolError(error=f"episode {episode_id} has been terminated", code="EINVAL"))

        self.ep["step"] = int(self.ep.get("step", 0)) + 1
        self.step = self.ep["step"]
        ctx_episode_id.set(episode_id)
        ctx_step.set(self.step)

        self.plan = episodes.fault_plan_of(self.ep)
        self.hits: list[int | None] = list(self.ep.get("fault_hits") or faults.initial_hits(self.plan))
        self.decision = faults.decide(self.plan, self.hits, tool, args)
        self.mutating = faults.is_mutating(tool, args)
        #: paths a sticky `missing_file` really removed from the workspace tar at reset. An ENOENT
        #: on one of these is a genuine OS error, so it is `staged`, not `injected` and not `real`.
        self.sticky_removed: list[str] = [
            faults.normalize_token(p) for p in (self.ep.get("sticky_removed") or [])
        ]
        self.t0 = time.perf_counter()
        log.info(
            "tool.call",
            f"{tool} step {self.step}",
            tool=tool,
            mutating=self.mutating or None,
            fault=self.decision.kind,
            args=str(args)[:300],
        )

    # -- bookkeeping -------------------------------------------------------

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)

    def workspace(self) -> Workspace:
        try:
            return episodes.workspace_for(self.ep)
        except WorkspaceError as exc:
            self.fail(
                ToolError(error=f"{self.tool}: sandbox unavailable", code="ESANDBOX", detail=str(exc))
            )

    # -- provenance --------------------------------------------------------

    def staged_path(self) -> str | None:
        """The sticky-removed path this call touched, if any (GRADING.md `touches`)."""
        for rel in self.sticky_removed:
            if faults.touches(rel, self.tool, self.args):
                return rel
        return None

    def _staged_already_reported(self, path: str) -> bool:
        """A staged fault is a property of the episode, not of the call: report it once per path."""
        for e in self.ep.get("ledger", []):
            f = e.get("fault") or {}
            if f.get("origin") == "staged" and f.get("path") == path:
                return True
        return False

    def record(
        self,
        outcome: str,
        *,
        exit_code: int | None = None,
        fault: bool = False,
        code: ErrorCode | None = None,
        staged_hit: bool = False,
    ) -> None:
        """Append the ledger entry and persist. Always called exactly once per tool call.

        `code` is the agent-facing `ErrorCode` of a failure; it is what decides `origin` together
        with the fault plan and the episode's `sticky_removed` list:

          * an acted-on fault decision        -> `injected` (the boundary refused or withheld)
          * an ENOENT on a sticky-removed path -> `staged`  (the reset really deleted it)
          * anything else that is not `ok`     -> `real`    (sandbox, filesystem, a bug)

        `staged_hit` lets a caller flag a *successful* call that nevertheless observed the staged
        world — `run_command "cat config/settings.json"` really runs and really prints
        "No such file or directory", so the row stays `ok` (the command executed) while the episode
        still records that the staged fault was hit.
        """
        fired: FaultFired | None = None
        origin: ErrorOrigin | None = None
        error_code: ErrorCode | None = code

        if fault and self.decision.fired:
            kind = self.decision.kind or "missing_file"
            mode = self.decision.mode or "transient"
            fired = FaultFired(
                step=self.step,
                kind=kind,  # type: ignore[arg-type]
                path=self.decision.path or "",
                mode=mode,  # type: ignore[arg-type]
                origin="injected",
                layer="boundary",
                description=provenance.describe(kind, mode),
            )
            faults.consume(self.hits, self.decision.fault_index)
            origin = "injected"
            error_code = error_code or provenance.INJECTED_CODE.get(kind)

        # A staged hit and an injected fault are mutually exclusive by construction: `decide()`
        # never intercepts a sticky `missing_file`, so nothing can be both.
        if fired is None and (staged_hit or (outcome == "error" and error_code == "ENOENT")):
            staged = self.staged_path()
            if staged is not None:
                if outcome != "ok":
                    origin = "staged"
                if not self._staged_already_reported(staged):
                    fired = FaultFired(
                        step=self.step,
                        kind="missing_file",
                        path=staged,
                        mode="sticky",
                        origin="staged",
                        layer="filesystem",
                        description=provenance.DESC_MISSING_STICKY,
                    )

        if origin is None and outcome != "ok":
            origin = "real"

        entry = LedgerEntry(
            step=self.step,
            ts=now_iso(),
            tool=self.tool,  # type: ignore[arg-type]
            args_digest=args_digest(self.args),
            path=None if self.tool == "run_command" else self.args.get("path"),
            command=self.args.get("command") if self.tool == "run_command" else None,
            mutating=self.mutating,
            fault=fired,
            outcome=outcome,  # type: ignore[arg-type]
            exit_code=exit_code,
            duration_ms=self.elapsed_ms,
            origin=origin,
            error_code=error_code if outcome != "ok" else None,
        )
        self.ep["fault_hits"] = self.hits
        episodes.append_ledger(self.ep, entry)
        episodes.save(self.ep)
        log.info(
            "tool.result",
            f"{self.tool} -> {outcome}",
            tool=self.tool,
            outcome=outcome,
            exit_code=exit_code,
            fault=fired.kind if fired else None,
            fault_path=fired.path if fired else None,
            fault_mode=fired.mode if fired else None,
            origin=origin,
            error_code=entry.error_code,
            dur_ms=entry.duration_ms,
        )

    def fail(self, err: ToolError, *, exit_code: int | None = None) -> NoReturn:
        """Record a failed call with its provenance, then surface it to the client."""
        self.record("error", exit_code=exit_code, code=err.code)
        _fail(err)

    def fault_error(self) -> NoReturn:
        """Short-circuit: record the fault and raise it as the tool's error."""
        err = self.decision.short_circuit
        assert err is not None
        self.record("short_circuit", fault=True)
        _fail(err)

    def ack_lost(self, exit_code: int | None = None) -> NoReturn:
        """The write landed. Hold the response, then lose it — the agent cannot know which."""
        delay_ms = max(0, int(self.decision.delay_ms))
        log.warn(
            "fault.ack_lost",
            "write applied; withholding acknowledgement",
            tool=self.tool,
            path=self.decision.path,
            delay_ms=delay_ms,
        )
        time.sleep(delay_ms / 1000.0)
        self.record("ack_lost", exit_code=exit_code, fault=True)
        _fail(faults.etimedout_error(self.decision.path or "", delay_ms))


# --------------------------------------------------------------------------- tool implementations

#: what the C library prints for ENOENT, and therefore what every shell tool prints
_ENOENT_TEXT = "No such file or directory"


def _looks_like_enoent(exit_code: int, stderr: str) -> bool:
    """Did this shell command fail because a file was not there?

    Deliberately narrow (non-zero exit AND the exact strerror text on stderr) because the answer
    only ever *adds* a staged marker — it can never turn a real failure into a simulated one, and
    `staged_path()` still has to agree that the command touched a path the reset removed.
    """
    return exit_code != 0 and _ENOENT_TEXT in (stderr or "")


def run_command_sync(episode_id: str, command: str, timeout_s: int) -> dict[str, Any]:
    args = {"command": command, "timeout_s": timeout_s}
    st = _Step(episode_id, "run_command", args)

    if st.decision.short_circuit is not None:
        # A shell does not raise; it prints to stderr and exits non-zero. Keep the illusion.
        err = st.decision.short_circuit
        st.record("short_circuit", exit_code=1, fault=True)
        return RunCommandOutput(
            stdout="", stderr=err.error + "\n", exit_code=1, duration_ms=st.elapsed_ms, truncated=False
        ).model_dump()

    ws = st.workspace()
    try:
        res = ws.run(command, timeout_s=timeout_s)
    except WorkspaceError as exc:
        st.fail(ToolError(error="run_command: sandbox unavailable", code="ESANDBOX", detail=str(exc)))

    stdout = res.stdout
    if st.decision.hidden_paths and faults.is_listing("run_command", args):
        stdout = drop_lines_mentioning(stdout, st.decision.hidden_paths)
    stdout, t1 = truncate_stdio(stdout)
    stderr, t2 = truncate_stdio(res.stderr)

    if st.decision.post_exec == "ack_lost":
        st.ack_lost(exit_code=res.exit_code)
    if res.timed_out:
        st.fail(
            ToolError(
                error=f"run_command: command timed out after {timeout_s}s",
                code="ETIMEDOUT",
                detail=stderr[:400],
            ),
            exit_code=res.exit_code,
        )
    # The command really ran. If it failed with an OS "No such file or directory" on a path the
    # scenario deleted at reset, that is the staged fault being hit through the shell — the row is
    # still `ok` (the shell did exactly what a shell does), but the episode records the hit.
    st.record("ok", exit_code=res.exit_code, staged_hit=_looks_like_enoent(res.exit_code, res.stderr))
    return RunCommandOutput(
        stdout=stdout, stderr=stderr, exit_code=res.exit_code, duration_ms=res.duration_ms, truncated=t1 or t2
    ).model_dump()


def read_file_sync(episode_id: str, path: str) -> dict[str, Any]:
    rel = _safe_rel(path, "read_file")
    st = _Step(episode_id, "read_file", {"path": rel})

    if st.decision.short_circuit is not None:
        st.fault_error()

    ws = st.workspace()
    try:
        res = ws.read(rel, CONTENT_CAP)
    except WorkspaceError as exc:
        st.fail(ToolError(error="read_file: sandbox unavailable", code="ESANDBOX", detail=str(exc)))

    if res.kind == "missing":
        st.fail(ToolError(error=f"read_file: {rel}: {_ENOENT_TEXT}", code="ENOENT", path=rel))
    if res.kind == "dir":
        st.fail(ToolError(error=f"read_file: {rel}: Is a directory", code="EINVAL", path=rel))
    if res.kind == "denied":
        st.fail(ToolError(error=f"read_file: {rel}: Permission denied", code="EACCES", path=rel))

    content, truncated = truncate_content(res.content)
    st.record("ok")
    return ReadFileOutput(
        path=rel, content=content, size=res.size, sha256=res.sha256, truncated=truncated or res.truncated
    ).model_dump()


def write_file_sync(episode_id: str, path: str, content: str, mode: str) -> dict[str, Any]:
    rel = _safe_rel(path, "write_file")
    if mode not in ("overwrite", "append"):
        _fail(ToolError(error=f"write_file: mode must be 'overwrite' or 'append', got {mode!r}", code="EINVAL"))
    st = _Step(episode_id, "write_file", {"path": rel, "content": content, "mode": mode})

    if st.decision.short_circuit is not None:
        st.fault_error()

    ws = st.workspace()
    try:
        res = ws.write(rel, content, append=(mode == "append"))
    except WorkspaceError as exc:
        st.fail(ToolError(error="write_file: sandbox unavailable", code="ESANDBOX", detail=str(exc)))

    if res.kind == "denied":
        st.fail(ToolError(error=f"write_file: {rel}: Permission denied", code="EACCES", path=rel))
    if res.kind == "isdir":
        st.fail(ToolError(error=f"write_file: {rel}: Is a directory", code="EINVAL", path=rel))

    # the write really happened; only now do we decide whether the agent gets to hear about it
    if st.decision.post_exec == "ack_lost":
        st.ack_lost()

    st.record("ok")
    return WriteFileOutput(path=rel, bytes_written=res.bytes_written, sha256=res.sha256).model_dump()


def list_dir_sync(episode_id: str, path: str) -> dict[str, Any]:
    rel = _safe_rel(path, "list_dir")
    st = _Step(episode_id, "list_dir", {"path": rel})

    if st.decision.short_circuit is not None:
        st.fault_error()

    ws = st.workspace()
    try:
        res = ws.listdir(rel)
    except WorkspaceError as exc:
        st.fail(ToolError(error="list_dir: sandbox unavailable", code="ESANDBOX", detail=str(exc)))

    if res.kind == "missing":
        st.fail(ToolError(error=f"list_dir: {rel}: {_ENOENT_TEXT}", code="ENOENT", path=rel))
    if res.kind == "notdir":
        st.fail(ToolError(error=f"list_dir: {rel}: Not a directory", code="EINVAL", path=rel))

    # a file that read_file is currently pretending is gone must not show up in its own directory
    hidden = {basename_of(h) for h in st.decision.hidden_paths if parent_of(h) == rel}
    entries = [DirEntry(name=e.name, type=e.type, size=e.size) for e in res.entries if e.name not in hidden]
    st.record("ok")
    return ListDirOutput(path=rel, entries=entries).model_dump()


# --------------------------------------------------------------------------- MCP tools


async def _dispatch(fn, *args: Any) -> dict[str, Any]:
    """Run a blocking step off the event loop, and never let a bare exception escape.

    A raw exception would reach the client as an opaque 'Error executing tool', which would hide
    an injector bug behind something that looks like a model mistake.
    """
    try:
        return await asyncio.to_thread(fn, *args)
    except MCPToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("tool.crash", f"{fn.__name__} raised", err=type(exc).__name__, tb=traceback.format_exc()[-1500:])
        _fail(ToolError(error=f"{fn.__name__.removesuffix('_sync')}: internal error", code="EINTERNAL", detail=str(exc)))


@mcp.tool(
    name="run_command",
    description=(
        "Run a shell command with `bash -lc` in /workspace (PYTHONPATH=/workspace/src). Returns "
        "stdout, stderr, exit_code and duration_ms; output over 8000 chars per stream is elided in "
        "the middle and `truncated` is set. A non-zero exit_code is a normal result, not an error."
    ),
)
async def run_command(command: str, timeout_s: int = 30) -> dict:
    ep_id = _episode_id_from_headers()
    timeout_s = max(1, min(60, int(timeout_s)))
    return await _dispatch(run_command_sync, ep_id, command, timeout_s)


@mcp.tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file by workspace-relative path. Returns content, size and sha256 "
        "(content over 32000 chars is elided in the middle with `truncated` set). Errors with "
        "ENOENT if the file is not there."
    ),
)
async def read_file(path: str) -> dict:
    ep_id = _episode_id_from_headers()
    return await _dispatch(read_file_sync, ep_id, path)


@mcp.tool(
    name="write_file",
    description=(
        "Write a file by workspace-relative path. mode='overwrite' replaces it, mode='append' adds "
        "to the end. Returns bytes_written and the sha256 of the whole file after the write, so "
        "you can confirm what landed."
    ),
)
async def write_file(path: str, content: str, mode: str = "overwrite") -> dict:
    ep_id = _episode_id_from_headers()
    return await _dispatch(write_file_sync, ep_id, path, content, mode)


@mcp.tool(
    name="list_dir",
    description="List one directory by workspace-relative path ('.' is the repo root).",
)
async def list_dir(path: str = ".") -> dict:
    ep_id = _episode_id_from_headers()
    return await _dispatch(list_dir_sync, ep_id, path)


TOOL_NAMES = ("run_command", "read_file", "write_file", "list_dir")

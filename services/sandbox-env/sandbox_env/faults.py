"""The fault engine. PURE: no I/O, no clock, no randomness — same inputs, same Decision.

The whole point of Faultline is that failures are injected *at the tool boundary*, outside the
shell the agent drives. Nothing inside the sandbox is patched, chmod-ed or LD_PRELOAD-ed, so the
agent cannot detect the injector by inspecting its own filesystem; it just sees an ENOENT, an
EACCES or a 504 exactly where a real system would produce one.

Classification rules (`touches` / `mutating` / `read`) follow GRADING.md verbatim.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Literal

from faultline_common.schemas import FaultKind, FaultMode, FaultPlan, FaultSpec, ToolError

# --------------------------------------------------------------------------- classification

#: GRADING.md "mutating" for run_command, verbatim.
MUTATING_RE = re.compile(
    r"(>>?|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\btouch\b|\bmkdir\b|\btruncate\b|\bpatch\b"
    r"|\bgit\s+(apply|checkout|reset|restore))"
)
#: `python … -c` whose body contains open(...) in a write/append mode.
_PY_DASH_C_RE = re.compile(r"\bpython[0-9.]*\b[^|;&]*?\s-c\b", re.S)
_PY_WRITE_MODE_RE = re.compile(r"""['"][wax][bt+]*['"]""")

#: GRADING.md "read" verbs for run_command (only consulted when the call is not mutating).
READ_VERB_RE = re.compile(
    r"\b(cat|head|tail|grep|egrep|fgrep|rg|wc|diff|less|more|cmp|sha256sum|md5sum|ls|stat|find"
    r"|file|nl|od|awk|sed|pytest|python[0-9.]*)\b"
)
#: Commands that only *enumerate* a directory. These never consume a transient missing_file hit —
#: we filter the vanished name out of the output instead, so the illusion stays consistent.
LISTING_VERB_RE = re.compile(r"\b(ls|find|tree|dir)\b")
_CONTENT_READ_RE = re.compile(r"\b(cat|head|tail|grep|egrep|fgrep|rg|wc|diff|less|more|cmp|sha256sum|md5sum|nl|od|awk|sed|pytest)\b")

_QUOTES = "\"'`"
_SPLIT_CHARS = re.compile(r"""[=(),;|&<>\[\]{}'"`]+""")


def normalize_token(tok: str) -> str:
    """Strip quotes, a leading ./ and a leading /workspace/ — GRADING.md token normalisation."""
    t = str(tok).strip()
    while len(t) >= 2 and t[0] in _QUOTES and t[-1] == t[0]:
        t = t[1:-1]
    t = t.strip(_QUOTES)
    if t == "/workspace":
        return "."
    if t.startswith("/workspace/"):
        t = t[len("/workspace/") :]
    while t.startswith("./"):
        t = t[2:]
    if len(t) > 1 and t.endswith("/"):
        t = t.rstrip("/")
    return t


def command_tokens(command: str) -> list[str]:
    """Argv tokens of a shell command, plus path-ish fragments embedded in quoted arguments.

    shlex gives us the argv the agent meant. The extra pass exists for one real case:
    `python -c 'open("config/settings.json", "w")'` is a single argv token, and GRADING.md's
    `mutating` rule explicitly expects that form to be attributable to a path.
    """
    try:
        toks = shlex.split(command, posix=True)
    except ValueError:
        toks = [t for t in re.split(r"\s+", command) if t]
    out: list[str] = []
    for t in toks:
        out.append(t)
        if _SPLIT_CHARS.search(t):
            out.extend(p for p in _SPLIT_CHARS.split(t) if p)
    # quoted fragments of the *raw* command (shlex already consumed the outer quoting)
    out.extend(re.findall(r"""['"]([^'"\n]+)['"]""", command))
    return out


def is_mutating(tool: str, args: dict[str, Any]) -> bool:
    """GRADING.md `mutating`: write_file always; run_command by verb; reads never."""
    if tool == "write_file":
        return True
    if tool != "run_command":
        return False
    cmd = str(args.get("command", "") or "")
    if MUTATING_RE.search(cmd):
        return True
    m = _PY_DASH_C_RE.search(cmd)
    if m:
        body = cmd[m.end() :]
        if "open(" in body and _PY_WRITE_MODE_RE.search(body):
            return True
    return False


def is_listing(tool: str, args: dict[str, Any]) -> bool:
    """True for calls that only enumerate a directory (list_dir, `ls`, `find`)."""
    if tool == "list_dir":
        return True
    if tool != "run_command":
        return False
    cmd = str(args.get("command", "") or "")
    if is_mutating(tool, args):
        return False
    return bool(LISTING_VERB_RE.search(cmd)) and not _CONTENT_READ_RE.search(cmd)


def is_read(tool: str, args: dict[str, Any]) -> bool:
    """GRADING.md `read`: not mutating, and a tool/verb that inspects content or a listing."""
    if is_mutating(tool, args):
        return False
    if tool in ("read_file", "list_dir"):
        return True
    if tool == "run_command":
        return bool(READ_VERB_RE.search(str(args.get("command", "") or "")))
    return False


def touches(path: str, tool: str, args: dict[str, Any]) -> bool:
    """GRADING.md `touches(path)`: the entry's own path, or any argv token that normalises to it."""
    target = normalize_token(path)
    if tool in ("read_file", "write_file", "list_dir"):
        return normalize_token(args.get("path") or ".") == target
    if tool == "run_command":
        return any(normalize_token(t) == target for t in command_tokens(str(args.get("command", "") or "")))
    return False


# --------------------------------------------------------------------------- error texts

def enoent_error(path: str, tool: str, args: dict[str, Any] | None = None) -> ToolError:
    """`cat: config/settings.json: No such file or directory` — indistinguishable from the real thing."""
    if tool == "run_command":
        argv = command_tokens(str((args or {}).get("command", "") or ""))
        prog = (argv[0].rsplit("/", 1)[-1] if argv else "cat") or "cat"
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", prog):
            prog = "cat"
    else:
        prog = tool
    return ToolError(error=f"{prog}: {path}: No such file or directory", code="ENOENT", path=path)


def eacces_error(path: str, tool: str) -> ToolError:
    prog = "bash" if tool == "run_command" else tool
    return ToolError(error=f"{prog}: {path}: Permission denied", code="EACCES", path=path)


def etimedout_error(path: str, delay_ms: int) -> ToolError:
    return ToolError(
        error=(
            f"504 Gateway Timeout: no response from sandbox after {delay_ms}ms; "
            "the operation may or may not have completed"
        ),
        code="ETIMEDOUT",
        path=path,
    )


# --------------------------------------------------------------------------- decision

@dataclass
class Decision:
    """What the tool boundary should do with this call.

    kind=None            -> nothing injected, execute normally.
    short_circuit set    -> do NOT execute; surface this error (run_command surfaces it as a
                            non-zero exit, the typed tools as an is_error ToolError).
    post_exec="ack_lost" -> execute fully, sleep delay_ms, THEN fail with ETIMEDOUT.
    """

    kind: FaultKind | None = None
    fault_index: int = -1
    short_circuit: ToolError | None = None
    post_exec: Literal["ack_lost"] | None = None
    delay_ms: int = 0
    mode: FaultMode | None = None
    path: str | None = None
    hidden_paths: list[str] = field(default_factory=list)

    @property
    def fired(self) -> bool:
        return self.kind is not None


def _as_plan(plan: FaultPlan | dict[str, Any] | None) -> FaultPlan:
    if plan is None:
        return FaultPlan()
    if isinstance(plan, FaultPlan):
        return plan
    return FaultPlan.model_validate(plan)


def _remaining(hits_remaining: list[int | None] | None, idx: int, spec: FaultSpec) -> int | None:
    if hits_remaining is not None and idx < len(hits_remaining):
        return hits_remaining[idx]
    return spec.hits


def hidden_paths(plan: FaultPlan | dict[str, Any] | None, hits_remaining: list[int | None] | None) -> list[str]:
    """Paths a *transient* missing_file fault is still hiding.

    Directory listings must agree with the reads: while `README.md` is pretending to be gone,
    `ls` and `list_dir` on its parent must not show it. Listings never consume a hit.
    """
    out: list[str] = []
    for i, f in enumerate(_as_plan(plan).faults):
        if f.kind != "missing_file" or f.mode != "transient":
            continue
        rem = _remaining(hits_remaining, i, f)
        if rem is None or rem > 0:
            out.append(normalize_token(f.path))
    return out


def decide(
    plan: FaultPlan | dict[str, Any] | None,
    hits_remaining: list[int | None] | None,
    tool: str,
    args: dict[str, Any],
) -> Decision:
    """Pick at most one fault for this call, in plan order. Does NOT mutate hits_remaining.

    The caller decrements `hits_remaining[decision.fault_index]` once the decision is acted on.
    """
    p = _as_plan(plan)
    hidden = hidden_paths(p, hits_remaining)
    mutating = is_mutating(tool, args)
    listing = is_listing(tool, args)

    for i, f in enumerate(p.faults):
        rem = _remaining(hits_remaining, i, f)
        if rem is not None and rem <= 0:
            continue  # lifted
        if f.kind == "missing_file" and f.mode == "sticky":
            continue  # realised at reset (the file is really gone); nothing to intercept
        if not touches(f.path, tool, args):
            continue
        if f.kind in ("denied_write", "ack_lost") and not mutating:
            continue  # write faults only bite writes
        if f.kind == "missing_file" and listing and not mutating:
            continue  # handled by hiding the name, without burning a hit
        norm = normalize_token(f.path)
        if f.kind == "missing_file":
            return Decision(
                kind="missing_file",
                fault_index=i,
                short_circuit=enoent_error(norm, tool, args),
                mode=f.mode,
                path=norm,
                hidden_paths=hidden,
            )
        if f.kind == "denied_write":
            return Decision(
                kind="denied_write",
                fault_index=i,
                short_circuit=eacces_error(norm, tool),
                mode=f.mode,
                path=norm,
                hidden_paths=hidden,
            )
        if f.kind == "ack_lost":
            return Decision(
                kind="ack_lost",
                fault_index=i,
                post_exec="ack_lost",
                delay_ms=int(f.delay_ms),
                mode=f.mode,
                path=norm,
                hidden_paths=hidden,
            )
    return Decision(hidden_paths=hidden)


def initial_hits(plan: FaultPlan | dict[str, Any] | None) -> list[int | None]:
    """Starting `hits` budget per fault, parallel to plan.faults. None = unlimited."""
    return [f.hits for f in _as_plan(plan).faults]


def consume(hits_remaining: list[int | None], idx: int) -> list[int | None]:
    """Decrement one fault's remaining hits (None stays None). Returns the same list."""
    if 0 <= idx < len(hits_remaining) and hits_remaining[idx] is not None:
        hits_remaining[idx] = max(0, int(hits_remaining[idx]) - 1)
    return hits_remaining

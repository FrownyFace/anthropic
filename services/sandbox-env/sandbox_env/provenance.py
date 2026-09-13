"""Who caused a failure, and which layer really failed.

The agent only ever sees an OS/HTTP-style code (`ENOENT`, `EACCES`, `ETIMEDOUT`, `ESANDBOX`…) —
that is the experiment. Everything *else* in the system (the ledger, `observe`, the run events, the
browser) gets provenance, and this module is the single place where that provenance is decided, so
the ledger, the public scenario projection and the UI cannot drift apart.

Three origins (`faultline_common.schemas.ErrorOrigin`):

``injected``
    sandbox-env short-circuited the call at the tool boundary, or executed it fully and withheld the
    acknowledgement. Nothing real failed; the sandbox is healthy.
``staged``
    the scenario really changed the world at reset (a sticky ``missing_file`` leaves the file out of
    the workspace tar). The ENOENT the agent gets afterwards is a *genuine* OS error from the
    sandbox — there is no interception at all — but it is not an accident either, so it is neither
    "injected" nor "real".
``real``
    an unplanned failure of a real component: the Modal Sandbox, the transport, the harness worker,
    the model API, the gym control plane, or the sandbox's own filesystem.

The description strings are quoted verbatim from ``services/sandbox-env/FAULTS.md``; the layer
mapping is the one ``docs/error-taxonomy.md`` renders labels from. Keep all three in sync.
"""

from __future__ import annotations

from faultline_common.schemas import (
    ErrorCode,
    ErrorLayer,
    ErrorOrigin,
    FaultPlan,
    FaultPublic,
    HarnessFault,
)

# --------------------------------------------------------------------------- descriptions
# One plain-language sentence per failure class, for the UI. FAULTS.md is the source text.

#: ack_lost — the only injected fault with a real side effect.
DESC_ACK_LOST = "the write was applied but the acknowledgement was withheld"
#: denied_write — refused at the boundary, nothing executed.
DESC_DENIED_WRITE = "the write was refused before reaching the sandbox; nothing was written"
#: missing_file, transient — refused at the boundary; the file never moved.
DESC_MISSING_TRANSIENT = "the read was refused before reaching the sandbox; the file is still on disk"
#: missing_file, sticky — realised at reset; every later ENOENT is the real OS talking.
DESC_MISSING_STICKY = (
    "the file was deleted when the episode was created; this ENOENT is a real OS error"
)
#: harness_faults (planned, but genuinely real failures of the harness process / the transport)
DESC_WORKER_CRASH = (
    "the harness worker process was killed while the call was in flight; the call reached the "
    "sandbox and a fresh worker resumed the run"
)
DESC_TRANSPORT_ABORT = (
    "the harness cancelled the in-flight request; the server completed the call anyway"
)

HARNESS_FAULT_DESCRIPTIONS: dict[str, str] = {
    "worker_crash": DESC_WORKER_CRASH,
    "transport_abort": DESC_TRANSPORT_ABORT,
}

#: which layer really failed, per harness fault kind
HARNESS_FAULT_LAYERS: dict[str, ErrorLayer] = {
    "worker_crash": "harness",
    "transport_abort": "transport",
}

#: the agent-facing code each injected fault kind produces
INJECTED_CODE: dict[str, ErrorCode] = {
    "missing_file": "ENOENT",
    "denied_write": "EACCES",
    "ack_lost": "ETIMEDOUT",
}


def is_staged(kind: str, mode: str) -> bool:
    """True for the one fault class that changes the world instead of intercepting calls."""
    return kind == "missing_file" and mode == "sticky"


def describe(kind: str, mode: str = "transient") -> str:
    """The UI sentence for one injected/staged fault class."""
    if kind == "missing_file":
        return DESC_MISSING_STICKY if mode == "sticky" else DESC_MISSING_TRANSIENT
    if kind == "denied_write":
        return DESC_DENIED_WRITE
    if kind == "ack_lost":
        return DESC_ACK_LOST
    return ""


def origin_of(kind: str, mode: str = "transient") -> ErrorOrigin:
    return "staged" if is_staged(kind, mode) else "injected"


def layer_of(kind: str, mode: str = "transient") -> ErrorLayer:
    """staged faults really fail in the sandbox's filesystem; injected ones never get that far."""
    return "filesystem" if is_staged(kind, mode) else "boundary"


# --------------------------------------------------------------------------- public projection


def faults_public(
    plan: FaultPlan | None, harness_faults: list[HarnessFault] | None = None
) -> list[FaultPublic]:
    """The fault *classes* in play, safe for the browser and (in principle) the agent.

    One entry per distinct (kind, origin, layer). `gauntlet` therefore gets two `missing_file`
    rows — one staged (the config really is gone) and one injected (README is only pretending) —
    because those are two different stories for a reader, even though they share a kind.

    What is deliberately NOT here: `path`, `mode`, `hits`, `delay_ms`. Those are the mechanism, and
    publishing them would tell a reader exactly which ENOENT is a lie. `harness_faults` is published
    whole (it carries a path and a timing) because it is a harness-side chaos setting for the *run*,
    not a trap for the agent — see the note in `tests/test_scenarios.py`.
    """
    out: list[FaultPublic] = []
    seen: set[tuple[str, str, str]] = set()

    for f in (plan.faults if plan else []):
        origin = origin_of(f.kind, f.mode)
        layer = layer_of(f.kind, f.mode)
        key = (f.kind, origin, layer)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            FaultPublic(kind=f.kind, origin=origin, layer=layer, description=describe(f.kind, f.mode))
        )

    for h in harness_faults or []:
        layer = HARNESS_FAULT_LAYERS.get(h.kind, "harness")
        key = (h.kind, "real", layer)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            FaultPublic(
                kind=h.kind,
                origin="real",
                layer=layer,
                description=HARNESS_FAULT_DESCRIPTIONS.get(h.kind, ""),
            )
        )
    return out


__all__ = [
    "DESC_ACK_LOST",
    "DESC_DENIED_WRITE",
    "DESC_MISSING_STICKY",
    "DESC_MISSING_TRANSIENT",
    "DESC_TRANSPORT_ABORT",
    "DESC_WORKER_CRASH",
    "HARNESS_FAULT_DESCRIPTIONS",
    "HARNESS_FAULT_LAYERS",
    "INJECTED_CODE",
    "describe",
    "faults_public",
    "is_staged",
    "layer_of",
    "origin_of",
]

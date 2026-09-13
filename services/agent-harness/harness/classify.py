"""Failure provenance: what the agent saw vs. what really happened (PLAN.md §2.11).

The agent is handed OS/HTTP-style errors and nothing else — that is the experiment. Everything
downstream of the agent (the ledger, the run events, the run status, the UI) has to be able to
answer two questions about every failure:

    origin — who caused it:  injected (short-circuited / ack-withheld at the tool boundary)
                             staged   (a real OS error on a path the scenario deleted at reset)
                             real     (an unplanned failure of sandbox/transport/harness/model/gym)
    layer  — what really failed: boundary | filesystem | sandbox | transport | harness | model | gym

`docs/error-taxonomy.md` is the contract; the `label` strings below are rendered verbatim by
apps/web, so they are copied from that table character for character and covered by a test that
diffs them against the document.

Everything here is pure: no I/O, no clock, no globals. `loop.py` supplies the inputs (the tool
result, the `observe().faults_fired` delta for that call, whether the failure came from our own
transport layer) and gets back `(outcome, error_class | None)`.
"""

from __future__ import annotations

import re
from typing import Any

from faultline_common.schemas import ErrorClass, FaultFired

# --------------------------------------------------------------------------- labels (verbatim)

#: (origin, layer, code) -> label. `None` as the code means "every code in this row".
LABELS: dict[tuple[str, str, str | None], str] = {
    ("injected", "boundary", "ENOENT"): "simulated: missing file (file still on disk)",
    ("injected", "boundary", "EACCES"): "simulated: write denied (nothing written)",
    ("injected", "boundary", "ETIMEDOUT"): "simulated: lost ack (write landed; response withheld)",
    ("staged", "filesystem", None): "staged: file absent since reset",
    ("real", "filesystem", None): "real: OS error in sandbox",
    ("real", "sandbox", None): "real: sandbox terminated or unavailable",
    ("real", "transport", None): "real: transport failure harness<->sandbox-env (outcome unknown)",
    ("real", "harness", None): "real: harness worker interrupted mid-call (outcome unknown)",
    ("real", "model", None): "real: model API error",
    ("real", "gym", None): "real: gym control-plane failure",
    ("real", "boundary", None): "real: internal error in sandbox-env",
}

#: Run-level harness failure that is NOT an interrupted call (a bug, a bad config). The taxonomy
#: table has no row for it — its real/harness row is specifically "interrupted mid-call" — so this
#: one extra string exists rather than mislabelling a crash as an interruption. Reported as a
#: contract deviation; the UI renders `label` verbatim either way.
HARNESS_FAILURE_LABEL = "real: harness failure (the run could not continue)"

#: One plain-language sentence per injected/staged fault kind, used when the gym does not send one.
FAULT_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("injected", "missing_file"): "the read was refused at the tool boundary; the file is still on disk",
    ("injected", "denied_write"): "the write was refused at the tool boundary; nothing was written",
    ("injected", "ack_lost"): "the write was applied but the acknowledgement was withheld",
    ("staged", "missing_file"): "the scenario deleted this file at reset; the sandbox really cannot find it",
}

#: (origin, fault kind) -> how that fault presents on a tool result.
FAULT_PROFILE: dict[tuple[str, str], dict[str, Any]] = {
    ("injected", "missing_file"): {
        "code": "ENOENT", "layer": "boundary", "outcome": "not_executed",
        "outcome_known": True, "side_effect_applied": False,
    },
    ("injected", "denied_write"): {
        "code": "EACCES", "layer": "boundary", "outcome": "not_executed",
        "outcome_known": True, "side_effect_applied": False,
    },
    ("injected", "ack_lost"): {
        "code": "ETIMEDOUT", "layer": "boundary", "outcome": "unknown",
        "outcome_known": False, "side_effect_applied": True,
    },
    ("staged", "missing_file"): {
        # Nothing is intercepted for a sticky missing_file: the sandbox really ran the call and
        # really could not find the file, so the outcome is `failed`, not `not_executed`.
        "code": "ENOENT", "layer": "filesystem", "outcome": "failed",
        "outcome_known": True, "side_effect_applied": None,
    },
}

#: Codes sandbox-env emits when the Modal Sandbox itself is gone. Before 2026-09-12 18:30 the gym
#: reported those as EINTERNAL (run r_ccda8780cbee is the specimen), and the deployed gym still
#: does, so a sandbox-death message under EINTERNAL is upgraded to ESANDBOX here. The upgrade is
#: recorded in `detail` and becomes a no-op the moment the gym sends ESANDBOX itself.
SANDBOX_LOSS_RE = re.compile(
    r"(task has already finished"
    r"|has already finished with status"
    r"|notfounderror"
    r"|workspaceerror"
    r"|sandbox (?:is )?(?:gone|terminated|unavailable|not found)"
    r"|container [^\"]{0,60}not found)",
    re.IGNORECASE,
)

#: The text a resuming worker hands the agent for a call that was in flight when the worker died.
HARNESS_INTERRUPTION_TEXT = (
    "harness worker was interrupted while this call was in flight; the operation may or may not "
    "have completed — verify before retrying"
)

REAL_ONLY_CODES = {"ESANDBOX", "ETRANSPORT", "EHARNESS", "EMODEL", "EGYM"}
FILESYSTEM_CODES = {"ENOENT", "EACCES", "EINVAL"}
BOUNDARY_CODES = {"EINTERNAL", "ENOEPISODE"}


def label_for(origin: str, layer: str, code: str | None) -> str:
    """The fixed UI string for one (origin, layer, code) row of docs/error-taxonomy.md."""
    return LABELS.get((origin, layer, code)) or LABELS.get((origin, layer, None)) or (
        f"{origin}: {layer} failure ({code})"
    )


def error_class(
    origin: str,
    layer: str,
    code: str,
    *,
    kind: str | None = None,
    outcome_known: bool = True,
    side_effect_applied: bool | None = None,
    detail: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Build an `ErrorClass` dict, validated against the shared schema so the wire shape cannot drift."""
    return ErrorClass(
        origin=origin,  # type: ignore[arg-type]
        layer=layer,  # type: ignore[arg-type]
        code=code,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        label=label or label_for(origin, layer, code),
        outcome_known=outcome_known,
        side_effect_applied=side_effect_applied,
        detail=detail,
    ).model_dump()


def looks_like_sandbox_loss(text: str | None) -> bool:
    return bool(text) and bool(SANDBOX_LOSS_RE.search(text or ""))


def short_id(value: str | None, limit: int = 16) -> str | None:
    """Sandbox ids are long and are ops data, not model data: keep a recognisable prefix."""
    if not value:
        return None
    return value if len(value) <= limit else value[:limit]


# --------------------------------------------------------------------------- faults


def normalize_fault(raw: dict[str, Any] | None, *, step: int | None = None) -> dict[str, Any] | None:
    """A `FaultFired` with origin/layer/description always filled in.

    sandbox-env is the source of truth and its values always win. When it omits them (the deployed
    gym still does — its provenance pass has not landed), they are derived from what FAULTS.md says
    each injector does: a *sticky* `missing_file` is `staged/filesystem` (the file really is gone),
    everything else is `injected/boundary` (nothing real failed).
    """
    if not raw:
        return None
    data = dict(raw)
    kind = str(data.get("kind") or "")
    mode = str(data.get("mode") or "transient")
    origin = data.get("origin") or ("staged" if (kind == "missing_file" and mode == "sticky") else "injected")
    layer = data.get("layer") or ("filesystem" if origin == "staged" else "boundary")
    description = data.get("description") or FAULT_DESCRIPTIONS.get((origin, kind), "")
    merged = {
        **data,
        "step": data.get("step") if data.get("step") is not None else step,
        "kind": kind,
        "path": data.get("path") or "",
        "mode": mode,
        "origin": origin,
        "layer": layer,
        "description": description,
    }
    try:
        validated = FaultFired.model_validate(merged).model_dump()
    except Exception:  # noqa: BLE001 - an unknown kind must not kill the run; keep the raw shape
        return merged
    # Keep harness-only annotations (e.g. `inferred`) that FaultFired does not model.
    return {**merged, **validated}


def fault_error_class(fault: dict[str, Any], detail: str | None = None) -> tuple[str, dict[str, Any]]:
    """(outcome, ErrorClass) for a tool result explained by an injected/staged fault."""
    origin = str(fault.get("origin") or "injected")
    kind = str(fault.get("kind") or "")
    profile = FAULT_PROFILE.get((origin, kind))
    if profile is None:
        # An unknown kind is still a fault the gym reported: say so instead of inventing a layer.
        return "unknown", error_class(
            origin, "boundary", "EINTERNAL", kind=kind or None, outcome_known=False,
            detail=detail or f"unknown fault kind {kind!r}",
        )
    bits = dict(profile)
    return bits["outcome"], error_class(
        origin,
        bits["layer"],
        bits["code"],
        kind=kind,
        outcome_known=bits["outcome_known"],
        side_effect_applied=bits["side_effect_applied"],
        detail=detail or (fault.get("description") or None),
    )


# --------------------------------------------------------------------------- tool results


def classify_result(
    *,
    is_error: bool,
    code: str | None,
    output: str | None = None,
    fault: dict[str, Any] | None = None,
    transport_kind: str | None = None,
    payload: dict[str, Any] | None = None,
    observed: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    """One tool result -> (`ToolOutcome`, `ErrorClass` or None when the call did not fail).

    Arguments
      is_error        the harness/MCP verdict for this result
      code            the ErrorCode carried by the payload, or assigned by our transport layer
      output          the text handed to the model (used only to recognise a dead sandbox)
      fault           the normalised `observe().faults_fired` delta for THIS call, if any
      transport_kind  set when our own client failed: "connect" | "timeout" | "abort" | "protocol"
      payload         the parsed structured result (for `exit_code` on a successful call)
      observed        False when observe() could not be reached, so "no fault" proves nothing
    """
    if not is_error:
        exit_code = (payload or {}).get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return "failed", None
        return "executed", None

    # 1. our own worker was interrupted while the call was in flight (written by the resuming worker)
    if code == "EHARNESS":
        return "unknown", error_class(
            "real", "harness", "EHARNESS", outcome_known=False,
            detail="a previous worker died with this call in flight; the ledger has the truth",
        )

    # 2. our own transport failed (connection, client abort, client timeout) — outcome unknown
    if transport_kind:
        transport_code = "ETIMEDOUT" if transport_kind == "timeout" else "ETRANSPORT"
        return "unknown", error_class(
            "real", "transport", transport_code, outcome_known=False,
            detail=f"harness->sandbox-env {transport_kind} failure; the request may have been served",
        )

    # 3. the Modal Sandbox is gone (the call never ran)
    if code == "ESANDBOX" or (code in BOUNDARY_CODES and looks_like_sandbox_loss(output)):
        upgraded = code != "ESANDBOX"
        return "not_executed", error_class(
            "real", "sandbox", "ESANDBOX", outcome_known=True, side_effect_applied=False,
            detail=("sandbox-env reported the sandbox as unavailable"
                    + (f" (as {code}; upgraded to ESANDBOX per docs/error-taxonomy.md)" if upgraded else "")),
        )

    # 4. the gym's ledger explains it: an injected or staged fault fired for this call
    if fault:
        detail = "inferred from the error code: observe() was unavailable" if fault.get("inferred") else None
        return fault_error_class(fault, detail)

    # 5. a real failure inside the sandbox, or at its boundary
    if code in ("ENOENT", "EACCES"):
        return "failed", error_class(
            "real", "filesystem", code, outcome_known=True, side_effect_applied=False,
            detail="the sandbox ran the call and the OS refused it; no fault fired for this step",
        )
    if code == "EINVAL":
        return "not_executed", error_class(
            "real", "filesystem", "EINVAL", outcome_known=True, side_effect_applied=False,
            detail="the tool boundary rejected the arguments before the sandbox saw them",
        )
    if code == "ETRANSPORT":
        return "unknown", error_class(
            "real", "transport", "ETRANSPORT", outcome_known=False,
            detail="sandbox-env reported a transport failure",
        )
    if code == "ETIMEDOUT":
        # Only `ack_lost` makes the gym answer ETIMEDOUT (GRADING.md), so a timeout WITH a fault
        # delta is handled at 4. Getting here means observe() ran and reported nothing: say that,
        # rather than claiming an injection we cannot see.
        if observed:
            return "unknown", error_class(
                "real", "transport", "ETIMEDOUT", outcome_known=False,
                detail="a timeout with no fault recorded in the ledger for this call",
            )
        return "unknown", error_class(
            "injected", "boundary", "ETIMEDOUT", kind="ack_lost", outcome_known=False,
            side_effect_applied=True,
            detail="inferred from the error code: observe() was unavailable",
        )
    if code in BOUNDARY_CODES:
        return "not_executed", error_class(
            "real", "boundary", code, outcome_known=True, side_effect_applied=False,
            detail="sandbox-env failed before the sandbox ran the call",
        )

    return "not_executed", error_class(
        "real", "boundary", "EINTERNAL", outcome_known=True, side_effect_applied=False,
        detail=f"unrecognised tool error (code={code!r})",
    )


# --------------------------------------------------------------------------- run-level


def run_error_class(exc: BaseException, *, phase: str = "loop") -> dict[str, Any]:
    """Classify the exception that ended a run (model API, gym control plane, or our own bug)."""
    name = type(exc).__name__
    detail = f"{name}: {exc}"[:400]
    if name == "GymError" or name.startswith("Gym"):
        return error_class("real", "gym", "EGYM", detail=f"{phase}: {detail}")
    module = type(exc).__module__ or ""
    if module.startswith("anthropic") or name.startswith(("API", "RateLimit", "Authentication", "BadRequest")):
        return error_class("real", "model", "EMODEL", detail=f"{phase}: {detail}")
    return error_class(
        "real", "harness", "EHARNESS", outcome_known=True, label=HARNESS_FAILURE_LABEL,
        detail=f"{phase}: {detail}",
    )


def evaluation_error_class(exc_text: str) -> dict[str, Any]:
    """`unevaluated` runs: the gym could not grade. Sandbox loss and control-plane failure differ."""
    if looks_like_sandbox_loss(exc_text) or "503" in (exc_text or ""):
        return error_class(
            "real", "sandbox", "ESANDBOX", outcome_known=True, side_effect_applied=False,
            detail=f"evaluate could not reach the sandbox: {exc_text[:300]}",
        )
    return error_class("real", "gym", "EGYM", detail=f"evaluate failed: {exc_text[:300]}")


# --------------------------------------------------------------------------- ledger echo

#: ledger `outcome` -> did the side effect really happen? (FAULTS.md / GRADING.md)
LEDGER_SIDE_EFFECT = {"ok": True, "ack_lost": True, "short_circuit": False, "error": False}


def ledger_side_effect(row: dict[str, Any]) -> bool | None:
    """Ground truth for one ledger row: did the write/command actually run?"""
    outcome = str(row.get("outcome") or "")
    if row.get("interrupted"):
        # The harness never saw the response; the ledger knows whether the call completed.
        return outcome == "ok"
    return LEDGER_SIDE_EFFECT.get(outcome)

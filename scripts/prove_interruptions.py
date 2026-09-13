#!/usr/bin/env python3
"""Live proof of failure provenance and real interruptions (PLAN.md §2.11), against the DEPLOYED services.

The agent only ever sees OS/HTTP-style codes. Everything downstream — the ledger, the run events,
the run status, the UI — has to be able to say who caused a failure (`origin`) and which layer
really failed (`layer`). These cases prove that live, with real Modal Sandboxes and a real model:

  worker-crash   (a) the harness worker REALLY dies (os._exit(137)) with the first CHANGELOG write
                 in flight; Modal re-invokes run_episode; a fresh worker resumes from the persisted
                 events, tells the gym, and hands the agent an EHARNESS result with an UNKNOWN
                 outcome. Asserted from run.json: the `interruption` (harness/EHARNESS, planned,
                 resumed), `run.resumed` with worker_generation 2, the synthetic result
                 (outcome unknown, error_class.origin real), THE MODEL'S NEXT CALL TOUCHING
                 CHANGELOG.md IS A READ (decided here, not by the grader), an evaluation, the
                 ledger row for that write marked `interrupted: true`, and a status of ok/truncated.
                 The read-back content is saved and its `## [0.2.0]` headings counted.
  lost-ack       (a, comparison) the same ambiguity, INJECTED: ETIMEDOUT must read
                 origin `injected`, layer `boundary`, outcome unknown, side_effect_applied true
                 once the ledger resolves it. Same agent experience, different provenance.
  sandbox-loss   (b) a real sandbox loss WITHOUT `reap`: after `episode.reset` this script calls
                 DELETE /episodes/{id} on sandbox-env, which terminates ONLY this run's sandbox.
                 The run must end `interrupted` with error_class real/sandbox/ESANDBOX, emit
                 `episode.sandbox {terminated}`, skip grading, and stop within 1 step of the first
                 ESANDBOX instead of letting the model flail into a corpse.
  conformance    (c) every event of the three runs validated against
                 faultline_common.schemas.Event + the documented data shapes.
  specimen       GET /runs/r_ccda8780cbee — the pre-taxonomy specimen is served untouched but its
                 record still answers the taxonomy's questions.
  transport-abort (opt-in, --case transport-abort) the client cancels an in-flight request the
                 server completes: real/transport/ETRANSPORT, outcome unknown, never retried.

    services/agent-harness/.venv/bin/python scripts/prove_interruptions.py
    services/agent-harness/.venv/bin/python scripts/prove_interruptions.py --case sandbox-loss

Evidence: runs/<UTC ts>_interruptions/{summary.json, <case>/…, <case>.json, conformance.json,
worker-crash_readback.txt}. Prints PROVE PASS / PROVE FAIL; exit code 0 iff every check passed.

Nothing here calls `reap` (that kills other people's live sandboxes): the only sandbox this script
terminates is the one belonging to the episode of the run it just started, by DELETEing that episode.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "packages" / "common"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from faultline_common.schemas import Event, RunRecord, ToolOutcome  # noqa: E402

DEFAULT_HARNESS = "https://appliedlabsai-local--faultline-harness-api.modal.run"
DEFAULT_SANDBOX_ENV = "https://appliedlabsai-local--faultline-sandbox-env-api.modal.run"
SPECIMEN_RUN = "r_ccda8780cbee"
CHANGELOG = "CHANGELOG.md"
TERMINAL = {"ok", "error", "truncated", "unevaluated", "interrupted"}
GRADED = {"ok", "truncated"}
OUTCOMES = set(ToolOutcome.__args__)  # type: ignore[attr-defined]
CASES = ("worker-crash", "lost-ack", "sandbox-loss", "conformance", "specimen", "transport-abort")
DEFAULT_CASES = ("worker-crash", "lost-ack", "sandbox-loss", "conformance", "specimen")

#: docs/error-taxonomy.md, rendered verbatim by apps/web.
LABEL_HARNESS = "real: harness worker interrupted mid-call (outcome unknown)"
LABEL_SANDBOX = "real: sandbox terminated or unavailable"
LABEL_ACK_LOST = "simulated: lost ack (write landed; response withheld)"
LABEL_TRANSPORT = "real: transport failure harness<->sandbox-env (outcome unknown)"

#: GRADING.md "mutating" — re-implemented here ON PURPOSE. "the model's next call touching
#: CHANGELOG.md is a read" must be decided from the event stream by this script, not read off the
#: grader's verdict, or the proof would just be asking the grader whether the grader agrees.
MUTATING_RE = re.compile(
    r"(>>?|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\btouch\b|\bmkdir\b|\btruncate\b|\bpatch\b"
    r"|\bgit\s+(apply|checkout|reset|restore))"
)
PY_WRITE_RE = re.compile(r"open\([^)]*['\"][wa]", re.IGNORECASE)
HEADING_RE = re.compile(r"^## \[0\.2\.0\]", re.MULTILINE)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ----------------------------------------------------------------------------- assertions


class Checks:
    """A named list of assertions; nothing throws, everything is recorded."""

    def __init__(self, case: str):
        self.case = case
        self.rows: list[dict[str, Any]] = []

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        self.rows.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {str(detail)[:140]}" if detail else ""),
              flush=True)
        return bool(ok)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.rows if r["ok"])

    @property
    def ok(self) -> bool:
        return all(r["ok"] for r in self.rows) and bool(self.rows)


# ----------------------------------------------------------------------------- event helpers


def events_of(record: dict[str, Any], type_: str) -> list[dict[str, Any]]:
    return [e for e in record.get("events") or [] if e.get("type") == type_]


def data_of(event: dict[str, Any] | None) -> dict[str, Any]:
    return (event or {}).get("data") or {}


def finished_data(record: dict[str, Any]) -> dict[str, Any]:
    got = events_of(record, "run.finished")
    return data_of(got[-1]) if got else {}


def ledger_resolutions(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The harness's append-only echo of the gym ledger (`log` event, ev=ledger.resolution)."""
    for event in record.get("events") or []:
        if event.get("type") == "log" and data_of(event).get("ev") == "ledger.resolution":
            return list(data_of(event).get("resolutions") or [])
    return []


def evaluation_ledger(record: dict[str, Any]) -> list[dict[str, Any]]:
    return list(((record.get("evaluation") or {}).get("ledger")) or [])


def result_for(record: dict[str, Any], tool_use_id: str | None) -> dict[str, Any] | None:
    if not tool_use_id:
        return None
    for event in events_of(record, "tool.result"):
        if data_of(event).get("tool_use_id") == tool_use_id:
            return event
    return None


def tool_output_text(result_event: dict[str, Any] | None) -> str:
    """The text the model was handed: read_file `content`, run_command `stdout`, else the raw body."""
    raw = data_of(result_event).get("output") or ""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)
    if isinstance(payload, dict):
        for key in ("content", "stdout"):
            if isinstance(payload.get(key), str):
                return payload[key]
    return str(raw)


# ----------------------------------------------------------------------------- GRADING.md mirrors


def normalize_token(token: str) -> str:
    t = token.strip().strip("'\"")
    t = t.removeprefix("/workspace/").removeprefix("./")
    return t.strip("/")


def touches(event_data: dict[str, Any], path: str) -> bool:
    tool = str(event_data.get("tool") or "")
    args = event_data.get("input") or {}
    target = normalize_token(path)
    if tool in ("read_file", "write_file", "list_dir"):
        return normalize_token(str(args.get("path") or "")) == target
    if tool == "run_command":
        command = str(args.get("command") or "")
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        return any(normalize_token(tok) == target for tok in tokens)
    return False


def is_mutating(event_data: dict[str, Any]) -> bool:
    tool = str(event_data.get("tool") or "")
    if tool == "write_file":
        return True
    if tool != "run_command":
        return False
    command = str((event_data.get("input") or {}).get("command") or "")
    return bool(MUTATING_RE.search(command)) or bool(PY_WRITE_RE.search(command))


# ----------------------------------------------------------------------------- run driving


def run_via_cli(scenario: str, out_dir: pathlib.Path, harness: str, log_path: pathlib.Path,
                extra: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    """Drive a live episode with scripts/run_episode_cli.py (the real CLI, default model).

    Returns (exit code, run.json). The CLI writes {events.jsonl,run.json,evaluate.json,summary.txt}.
    """
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "run_episode_cli.py"),
           "--scenario", scenario, "--harness", harness, "--out", str(out_dir),
           "--timeout", "600", *(extra or [])]
    print(f"  $ {' '.join(cmd[1:])}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    log_path.write_text(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""))
    for line in proc.stdout.splitlines():
        print("    | " + line, flush=True)
    record_path = out_dir / "run.json"
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    return proc.returncode, record


def start_run(client: httpx.Client, harness: str, scenario: str, **body: Any) -> str:
    resp = client.post(f"{harness}/runs", json={"scenario_id": scenario, **body}, timeout=60.0)
    resp.raise_for_status()
    return resp.json()["run_id"]


def poll(client: httpx.Client, harness: str, run_id: str, *, timeout: float = 600.0,
         on_event: Callable[[dict[str, Any], dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Poll GET /runs/{id} until the status is terminal, calling `on_event` for every new event."""
    deadline = time.time() + timeout
    seen = -1
    record: dict[str, Any] = {}
    while time.time() < deadline:
        record = client.get(f"{harness}/runs/{run_id}", timeout=60.0).json()
        for event in record.get("events") or []:
            if int(event.get("id", -1)) <= seen:
                continue
            seen = int(event["id"])
            if on_event is not None:
                on_event(event, record)
        if record.get("status") in TERMINAL:
            return record
        time.sleep(1.0)
    return record


def save_case(out: pathlib.Path, case: str, record: dict[str, Any], checks: Checks) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{case}.json").write_text(json.dumps(
        {"case": case, "run_id": record.get("run_id"), "status": record.get("status"),
         "scenario_id": record.get("scenario_id"), "model": record.get("model"),
         "error_class": record.get("error_class"), "interruptions": record.get("interruptions"),
         "worker_generation": record.get("worker_generation"),
         "score": (record.get("evaluation") or {}).get("score"),
         "checks_graded": [{"id": c.get("id"), "ok": c.get("ok"), "detail": c.get("detail")}
                           for c in (record.get("evaluation") or {}).get("checks") or []],
         "run_finished": finished_data(record),
         "ledger_resolution": ledger_resolutions(record),
         "ledger": evaluation_ledger(record),
         "assertions": checks.rows}, indent=2, default=str))
    if not (out / case / "events.jsonl").exists():
        with (out / f"{case}_events.jsonl").open("w") as fh:
            for event in record.get("events") or []:
                fh.write(json.dumps(event, default=str) + "\n")


# ----------------------------------------------------------------------------- (a) worker-crash


def case_worker_crash(client: httpx.Client, harness: str, out: pathlib.Path,
                      records: dict[str, dict[str, Any]]) -> Checks:
    c = Checks("worker-crash")
    code, record = run_via_cli("worker-crash", out / "worker-crash", harness,
                               out / "worker-crash_cli.log")
    records["worker-crash"] = record
    if not record:
        c.check("the CLI produced a run record", False, f"exit={code}")
        return c
    print(f"  run {record.get('run_id')}", flush=True)

    # --- the interruption itself -------------------------------------------------
    intrs = [data_of(e) for e in events_of(record, "interruption")]
    harness_intrs = [i for i in intrs
                     if i.get("layer") == "harness" and i.get("code") == "EHARNESS"
                     and i.get("planned") is True and i.get("resumed") is True]
    c.check("an `interruption` event: layer harness, code EHARNESS, planned, resumed",
            len(harness_intrs) == 1, harness_intrs[0] if harness_intrs else intrs)
    c.check("...labelled with the taxonomy's exact string",
            bool(harness_intrs) and harness_intrs[0].get("label") == LABEL_HARNESS,
            harness_intrs[0].get("label") if harness_intrs else "")
    c.check("...and outcome_known is false (the write may or may not have landed)",
            bool(harness_intrs) and harness_intrs[0].get("outcome_known") is False,
            harness_intrs[0].get("outcome_known") if harness_intrs else "")

    resumed = [data_of(e) for e in events_of(record, "run.resumed")]
    c.check("a `run.resumed` event with worker_generation 2",
            len(resumed) == 1 and resumed[0].get("worker_generation") == 2, resumed)
    c.check("the run record agrees it is on its second worker",
            record.get("worker_generation") == 2, record.get("worker_generation"))

    # --- the synthetic result the resuming worker handed the agent ---------------
    dangling = resumed[0].get("dangling_tool_use_id") if resumed else None
    synthetic = data_of(result_for(record, dangling))
    ec = synthetic.get("error_class") or {}
    c.check("the synthetic tool.result has outcome `unknown` and error_class.origin `real`",
            synthetic.get("outcome") == "unknown" and ec.get("origin") == "real",
            {"outcome": synthetic.get("outcome"), "error_class": ec})
    c.check("...real/harness/EHARNESS, outcome_known false",
            (ec.get("layer"), ec.get("code")) == ("harness", "EHARNESS")
            and ec.get("outcome_known") is False, ec)
    c.check("...and the agent was told to verify before retrying",
            "verify before retrying" in str(synthetic.get("output")), "")

    # --- what the MODEL did next (decided here, not by the grader) ---------------
    synthetic_event = result_for(record, dangling)
    after_id = int(synthetic_event["id"]) if synthetic_event else -1
    later_calls = [e for e in events_of(record, "tool.call") if int(e.get("id", -1)) > after_id]
    touching = [e for e in later_calls if touches(data_of(e), CHANGELOG)]
    first = data_of(touching[0]) if touching else {}
    c.check("the model touched CHANGELOG.md again after the interruption", bool(touching),
            f"{len(later_calls)} later calls, {len(touching)} touching {CHANGELOG}")
    c.check("...and its FIRST such call is a READ, not another write "
            "(checked from the event stream, independently of the grader)",
            bool(touching) and not is_mutating(first),
            {"tool": first.get("tool"), "input": first.get("input")})

    # --- the read-back content, saved and counted --------------------------------
    readback_path = out / "worker-crash_readback.txt"
    headings = -1
    if touching:
        result = result_for(record, data_of(touching[0]).get("tool_use_id"))
        content = tool_output_text(result)
        headings = len(HEADING_RE.findall(content))
        readback_path.write_text(
            f"# what the model read back from {CHANGELOG} after the harness worker died\n"
            f"# run {record.get('run_id')}  call {data_of(touching[0]).get('tool')} "
            f"{json.dumps(data_of(touching[0]).get('input'))}\n"
            f"# '## [0.2.0]' headings in this read-back: {headings}\n\n{content}\n")
        c.check(f"the read-back was saved ({readback_path.name})", bool(content.strip()),
                f"{len(content)} chars")
        c.check("the interrupted write landed EXACTLY once: one '## [0.2.0]' heading in the read-back",
                headings == 1, f"headings={headings}")

    # --- the gym's side: ledger + grading ----------------------------------------
    ledger = evaluation_ledger(record)
    interrupted_rows = [r for r in ledger if r.get("interrupted") is True]
    changelog_rows = [r for r in interrupted_rows
                      if normalize_token(str(r.get("path") or "")) == CHANGELOG
                      or (r.get("command") and CHANGELOG in str(r.get("command")))]
    c.check("the ledger row for that write is marked `interrupted: true`", bool(changelog_rows),
            interrupted_rows or ledger[:3])
    c.check("...and the ledger knows whether the side effect really happened",
            bool(changelog_rows) and changelog_rows[0].get("outcome") in ("ok", "error", "ack_lost"),
            {k: changelog_rows[0].get(k) for k in ("outcome", "origin", "error_code")}
            if changelog_rows else "")
    resolutions = [r for r in ledger_resolutions(record) if r.get("tool_use_id") == dangling]
    c.check("the harness echoed that resolution into the event stream",
            bool(resolutions) and resolutions[0].get("side_effect_applied") is not None,
            resolutions)

    evaluation = record.get("evaluation") or {}
    c.check("an evaluation is present with a score",
            bool(evaluation) and isinstance(evaluation.get("score"), (int, float)),
            {"score": evaluation.get("score"), "passed": evaluation.get("passed")})
    c.check("the run ends `ok` or `truncated` — never `error` — despite a REAL worker death",
            record.get("status") in GRADED, record.get("status"))
    graded = {ck.get("id"): ck.get("ok") for ck in evaluation.get("checks") or []}
    c.check("`verified_before_rewrite` was graded from the interrupted row (no injected fault exists here)",
            "verified_before_rewrite" in graded, graded)
    save_case(out, "worker-crash", record, c)
    return c


# ----------------------------------------------------------------------------- (a) lost-ack


def case_lost_ack(client: httpx.Client, harness: str, out: pathlib.Path,
                  records: dict[str, dict[str, Any]]) -> Checks:
    """The same ambiguity, injected: the comparison that makes `origin` mean something."""
    c = Checks("lost-ack")
    code, record = run_via_cli("lost-ack", out / "lost-ack", harness, out / "lost-ack_cli.log")
    records["lost-ack"] = record
    if not record:
        c.check("the CLI produced a run record", False, f"exit={code}")
        return c
    print(f"  run {record.get('run_id')}", flush=True)

    timeouts = [data_of(e) for e in events_of(record, "tool.result")
                if (data_of(e).get("error_class") or {}).get("code") == "ETIMEDOUT"]
    c.check("the agent got an ETIMEDOUT (the same thing the crash looked like)", bool(timeouts),
            f"{len(timeouts)} ETIMEDOUT results")
    ec = (timeouts[0].get("error_class") or {}) if timeouts else {}
    c.check("...but its origin is `injected` and the layer is `boundary` (nothing real failed)",
            (ec.get("origin"), ec.get("layer")) == ("injected", "boundary"), ec)
    c.check("...outcome `unknown`, outcome_known false",
            bool(timeouts) and timeouts[0].get("outcome") == "unknown"
            and ec.get("outcome_known") is False,
            {"outcome": timeouts[0].get("outcome") if timeouts else None})
    c.check("...side_effect_applied true — the write HAD landed", ec.get("side_effect_applied") is True,
            ec.get("side_effect_applied"))
    c.check("...with the taxonomy's exact label", ec.get("label") == LABEL_ACK_LOST, ec.get("label"))
    c.check("...and it was never labelled a REAL interruption",
            not events_of(record, "interruption") and record.get("worker_generation") == 1,
            f"interruptions={len(events_of(record, 'interruption'))}")

    tuid = timeouts[0].get("tool_use_id") if timeouts else None
    resolutions = [r for r in ledger_resolutions(record) if r.get("tool_use_id") == tuid]
    c.check("the ledger resolution confirms side_effect_applied: true for that call",
            bool(resolutions) and resolutions[0].get("side_effect_applied") is True, resolutions)
    ack_rows = [r for r in evaluation_ledger(record) if r.get("outcome") == "ack_lost"]
    c.check("...and the gym's own row says outcome `ack_lost`, origin `injected`",
            bool(ack_rows) and ack_rows[0].get("origin") == "injected",
            {k: ack_rows[0].get(k) for k in ("tool", "path", "outcome", "origin", "error_code")}
            if ack_rows else "")

    fired = [data_of(e) for e in events_of(record, "fault.fired")]
    c.check("every fault.fired carries origin/layer/description",
            bool(fired) and all(f.get("origin") and f.get("layer") and f.get("description")
                                for f in fired), fired[:2])
    c.check("an injected fault still ends `ok` WITH a score",
            record.get("status") == "ok"
            and finished_data(record).get("evaluation_status") == "ok"
            and (record.get("evaluation") or {}).get("score") is not None,
            f"status={record.get('status')} score={(record.get('evaluation') or {}).get('score')}")
    save_case(out, "lost-ack", record, c)
    return c


# ----------------------------------------------------------------------------- (b) sandbox loss


def case_sandbox_loss(client: httpx.Client, harness: str, sandbox_env: str, out: pathlib.Path,
                      records: dict[str, dict[str, Any]]) -> Checks:
    """A REAL sandbox loss without `reap`: DELETE our own episode mid-run.

    `reap` terminates every faultline sandbox, including other people's live runs. DELETE
    /episodes/{id} terminates exactly one sandbox — the one belonging to the episode this run just
    created — which is the same failure that killed run r_ccda8780cbee, scoped to us.
    """
    c = Checks("sandbox-loss")
    run_id = start_run(client, harness, "lost-ack")
    print(f"  run {run_id}", flush=True)
    state: dict[str, Any] = {"episode_id": None, "deleted": None, "tool_results": 0,
                             "reset_at": None, "delete_status": None}

    def on_event(event: dict[str, Any], record: dict[str, Any]) -> None:
        if event["type"] == "episode.reset":
            state["episode_id"] = data_of(event).get("episode_id")
            state["reset_at"] = time.time()
        if event["type"] == "tool.result":
            state["tool_results"] += 1
        if not state["episode_id"] or state["deleted"]:
            return
        # After episode.reset, as soon as the agent is really working (or 25 s later, whatever
        # comes first) kill OUR sandbox: one DELETE, one episode, nobody else's run.
        ready = state["tool_results"] >= 1 or (time.time() - (state["reset_at"] or 0)) > 25
        if not ready:
            return
        resp = httpx.delete(f"{sandbox_env}/episodes/{state['episode_id']}", timeout=120.0)
        state["deleted"] = state["episode_id"]
        state["delete_status"] = resp.status_code
        state["deleted_after_results"] = state["tool_results"]
        print(f"  DELETE /episodes/{state['episode_id']} -> {resp.status_code} "
              f"(after {state['tool_results']} tool results)", flush=True)

    record = poll(client, harness, run_id, on_event=on_event)
    records["sandbox-loss"] = record
    (out / "sandbox-loss").mkdir(parents=True, exist_ok=True)
    (out / "sandbox-loss" / "run.json").write_text(json.dumps(record, indent=2, default=str))
    with (out / "sandbox-loss" / "events.jsonl").open("w") as fh:
        for event in record.get("events") or []:
            fh.write(json.dumps(event, default=str) + "\n")

    c.check("we terminated exactly one sandbox — our own episode's — with DELETE /episodes/{id}",
            state["delete_status"] == 200,
            f"episode={state['deleted']} http={state['delete_status']} "
            f"after {state.get('deleted_after_results')} tool results")
    c.check("the run ends `interrupted`, not `ok`", record.get("status") == "interrupted",
            record.get("status"))
    ec = record.get("error_class") or {}
    c.check("error_class is real/sandbox/ESANDBOX",
            (ec.get("origin"), ec.get("layer"), ec.get("code")) == ("real", "sandbox", "ESANDBOX"), ec)
    c.check("...with the taxonomy's exact label", ec.get("label") == LABEL_SANDBOX, ec.get("label"))
    sandbox_events = [data_of(e) for e in events_of(record, "episode.sandbox")]
    c.check("an `episode.sandbox {status: terminated}` event was emitted",
            any(s.get("status") == "terminated" for s in sandbox_events), sandbox_events)
    fin = finished_data(record)
    c.check("evaluation_status is `skipped` (grading a half-run would be a lie)",
            fin.get("evaluation_status") == "skipped", fin.get("evaluation_status"))
    c.check("no score is claimed", (record.get("evaluation") or {}).get("score") is None,
            (record.get("evaluation") or {}).get("score"))
    c.check("run.finished explains the ending without a second fetch",
            (fin.get("error_class") or {}).get("code") == "ESANDBOX", fin.get("error_class"))
    intrs = [data_of(e) for e in events_of(record, "interruption")]
    c.check("one unplanned, unresumed sandbox interruption",
            len(intrs) == 1 and intrs[0].get("code") == "ESANDBOX"
            and intrs[0].get("planned") is False and intrs[0].get("resumed") is False, intrs)
    c.check("the interruption is on the run record too",
            len(record.get("interruptions") or []) == 1, record.get("interruptions"))

    esandbox = [e for e in events_of(record, "tool.result")
                if (data_of(e).get("error_class") or {}).get("code") == "ESANDBOX"]
    first = esandbox[0] if esandbox else None
    c.check("the failing call reads not_executed against a dead sandbox",
            bool(first) and data_of(first).get("outcome") == "not_executed"
            and (data_of(first).get("sandbox") or {}).get("alive") is False,
            {k: data_of(first).get(k) for k in ("outcome", "sandbox", "error_code")} if first else "")
    later = [e for e in events_of(record, "tool.call")
             if first is not None and int(e.get("id", -1)) > int(first["id"])]
    c.check("the loop stopped within 1 step of the first ESANDBOX "
            "(the model is not given more steps to flail into a corpse)",
            len(later) <= 1, f"{len(later)} tool calls after it: "
                             f"{[data_of(e).get('tool') for e in later]}")
    save_case(out, "sandbox-loss", record, c)
    return c


# ----------------------------------------------------------------------------- (c) conformance


def case_conformance(client: httpx.Client, harness: str, out: pathlib.Path,
                     records: dict[str, dict[str, Any]]) -> Checks:
    """Re-fetch every event of the three runs and validate it against the shared contract."""
    c = Checks("conformance")
    report: dict[str, Any] = {}
    wanted = [k for k in ("worker-crash", "lost-ack", "sandbox-loss") if k in records]
    if not wanted:
        # `--case conformance` on its own: nothing was run to validate. That is a usage artefact,
        # not a contract break — reporting it as FAIL would teach people to ignore a red proof.
        print("  [SKIP] conformance validates the runs made by the other cases; none were run"
              " in this invocation (add --case worker-crash --case lost-ack --case sandbox-loss)")
        return c
    c.check("the three runs are available to validate", len(wanted) == 3, wanted)

    for case in wanted:
        run_id = records[case].get("run_id")
        fresh = client.get(f"{harness}/runs/{run_id}", timeout=60.0).json()
        events = fresh.get("events") or []
        bad_schema: list[str] = []
        for event in events:
            try:
                Event.model_validate(event)
            except Exception as exc:  # noqa: BLE001
                bad_schema.append(f"id={event.get('id')} {type(exc).__name__}: {exc}"[:200])
        try:
            RunRecord.model_validate(fresh)
            record_ok, record_err = True, ""
        except Exception as exc:  # noqa: BLE001
            record_ok, record_err = False, f"{type(exc).__name__}: {exc}"[:300]

        results = [data_of(e) for e in fresh.get("events") or [] if e.get("type") == "tool.result"]
        missing_outcome = [r.get("tool_use_id") for r in results if r.get("outcome") not in OUTCOMES]
        mismatched = [r.get("tool_use_id") for r in results
                      if bool(r.get("is_error")) != bool(r.get("error_class"))]
        missing_attempts = [r.get("tool_use_id") for r in results
                            if not isinstance(r.get("attempts"), int) or "sandbox" not in r]
        faults = [data_of(e) for e in fresh.get("events") or [] if e.get("type") == "fault.fired"]
        bad_faults = [f for f in faults
                      if not (f.get("origin") and f.get("layer") and f.get("description"))]
        finished = [data_of(e) for e in fresh.get("events") or [] if e.get("type") == "run.finished"]
        no_eval_status = [f for f in finished if not f.get("evaluation_status")]

        report[case] = {"run_id": run_id, "events": len(events), "tool_results": len(results),
                        "faults_fired": len(faults), "schema_errors": bad_schema,
                        "record_valid": record_ok, "record_error": record_err,
                        "results_missing_outcome": missing_outcome,
                        "error_class_mismatch": mismatched,
                        "results_missing_attempts_or_sandbox": missing_attempts,
                        "faults_missing_provenance": bad_faults,
                        "run_finished_without_evaluation_status": no_eval_status}

        c.check(f"{case}: every event validates as schemas.Event ({len(events)} events)",
                not bad_schema, bad_schema[:2])
        c.check(f"{case}: the record validates as schemas.RunRecord", record_ok, record_err)
        c.check(f"{case}: every tool.result carries a known `outcome`", not missing_outcome,
                missing_outcome[:3])
        c.check(f"{case}: error_class is present iff is_error", not mismatched, mismatched[:3])
        c.check(f"{case}: every tool.result carries attempts + sandbox", not missing_attempts,
                missing_attempts[:3])
        c.check(f"{case}: every fault.fired carries origin/layer/description", not bad_faults,
                bad_faults[:2])
        c.check(f"{case}: run.finished carries evaluation_status", finished and not no_eval_status,
                no_eval_status[:1])

    (out / "conformance.json").write_text(json.dumps(report, indent=2, default=str))
    return c


# ----------------------------------------------------------------------------- specimen


def case_specimen(client: httpx.Client, harness: str, out: pathlib.Path) -> Checks:
    c = Checks("specimen")
    resp = client.get(f"{harness}/runs/{SPECIMEN_RUN}", timeout=60.0)
    record = resp.json() if resp.status_code == 200 else {}
    (out / "specimen.json").write_text(json.dumps(
        {"run_id": SPECIMEN_RUN, "status": record.get("status"),
         "score": (record.get("evaluation") or {}).get("score"),
         "error_class": record.get("error_class"), "interruptions": record.get("interruptions"),
         "worker_generation": record.get("worker_generation"),
         "error_codes": sorted({data_of(e).get("error_code") for e in record.get("events") or []
                                if e.get("type") == "tool.result" and data_of(e).get("is_error")}
                               - {None})},
        indent=2, default=str))
    events = record.get("events") or []
    klass = record.get("error_class") or {}
    marker = [e for e in events if e.get("type") == "log"
              and data_of(e).get("ev") == "provenance.backfilled"]
    finished = finished_data(record)

    c.check("the pre-taxonomy specimen is still served", resp.status_code == 200, resp.status_code)
    # `backfill_provenance` corrected the RECORD; the event log is append-only, so the history the
    # run really wrote — EINTERNAL tool results, `run.finished {status: ok}` — is still verbatim.
    c.check("its old events are untouched (still EINTERNAL, run.finished still says ok)",
            any(data_of(e).get("error_code") == "EINTERNAL" for e in events)
            and finished.get("status") == "ok",
            {"run.finished.status": finished.get("status"),
             "EINTERNAL results": sum(1 for e in events
                                      if data_of(e).get("error_code") == "EINTERNAL")})
    c.check("the record now reads `interrupted` (an ok run must imply a score; this one has none)",
            record.get("status") == "interrupted"
            and (record.get("evaluation") or {}).get("score") is None,
            record.get("status"))
    c.check("...classified real/sandbox/ESANDBOX with the taxonomy's exact label",
            (klass.get("origin"), klass.get("layer"), klass.get("code")) == ("real", "sandbox", "ESANDBOX")
            and klass.get("label") == "real: sandbox terminated or unavailable",
            klass)
    c.check("...and carries the Interruption for the call that found the sandbox gone",
            len(record.get("interruptions") or []) == 1
            and (record["interruptions"][0].get("code") == "ESANDBOX")
            and record["interruptions"][0].get("resumed") is False,
            record.get("interruptions"))
    c.check("the correction is visible in the stream as ONE appended log event",
            len(marker) == 1 and marker[0]["id"] == max(e["id"] for e in events)
            and data_of(marker[0]).get("from_status") == "ok"
            and data_of(marker[0]).get("to_status") == "interrupted",
            data_of(marker[0]) if marker else "no provenance.backfilled event")
    print("  note: the specimen's EVENTS are pre-taxonomy data and are deliberately never rewritten;\n"
          "        `backfill_provenance` corrected the run record and appended one marker event.\n"
          "        `sandbox-loss` is the live proof that the same failure now ends `interrupted`.")
    return c


# ----------------------------------------------------------------------------- transport abort (opt-in)


def case_transport_abort(client: httpx.Client, harness: str, out: pathlib.Path,
                         records: dict[str, dict[str, Any]]) -> Checks:
    """The other real interruption: we cancel the request; the server completes it anyway."""
    c = Checks("transport-abort")
    run_id = start_run(client, harness, "lost-ack", harness_faults=[
        {"kind": "transport_abort", "tool": "write_file", "path": CHANGELOG,
         "nth": 1, "after_ms": 250}])
    print(f"  run {run_id}", flush=True)
    record = poll(client, harness, run_id)
    records["transport-abort"] = record
    save_case(out, "transport-abort", record, c)

    aborted = [data_of(e) for e in events_of(record, "tool.result")
               if (data_of(e).get("error_class") or {}).get("code") == "ETRANSPORT"]
    c.check("the in-flight write came back as real/transport/ETRANSPORT", bool(aborted),
            aborted[0].get("error_class") if aborted else "")
    if aborted:
        ec = aborted[0]["error_class"]
        c.check("...outcome unknown (the server may well have applied it)",
                aborted[0].get("outcome") == "unknown" and ec.get("outcome_known") is False, ec)
        c.check("...with the taxonomy's exact label", ec.get("label") == LABEL_TRANSPORT,
                ec.get("label"))
        c.check("...and the harness did NOT retry it", aborted[0].get("attempts") == 1,
                aborted[0].get("attempts"))
    c.check("no worker died: a transport abort does not crash the run",
            record.get("worker_generation") == 1 and not events_of(record, "run.resumed"),
            record.get("worker_generation"))
    c.check("the run still reaches a graded end",
            record.get("status") in GRADED
            and (record.get("evaluation") or {}).get("score") is not None,
            f"status={record.get('status')} score={(record.get('evaluation') or {}).get('score')}")
    return c


# ----------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness", default=DEFAULT_HARNESS)
    parser.add_argument("--sandbox-url", default=DEFAULT_SANDBOX_ENV)
    parser.add_argument("--case", action="append", choices=CASES,
                        help="run only these cases (repeatable); default: everything but transport-abort")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    harness = args.harness.rstrip("/")
    sandbox_env = args.sandbox_url.rstrip("/")
    out = pathlib.Path(args.out) if args.out else REPO_ROOT / "runs" / f"{utc_stamp()}_interruptions"
    out.mkdir(parents=True, exist_ok=True)
    selected = args.case or list(DEFAULT_CASES)
    records: dict[str, dict[str, Any]] = {}
    started = time.time()

    results: list[Checks] = []
    with httpx.Client(follow_redirects=True) as client:
        health = client.get(f"{harness}/health", timeout=60.0).json()
        gym = client.get(f"{sandbox_env}/health", timeout=60.0).json()
        print(f"harness    ok={health.get('ok')} model={health.get('model_default')} "
              f"provider_key={health.get('has_provider_key')} "
              f"sandbox_env={health.get('detail', {}).get('sandbox_env_reachable')}")
        print(f"sandbox-env ok={gym.get('ok')} scenarios={gym.get('detail', {}).get('scenarios')}")
        (out / "health.json").write_text(json.dumps({"harness": health, "sandbox_env": gym},
                                                    indent=2, default=str))
        for case in selected:
            print(f"\n=== {case} ===", flush=True)
            try:
                if case == "worker-crash":
                    results.append(case_worker_crash(client, harness, out, records))
                elif case == "lost-ack":
                    results.append(case_lost_ack(client, harness, out, records))
                elif case == "sandbox-loss":
                    results.append(case_sandbox_loss(client, harness, sandbox_env, out, records))
                elif case == "conformance":
                    results.append(case_conformance(client, harness, out, records))
                elif case == "transport-abort":
                    results.append(case_transport_abort(client, harness, out, records))
                else:
                    results.append(case_specimen(client, harness, out))
            except Exception as exc:  # noqa: BLE001 - a broken case is a FAIL, not a traceback
                broken = Checks(case)
                broken.check("case ran to completion", False, f"{type(exc).__name__}: {exc}")
                results.append(broken)

    total = sum(len(r.rows) for r in results)
    passed = sum(r.passed for r in results)
    ok = all(r.ok for r in results) and bool(results)
    (out / "summary.json").write_text(json.dumps(
        {"ok": ok, "checks": total, "passed": passed, "duration_s": round(time.time() - started, 1),
         "harness": harness, "sandbox_env": sandbox_env, "at": utc_stamp(),
         "runs": {case: {"run_id": rec.get("run_id"), "status": rec.get("status"),
                         "score": (rec.get("evaluation") or {}).get("score"),
                         "worker_generation": rec.get("worker_generation")}
                  for case, rec in records.items()},
         "cases": [{"case": r.case, "ok": r.ok, "checks": len(r.rows), "passed": r.passed,
                    "assertions": r.rows} for r in results]}, indent=2, default=str))
    print("\n" + "\n".join(f"  {r.case:<16} {r.passed}/{len(r.rows)} {'ok' if r.ok else 'FAILED'}"
                           for r in results))
    print(f"\n{'PROVE PASS' if ok else 'PROVE FAIL'} :: {passed}/{total} checks, out={out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

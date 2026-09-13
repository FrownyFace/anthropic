#!/usr/bin/env python3
"""Run one Faultline episode against a deployed harness and save the evidence.

    python scripts/run_episode_cli.py --scenario lost-ack
    python scripts/run_episode_cli.py --scenario locked-file --model claude-sonnet-5 --max-steps 12

POSTs /runs, then tails GET /runs/{id}/events over SSE, reconnecting with Last-Event-ID whenever the
server closes the window (the harness closes every stream at ~110 s because Modal kills a web
request at 150 s). Prints a compact live transcript and, when the run finishes, writes

    runs/<UTC timestamp>_<run_id>/{events.jsonl,run.json,evaluate.json,summary.txt}

Needs only httpx: run it with the harness venv
(services/agent-harness/.venv/bin/python scripts/run_episode_cli.py …).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_HARNESS = "https://appliedlabsai-local--faultline-harness-api.modal.run"
#: schemas.RunStatus minus queued/running — `unevaluated` and `interrupted` are terminal too, and a
#: tail that does not know that waits for a run that will never move again.
TERMINAL = {"ok", "error", "truncated", "unevaluated", "interrupted"}
#: statuses that mean the agent got a fair shot and was graded (used for the exit code)
GRADED = {"ok", "truncated"}

FAULT_BADGE = {"missing_file": "MISSING-FILE", "denied_write": "DENIED-WRITE", "ack_lost": "ACK-LOST"}
#: docs/error-taxonomy.md: badge by origin — the agent cannot tell these apart, the operator must.
ORIGIN_BADGE = {"injected": "SIMULATED", "staged": "STAGED", "real": "REAL"}
OUTCOME_BADGE = {"executed": "ran", "failed": "ran/failed", "not_executed": "never ran",
                 "unknown": "OUTCOME UNKNOWN"}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def short(text: Any, n: int = 96) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ----------------------------------------------------------------------------- SSE


def sse_frames(response: httpx.Response) -> Iterator[dict[str, Any]]:
    """Yield {id?, event, data} frames from a text/event-stream response."""
    event_type = "message"
    event_id: int | None = None
    data_lines: list[str] = []
    for line in response.iter_lines():
        line = line.rstrip("\r")
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"raw": raw}
                yield {"id": event_id, "event": event_type, "data": payload}
            event_type, event_id, data_lines = "message", None, []
            continue
        if line.startswith(":"):  # keepalive comment
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            try:
                event_id = int(value)
            except ValueError:
                event_id = None


# ----------------------------------------------------------------------------- transcript


def render(event: dict[str, Any]) -> str | None:
    """One compact line per interesting event (None = don't print)."""
    etype = event.get("type", "")
    data = event.get("data") or {}
    step = event.get("step")
    prefix = f"  {step:>2} " if isinstance(step, int) else "     "

    if etype == "run.started":
        return (f"run   scenario={data.get('scenario_id')} model={data.get('model')} "
                f"max_steps={data.get('max_steps')} workspace={data.get('anthropic_workspace')}")
    if etype == "episode.reset":
        return (f"reset episode={data.get('episode_id')} files={len(data.get('files') or [])}"
                f" sandbox={data.get('sandbox_id')} attempt={data.get('attempt')}")
    if etype == "run.resumed":
        return (f"RESUMED worker={data.get('worker_generation')} "
                f"from_event={data.get('resumed_from_event_id')} "
                f"dangling={data.get('dangling_tool_use_id')}")
    if etype == "interruption":
        planned = "planned" if data.get("planned") else "unplanned"
        return (f"{prefix}!! INTERRUPTION [{data.get('layer')}/{data.get('code')}] {planned} "
                f"resumed={data.get('resumed')} outcome_known={data.get('outcome_known')} "
                f"— {short(data.get('label'), 70)}")
    if etype == "episode.sandbox":
        return (f"{prefix}   sandbox {data.get('sandbox_id')} -> {data.get('status')}"
                f"  {short(data.get('reason'), 70)}")
    if etype == "turn.text":
        return f"{prefix}· {short(data.get('text'), 110)}"
    if etype == "tool.call":
        args = data.get("input") or {}
        label = args.get("command") or args.get("path") or args.get("summary") or ""
        return f"{prefix}→ {data.get('tool'):<12} {short(label, 84)}"
    if etype == "tool.result":
        out = data.get("output") or ""
        exit_code: Any = None
        try:
            payload = json.loads(out)
            exit_code = payload.get("exit_code", payload.get("code"))
        except (json.JSONDecodeError, AttributeError, TypeError):
            payload = {}
        bits = []
        if exit_code is not None:
            bits.append(f"exit={exit_code}")
        bits.append(f"{data.get('duration_ms', 0)}ms")
        if data.get("attempts", 1) > 1:
            bits.append(f"attempts={data['attempts']}")
        if data.get("is_error"):
            bits.append("ERROR")
        outcome = data.get("outcome")
        if outcome and outcome != "executed":
            bits.append(OUTCOME_BADGE.get(outcome, outcome))
        ec = data.get("error_class") or {}
        if ec:
            # The agent saw only the OS-style code; this line is what the operator is owed.
            bits.append(f"[{ORIGIN_BADGE.get(ec.get('origin'), ec.get('origin'))} "
                        f"{ec.get('layer')}/{ec.get('code')}] {short(ec.get('label'), 60)}")
        fault = data.get("fault") or {}
        if fault and not ec:
            badge = FAULT_BADGE.get(fault.get("kind"), str(fault.get("kind")))
            bits.append(f"[{badge}{'?' if fault.get('inferred') else ''}]")
        sandbox = data.get("sandbox") or {}
        if sandbox.get("alive") is False:
            bits.append("sandbox=DEAD")
        return f"{prefix}← {' '.join(bits)}  {short(out, 60)}"
    if etype == "fault.fired":
        badge = FAULT_BADGE.get(data.get("kind"), str(data.get("kind")))
        origin = ORIGIN_BADGE.get(data.get("origin"), data.get("origin") or "?")
        return (f"{prefix}!! {badge} on {data.get('path')} ({data.get('mode')}, {origin}"
                f"/{data.get('layer')}) {short(data.get('description'), 60)}")
    if etype == "workspace.diff":
        changed = [f for f in (data.get("files") or []) if f.get("status") != "unchanged"]
        return f"{prefix}   diff: {len(changed)} changed " + short(
            ", ".join(f"{f.get('path')}({f.get('status')})" for f in changed), 70
        )
    if etype == "episode.evaluated":
        checks = data.get("checks") or []
        ok = sum(1 for c in checks if c.get("ok"))
        tests = data.get("tests") or {}
        return (f"grade score={data.get('score')} passed={data.get('passed')} "
                f"checks={ok}/{len(checks)} tests={tests.get('passed')}p/{tests.get('failed')}f")
    if etype == "run.finished":
        usage = data.get("usage") or {}
        ec = data.get("error_class") or {}
        return (f"done  status={data.get('status')} evaluation={data.get('evaluation_status')} "
                f"steps={data.get('steps')} workers={data.get('worker_generation')} "
                f"{data.get('duration_ms')}ms in={usage.get('input_tokens')} "
                f"out={usage.get('output_tokens')}"
                + (f"\n      why: [{ORIGIN_BADGE.get(ec.get('origin'), '?')} {ec.get('layer')}/"
                   f"{ec.get('code')}] {ec.get('label')}" if ec else "")
                + (f"\n      evaluation_error={short(data.get('evaluation_error'), 90)}"
                   if data.get("evaluation_error") else "")
                + (f"\n      error={short(data.get('error'), 90)}" if data.get("error") else ""))
    if etype == "log" and data.get("ev") == "ledger.resolution":
        rows = data.get("resolutions") or []
        applied = [r for r in rows if r.get("side_effect_applied")]
        return (f"      ledger resolved {len(rows)} call(s); side effect APPLIED for "
                f"{len(applied)}: " + short(", ".join(
                    f"{r.get('tool_use_id') or r.get('ledger_step')}={r.get('side_effect_applied')}"
                    for r in rows), 80))
    if etype == "log" and data.get("lvl") in {"warn", "error"}:
        return f"{prefix}   [{data.get('lvl')}] {data.get('ev')}: {short(data.get('msg'), 90)}"
    return None


# ----------------------------------------------------------------------------- main


def identity_headers(user: str | None) -> dict[str, str]:
    """PLAN.md §2.9.1: the browser's anonymous id. Sent on every request so the run lands in that
    user's conversation list; omitted entirely when --user is not given (anonymous run)."""
    return {"X-Faultline-User": user} if user else {}


def tail(client: httpx.Client, harness: str, run_id: str, events: list[dict[str, Any]],
         deadline: float, quiet: bool, windows: list[dict[str, Any]] | None = None,
         user: str | None = None) -> str | None:
    """Stream until the run is finished or the deadline passes; returns the final status.

    `windows` collects one entry per 110 s segment close (`event: done` with reason=window), which is
    the reconnect path apps/web has to implement; it is saved into the evidence summary.
    """
    last_id = -1
    status: str | None = None
    windows = [] if windows is None else windows
    t_open = time.time()
    while time.time() < deadline:
        headers = {"Accept": "text/event-stream", **identity_headers(user)}
        if last_id >= 0:
            headers["Last-Event-ID"] = str(last_id)
        try:
            with client.stream("GET", f"{harness}/runs/{run_id}/events", headers=headers,
                               timeout=httpx.Timeout(connect=15.0, read=130.0, write=15.0, pool=15.0)) as resp:
                resp.raise_for_status()
                for frame in sse_frames(resp):
                    if frame["event"] == "done":
                        reason = (frame["data"] or {}).get("reason")
                        status = (frame["data"] or {}).get("status")
                        if reason == "finished":
                            return status
                        # reason == "window": the 110 s segment elapsed on a still-running run.
                        # Record it — this is the reconnect path apps/web must implement too.
                        print(f"     ~ sse window closed at id={last_id} (status={status}); reconnecting",
                              file=sys.stderr, flush=True)
                        windows.append({"at_event_id": last_id, "status": status, "t": round(time.time() - t_open, 1)})
                        break  # window closed: reconnect from last_id
                    event = frame["data"]
                    if not isinstance(event, dict) or "id" not in event:
                        continue
                    if event["id"] <= last_id:
                        continue
                    last_id = event["id"]
                    events.append(event)
                    line = render(event)
                    if line and not quiet:
                        print(line, flush=True)
                    if event.get("type") == "run.finished":
                        status = (event.get("data") or {}).get("status")
        except (httpx.HTTPError, httpx.StreamError) as exc:
            print(f"… stream interrupted ({type(exc).__name__}: {exc}); reconnecting", file=sys.stderr)
            time.sleep(1.0)
            continue
        if status in TERMINAL:
            return status
        # Fall back to the record in case we missed the terminal event on a dropped stream.
        record = client.get(f"{harness}/runs/{run_id}", timeout=30.0,
                            headers=identity_headers(user)).json()
        if record.get("status") in TERMINAL:
            return record["status"]
        time.sleep(0.5)
    return status


def write_evidence(out_dir: pathlib.Path, run_id: str, record: dict[str, Any],
                   events: list[dict[str, Any]], windows: list[dict[str, Any]] | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stream = record.get("events") or events  # the record is authoritative when we have it

    with (out_dir / "events.jsonl").open("w") as fh:
        for event in stream:
            fh.write(json.dumps(event, default=str) + "\n")
    (out_dir / "run.json").write_text(json.dumps(record, indent=2, default=str))
    evaluation = record.get("evaluation") or {}
    (out_dir / "evaluate.json").write_text(json.dumps(evaluation, indent=2, default=str))

    finished = next((e for e in stream if e.get("type") == "run.finished"), {}) or {}
    fdata = finished.get("data") or {}
    usage = fdata.get("usage") or record.get("usage") or {}
    error_class = record.get("error_class") or fdata.get("error_class") or {}
    lines = [
        f"run_id      {run_id}",
        f"scenario    {record.get('scenario_id')}",
        f"model       {record.get('model')}",
        f"status      {record.get('status')}",
        f"evaluation  {fdata.get('evaluation_status')}"
        + (f"  ({fdata.get('evaluation_error')})" if fdata.get("evaluation_error") else ""),
        f"error_class {(error_class.get('origin') or '-')}/{error_class.get('layer') or '-'}/"
        f"{error_class.get('code') or '-'}  {error_class.get('label') or ''}",
        f"workers     {record.get('worker_generation')}",
        f"episode     {record.get('episode_id')}",
        f"steps       {record.get('steps')}",
        f"events      {len(stream)}",
        f"tokens      in={usage.get('input_tokens')} out={usage.get('output_tokens')}",
        f"duration_ms {fdata.get('duration_ms')}",
        f"sse_windows {json.dumps(windows or [])}",
        f"error       {record.get('error')}",
        "",
        f"score       {evaluation.get('score')}  passed={evaluation.get('passed')}",
    ]
    for check in evaluation.get("checks") or []:
        lines.append(f"  [{'x' if check.get('ok') else ' '}] {check.get('id'):<28} "
                     f"w={check.get('weight')} {short(check.get('detail'), 70)}")
    tests = evaluation.get("tests") or {}
    lines += ["", f"tests       passed={tests.get('passed')} failed={tests.get('failed')} "
                  f"errors={tests.get('errors')}", short(tests.get("output"), 400), ""]
    faults = [e for e in stream if e.get("type") == "fault.fired"]
    lines.append(f"faults fired ({len(faults)}):")
    for event in faults:
        data = event.get("data") or {}
        lines.append(f"  step {event.get('step')}: {data.get('kind')} on {data.get('path')} "
                     f"({data.get('mode')}, {data.get('origin')}/{data.get('layer')})")
    interruptions = [e for e in stream if e.get("type") == "interruption"]
    lines.append("")
    lines.append(f"real interruptions ({len(interruptions)}):")
    for event in interruptions:
        data = event.get("data") or {}
        lines.append(f"  step {event.get('step')}: {data.get('layer')}/{data.get('code')} "
                     f"planned={data.get('planned')} resumed={data.get('resumed')} "
                     f"outcome_known={data.get('outcome_known')} tool={data.get('tool')} "
                     f"path={data.get('path')}")
    resumes = [e for e in stream if e.get("type") == "run.resumed"]
    for event in resumes:
        data = event.get("data") or {}
        lines.append(f"  resumed as worker {data.get('worker_generation')} from event "
                     f"{data.get('resumed_from_event_id')} (dangling "
                     f"{data.get('dangling_tool_use_id')})")
    outcomes: dict[str, int] = {}
    for event in stream:
        if event.get("type") == "tool.result":
            key = str((event.get("data") or {}).get("outcome") or "?")
            outcomes[key] = outcomes.get(key, 0) + 1
    lines += ["", f"tool outcomes {json.dumps(outcomes)}"]
    resolution = next((e for e in stream if e.get("type") == "log"
                       and (e.get("data") or {}).get("ev") == "ledger.resolution"), None)
    if resolution:
        rows = (resolution.get("data") or {}).get("resolutions") or []
        lines.append(f"ledger resolution ({len(rows)}):")
        for row in rows:
            lines.append(f"  {row.get('tool_use_id') or ('ledger step ' + str(row.get('ledger_step')))}"
                         f"  {row.get('tool')} {row.get('path')} outcome={row.get('outcome')} "
                         f"origin={row.get('origin')} interrupted={row.get('interrupted')} "
                         f"side_effect_applied={row.get('side_effect_applied')}")
    if record.get("summary"):
        lines += ["", "agent summary:", short(record["summary"], 1200)]
    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one Faultline episode and save the evidence.")
    parser.add_argument("--harness", default=DEFAULT_HARNESS, help="harness base URL")
    parser.add_argument("--scenario", default="lost-ack", help="scenario id (see GET /scenarios)")
    parser.add_argument("--model", default=None, help="claude-haiku-4-5 | claude-sonnet-5 | claude-opus-5")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--user", default=None,
                        help="X-Faultline-User id (u_<uuid4>) to scope the run to a browser identity; "
                             "'new' mints one. Without it the run is anonymous.")
    parser.add_argument("--out", default=None, help="evidence directory (default runs/<ts>_<run_id>)")
    parser.add_argument("--timeout", type=float, default=900.0, help="give up after N seconds")
    parser.add_argument("--quiet", action="store_true", help="save evidence without the live transcript")
    args = parser.parse_args()

    harness = args.harness.rstrip("/")
    user = args.user
    if user == "new":
        user = f"u_{uuid.uuid4()}"
    if user and not user.startswith("u_"):
        print(f"--user must look like u_<uuid4>, got {user!r}", file=sys.stderr)
        return 2
    headers = identity_headers(user)
    body: dict[str, Any] = {"scenario_id": args.scenario}
    if args.model:
        body["model"] = args.model
    if args.max_steps:
        body["max_steps"] = args.max_steps
    if args.seed is not None:
        body["seed"] = args.seed

    with httpx.Client(follow_redirects=True) as client:
        health = client.get(f"{harness}/health", timeout=30.0).json()
        print(f"harness {harness} ok={health.get('ok')} model={health.get('model_default')} "
              f"provider_key={health.get('has_provider_key')} "
              f"sandbox_env={health.get('detail', {}).get('sandbox_env_reachable')}")
        if health.get("has_provider_key"):
            print("WARNING: the API function reports a provider key in its env (boundary leak)",
                  file=sys.stderr)

        resp = client.post(f"{harness}/runs", json=body, timeout=60.0, headers=headers)
        if resp.status_code >= 400:
            print(f"POST /runs failed: {resp.status_code} {resp.text[:500]}", file=sys.stderr)
            return 2
        created = resp.json()
        run_id = created["run_id"]
        print(f"run_id {run_id}  scenario={created.get('scenario_id')} model={created.get('model')}"
              + (f" conversation={created.get('conversation_id')}" if created.get("conversation_id") else "")
              + (f" user={user}" if user else ""))

        events: list[dict[str, Any]] = []
        windows: list[dict[str, Any]] = []
        t0 = time.time()
        status = tail(client, harness, run_id, events, t0 + args.timeout, args.quiet, windows, user)
        record = client.get(f"{harness}/runs/{run_id}", timeout=60.0, headers=headers).json()
        conversations: list[dict[str, Any]] = []
        if user:
            # PLAN.md §3.2 "done when": the run must show up in this browser's conversation list.
            got = client.get(f"{harness}/conversations", timeout=30.0, headers=headers)
            if got.status_code == 200:
                payload = got.json()
                conversations = payload if isinstance(payload, list) else payload.get("conversations", [])
            else:
                print(f"GET /conversations -> {got.status_code} {got.text[:200]}", file=sys.stderr)

    out_dir = pathlib.Path(args.out) if args.out else REPO_ROOT / "runs" / f"{utc_stamp()}_{run_id}"
    write_evidence(out_dir, run_id, record, events, windows)
    if user:
        (out_dir / "conversations.json").write_text(
            json.dumps({"user_id": user, "conversation_id": record.get("conversation_id"),
                        "conversations": conversations}, indent=2, default=str)
        )
        listed = any(c.get("id") == record.get("conversation_id") for c in conversations)
        print(f"user={user} conversation={record.get('conversation_id')} "
              f"listed_in_GET_/conversations={listed} ({len(conversations)} total)")
    final = record.get("status") or status
    finished = next((e for e in (record.get("events") or events)
                     if e.get("type") == "run.finished"), {}) or {}
    fdata = finished.get("data") or {}
    error_class = record.get("error_class") or fdata.get("error_class") or {}
    print(f"\nstatus={final} evaluation={fdata.get('evaluation_status')} "
          f"score={(record.get('evaluation') or {}).get('score')} "
          f"workers={record.get('worker_generation')} "
          f"interruptions={len(record.get('interruptions') or [])} evidence={out_dir}")
    if error_class:
        print(f"why    [{ORIGIN_BADGE.get(error_class.get('origin'), '?')} "
              f"{error_class.get('layer')}/{error_class.get('code')}] {error_class.get('label')}")
    # `unevaluated`/`interrupted` are NOT failures of the agent, but they are not a graded run
    # either: the caller needs a non-zero exit so a proof script cannot mistake one for a pass.
    return 0 if final in GRADED else 1


if __name__ == "__main__":
    raise SystemExit(main())

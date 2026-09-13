#!/usr/bin/env python3
"""Verify, against the DEPLOYED harness, every wire fact apps/web relies on.

    services/agent-harness/.venv/bin/python services/agent-harness/tools/check_contract.py \
        <finished run_id> <out.json> [harness base url]

Reads an already-finished run (no model call, no cost), validates its JSON against
`faultline_common.schemas`, and exercises the SSE surface four ways (plain, ?after=,
?last_event_id=, Last-Event-ID header). Writes a JSON report; exits non-zero if any check fails.

The result of the last run of this file is documented in docs/harness-contract.md.
"""
from __future__ import annotations

import json
import pathlib
import sys

import httpx

from faultline_common.schemas import EvaluateResponse, Event, RunRecord

# Every status a run can END on, straight from the shared contract — so adding one to schemas.py
# (PLAN.md §2.11 added `unevaluated` and `interrupted`) cannot leave this checker asserting a
# stale set and reporting a correct run as a contract break.
TERMINAL_STATUSES = {"ok", "truncated", "unevaluated", "interrupted", "error"}
#: Statuses that mean "never graded, and that is the right answer" (docs/error-taxonomy.md).
UNGRADED_STATUSES = {"unevaluated", "interrupted", "error"}

RUN_ID = sys.argv[1]
OUT = pathlib.Path(sys.argv[2])
HARNESS = (sys.argv[3] if len(sys.argv) > 3
           else "https://appliedlabsai-local--faultline-harness-api.modal.run").rstrip("/")

checks: list[dict] = []


def check(name: str, ok: bool, detail) -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:170]}")


def frames(text: str) -> list[dict]:
    out, ev, eid, data = [], "message", None, []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line == "":
            if data:
                raw = "\n".join(data)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"raw": raw}
                out.append({"id": eid, "event": ev, "data": payload})
            ev, eid, data = "message", None, []
            continue
        if line.startswith(":"):
            continue
        f, _, v = line.partition(":")
        v = v[1:] if v.startswith(" ") else v
        if f == "event":
            ev = v
        elif f == "data":
            data.append(v)
        elif f == "id":
            eid = v
    return out


with httpx.Client(follow_redirects=True, timeout=60.0) as c:
    # ---------------------------------------------------------------- REST shapes
    sc = c.get(f"{HARNESS}/scenarios").json()
    check("GET /scenarios -> {scenarios:[...]}", isinstance(sc, dict) and isinstance(sc.get("scenarios"), list),
          sorted(s["id"] for s in sc.get("scenarios", [])))

    rl = c.get(f"{HARNESS}/runs").json()
    check("GET /runs -> {runs:[...]}", isinstance(rl, dict) and isinstance(rl.get("runs"), list),
          f"{len(rl.get('runs', []))} runs; keys={sorted(rl['runs'][0].keys()) if rl.get('runs') else None}")

    rec = c.get(f"{HARNESS}/runs/{RUN_ID}").json()
    check("GET /runs/{id} -> whole RunRecord", rec.get("run_id") == RUN_ID and "events" in rec,
          {k: rec.get(k) for k in ("status", "scenario_id", "model", "steps", "episode_id")})

    events = rec["events"]
    ids = [e["id"] for e in events]
    check("Event.id 0-based and contiguous", ids == list(range(len(events))), f"0..{len(events)-1}")

    # ---------------------------------------------------------------- pydantic contract
    try:
        RunRecord.model_validate(rec)
        check("RunRecord validates against faultline_common.schemas", True, "ok")
    except Exception as exc:  # noqa: BLE001
        check("RunRecord validates against faultline_common.schemas", False, str(exc)[:300])

    bad = []
    for e in events:
        try:
            Event.model_validate(e)
        except Exception as exc:  # noqa: BLE001
            bad.append((e.get("id"), str(exc)[:120]))
    check("every Event validates against faultline_common.schemas", not bad,
          f"{len(events)} events, {len(bad)} invalid" + (f" {bad[:3]}" if bad else ""))

    # A run that ended `unevaluated`/`interrupted` was never graded — that is the taxonomy working
    # (`ok` implies a score; docs/error-taxonomy.md), not a missing field. Such a run must instead
    # carry the error_class that says why, and `interrupted` must carry an Interruption.
    if rec["evaluation"] is None and rec["status"] in UNGRADED_STATUSES:
        klass = rec.get("error_class") or {}
        check("an ungraded run explains itself with an error_class instead of an evaluation",
              bool(klass.get("origin") and klass.get("layer") and klass.get("code") and klass.get("label"))
              and (rec["status"] != "interrupted" or bool(rec.get("interruptions"))),
              {"status": rec["status"], "error_class": klass,
               "interruptions": len(rec.get("interruptions") or [])})
    else:
        try:
            EvaluateResponse.model_validate(rec["evaluation"])
            check("EvaluateResponse validates against faultline_common.schemas", True,
                  {"score": rec["evaluation"]["score"], "passed": rec["evaluation"]["passed"],
                   "checks": len(rec["evaluation"]["checks"]), "ledger": len(rec["evaluation"]["ledger"])})
        except Exception as exc:  # noqa: BLE001
            check("EvaluateResponse validates against faultline_common.schemas", False, str(exc)[:300])

    # ---------------------------------------------------------------- event payload shapes
    tr = [e for e in events if e["type"] == "tool.result"]
    structured = [e for e in tr if e["data"].get("tool") in {"read_file", "write_file", "list_dir", "run_command"}]
    json_strings = []
    for e in structured:
        out = e["data"].get("output")
        ok = isinstance(out, str)
        if ok:
            try:
                json.loads(out)
                json_strings.append(True)
            except json.JSONDecodeError:
                json_strings.append(False)
        else:
            json_strings.append(False)
    check("tool.result.data.output is a JSON string for structured tools",
          bool(structured) and all(json_strings),
          f"{sum(json_strings)}/{len(structured)} parse as JSON")

    ff = [e for e in events if e["type"] == "fault.fired"]
    if not ff and rec["status"] in UNGRADED_STATUSES:
        # A run the sandbox loss cut short may never reach its injected fault: the event is absent
        # because the scenario never got that far, not because the shape is wrong.
        check("no fault.fired on a run that was cut short before its fault could fire", True,
              {"status": rec["status"], "steps": rec.get("steps")})
    else:
        check("fault.fired data is the FaultFired object at top level",
              bool(ff) and all({"step", "kind", "path", "mode"} <= set(e["data"]) for e in ff),
              [e["data"] for e in ff])

    llm = [e for e in events if e["type"] == "llm.call"]
    check("llm.call carries attempt/model/stop_reason/request_id/usage/duration_ms",
          bool(llm) and all({"attempt", "model", "stop_reason", "request_id", "usage", "duration_ms"}
                            <= set(e["data"]) for e in llm),
          f"{len(llm)} calls; first={llm[0]['data'] if llm else None}")

    # A trap for the UI: Event.step is the harness loop turn, data.step (fault.fired) is the
    # sandbox-env ledger index. They are different counters and must not be conflated.
    check("fault.fired: Event.step (loop turn) != data.step (ledger index) is expected",
          all(isinstance(e["data"].get("step"), int) and isinstance(e.get("step"), int) for e in ff),
          [{"event.step": e["step"], "data.step": e["data"]["step"], "kind": e["data"]["kind"]} for e in ff])

    ev_types = sorted({e["type"] for e in events})
    check("event types emitted", True, ev_types)

    # ---------------------------------------------------------------- SSE
    r = c.get(f"{HARNESS}/runs/{RUN_ID}/events")
    fr = frames(r.text)
    check("SSE starts with retry:", r.text.startswith("retry: 1000"), r.text[:20].replace("\n", "\\n"))
    data_frames = [f for f in fr if f["event"] != "done"]
    check("SSE frame id == Event.id",
          [f["id"] for f in data_frames] == [str(e["id"]) for e in events],
          f"{len(data_frames)} frames, first id={data_frames[0]['id']}, last id={data_frames[-1]['id']}")
    check("SSE names the frame with event: <Event.type>",
          all(f["event"] == f["data"]["type"] for f in data_frames),
          sorted({f["event"] for f in data_frames}))
    done = [f for f in fr if f["event"] == "done"]
    check("terminal `event: done` with reason=finished",
          len(done) == 1 and done[0]["data"].get("reason") == "finished"
          and done[0]["data"].get("status") in TERMINAL_STATUSES,
          done[0]["data"] if done else None)

    n = len(events)
    r2 = frames(c.get(f"{HARNESS}/runs/{RUN_ID}/events?after={n-3}").text)
    check("?after=<n> resumes", [f["id"] for f in r2 if f["event"] != "done"] == [str(n - 2), str(n - 1)],
          [f["id"] for f in r2 if f["event"] != "done"])

    r3 = frames(c.get(f"{HARNESS}/runs/{RUN_ID}/events?last_event_id={n-3}").text)
    check("?last_event_id=<n> resumes", [f["id"] for f in r3 if f["event"] != "done"] == [str(n - 2), str(n - 1)],
          [f["id"] for f in r3 if f["event"] != "done"])

    r4 = frames(c.get(f"{HARNESS}/runs/{RUN_ID}/events", headers={"Last-Event-ID": str(n - 3)}).text)
    check("Last-Event-ID header resumes", [f["id"] for f in r4 if f["event"] != "done"] == [str(n - 2), str(n - 1)],
          [f["id"] for f in r4 if f["event"] != "done"])

    r5 = c.get(f"{HARNESS}/runs/does-not-exist/events")
    check("SSE 404s an unknown run", r5.status_code == 404, r5.status_code)

    cors = c.request("OPTIONS", f"{HARNESS}/runs",
                     headers={"Origin": "https://example.org",
                              "Access-Control-Request-Method": "POST",
                              "Access-Control-Request-Headers": "content-type,x-faultline-user"})
    check("CORS preflight allows POST /runs from any origin",
          cors.status_code < 400 and cors.headers.get("access-control-allow-origin") == "*",
          {"status": cors.status_code, "allow-origin": cors.headers.get("access-control-allow-origin"),
           "allow-headers": cors.headers.get("access-control-allow-headers")})

    bad = c.post(f"{HARNESS}/runs", json={"scenario_id": "nope"})
    check("POST /runs 400s an unknown scenario", bad.status_code == 400, bad.json())

    badm = c.post(f"{HARNESS}/runs", json={"scenario_id": "lost-ack", "model": "gpt-4"})
    check("POST /runs 400s a model off the allowlist", badm.status_code == 400, badm.json())

failed = [c_ for c_ in checks if not c_["ok"]]
OUT.write_text(json.dumps({"harness": HARNESS, "run_id": RUN_ID, "checks": checks,
                           "passed": len(checks) - len(failed), "failed": len(failed)}, indent=2))
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed -> {OUT}")
sys.exit(1 if failed else 0)

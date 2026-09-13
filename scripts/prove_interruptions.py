#!/usr/bin/env python3
"""Live proof of failure provenance and real interruptions (PLAN.md §2.11).

Four cases, all against the DEPLOYED services, each one asserting the things the ledger, the events,
the run status and the UI are supposed to be able to say about a failure:

  worker-crash   the harness worker really dies (os._exit(137)) with a write in flight; Modal
                 re-invokes run_episode; a fresh worker resumes from the persisted events, reports
                 the interruption to the gym and hands the agent an EHARNESS result whose outcome is
                 UNKNOWN. Ends `ok` with a score, worker_generation 2, and the ledger resolving
                 side_effect_applied: true (the write had landed).
  transport-abort the in-flight HTTP request is cancelled client-side while sandbox-env completes
                 it: real/transport/ETRANSPORT, outcome UNKNOWN, never retried, no worker dies.
                 Asked for in the run request, since no bundled scenario declares this kind.
  sandbox-loss   the Modal Sandbox of OUR OWN episode is terminated mid-run (what a concurrent
                 `reap` did to run r_ccda8780cbee). Must end `interrupted` /
                 real/sandbox/ESANDBOX, grading skipped, and the model must NOT be given the
                 remaining steps to flail in.
  lost-ack       regression: an INJECTED fault must still be labelled `simulated: …`, outcome
                 unknown, and resolved by the ledger afterwards. Ends `ok` with a score.
  specimen       GET /runs/r_ccda8780cbee — old data is untouched, but the record must still answer
                 the taxonomy's questions (error_class / interruptions / worker_generation present).

    services/agent-harness/.venv/bin/python scripts/prove_interruptions.py
    services/agent-harness/.venv/bin/python scripts/prove_interruptions.py --case sandbox-loss

Evidence: runs/<UTC ts>_interruptions/{summary.json,<case>.json,<case>_events.jsonl}.
Exit code 0 iff every check of every selected case passed.

`sandbox-loss` terminates exactly one Modal Sandbox: the one belonging to the episode this script
just created. It never enumerates or touches anything else (that is what `reap` does, and `reap`
kills other people's live episodes).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_HARNESS = "https://appliedlabsai-local--faultline-harness-api.modal.run"
DEFAULT_SANDBOX_ENV = "https://appliedlabsai-local--faultline-sandbox-env-api.modal.run"
SPECIMEN_RUN = "r_ccda8780cbee"
TERMINAL = {"ok", "error", "truncated", "unevaluated", "interrupted"}
CASES = ("worker-crash", "transport-abort", "sandbox-loss", "lost-ack", "specimen")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Checks:
    """A named list of assertions; nothing throws, everything is recorded."""

    def __init__(self, case: str):
        self.case = case
        self.rows: list[dict[str, Any]] = []

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        self.rows.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {str(detail)[:120]}" if detail else ""))
        return bool(ok)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.rows if r["ok"])

    @property
    def ok(self) -> bool:
        return all(r["ok"] for r in self.rows) and bool(self.rows)


# ----------------------------------------------------------------------------- run driving


def start_run(client: httpx.Client, harness: str, scenario: str, **body: Any) -> str:
    resp = client.post(f"{harness}/runs", json={"scenario_id": scenario, **body}, timeout=60.0)
    resp.raise_for_status()
    return resp.json()["run_id"]


def poll(client: httpx.Client, harness: str, run_id: str, *, timeout: float = 420.0,
         on_event: Callable[[dict[str, Any], dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Poll GET /runs/{id} until the status is terminal, calling `on_event` for each new event."""
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
        time.sleep(1.5)
    return record


def events_of(record: dict[str, Any], type_: str) -> list[dict[str, Any]]:
    return [e for e in record.get("events") or [] if e.get("type") == type_]


def finished_data(record: dict[str, Any]) -> dict[str, Any]:
    got = events_of(record, "run.finished")
    return (got[-1].get("data") or {}) if got else {}


def ledger_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    for event in record.get("events") or []:
        if event.get("type") == "log" and (event.get("data") or {}).get("ev") == "ledger.resolution":
            return list((event["data"] or {}).get("resolutions") or [])
    return []


def save(out: pathlib.Path, case: str, record: dict[str, Any], checks: Checks) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{case}.json").write_text(json.dumps(
        {"case": case, "run_id": record.get("run_id"), "status": record.get("status"),
         "error_class": record.get("error_class"), "interruptions": record.get("interruptions"),
         "worker_generation": record.get("worker_generation"),
         "score": (record.get("evaluation") or {}).get("score"),
         "run_finished": finished_data(record),
         "ledger_resolution": ledger_rows(record),
         "checks": checks.rows}, indent=2, default=str))
    with (out / f"{case}_events.jsonl").open("w") as fh:
        for event in record.get("events") or []:
            fh.write(json.dumps(event, default=str) + "\n")


# ----------------------------------------------------------------------------- cases


def case_worker_crash(client: httpx.Client, harness: str, out: pathlib.Path) -> Checks:
    c = Checks("worker-crash")
    run_id = start_run(client, harness, "worker-crash")
    print(f"  run {run_id}")
    record = poll(client, harness, run_id)
    save(out, "worker-crash", record, c)

    resumed = events_of(record, "run.resumed")
    intrs = events_of(record, "interruption")
    c.check("the worker really died and a new one resumed", len(resumed) == 1,
            (resumed[0]["data"] if resumed else ""))
    c.check("worker_generation is 2", record.get("worker_generation") == 2,
            record.get("worker_generation"))
    c.check("one REAL interruption, planned, at the harness layer",
            len(intrs) == 1 and intrs[0]["data"]["layer"] == "harness"
            and intrs[0]["data"]["code"] == "EHARNESS" and intrs[0]["data"]["planned"] is True
            and intrs[0]["data"]["resumed"] is True,
            intrs[0]["data"] if intrs else "")
    c.check("the interruption is on the run record too",
            len(record.get("interruptions") or []) == 1,
            record.get("interruptions"))

    dangling = (resumed[0]["data"].get("dangling_tool_use_id") if resumed else None)
    synthetic = [e for e in events_of(record, "tool.result")
                 if (e["data"] or {}).get("tool_use_id") == dangling]
    data = synthetic[0]["data"] if synthetic else {}
    c.check("the agent was handed EHARNESS with an UNKNOWN outcome",
            data.get("error_code") == "EHARNESS" and data.get("outcome") == "unknown"
            and (data.get("error_class") or {}).get("outcome_known") is False,
            {k: data.get(k) for k in ("error_code", "outcome")})
    c.check("...and told to verify before retrying",
            "verify before retrying" in str(data.get("output")), "")
    c.check("the gym was told (ledger row marked interrupted)",
            data.get("reported_to_gym") is True, data.get("reported_to_gym"))
    c.check("the label is the taxonomy's, verbatim",
            (data.get("error_class") or {}).get("label")
            == "real: harness worker interrupted mid-call (outcome unknown)",
            (data.get("error_class") or {}).get("label"))

    fin = finished_data(record)
    c.check("a resumed run can still end ok WITH a score",
            record.get("status") == "ok" and fin.get("evaluation_status") == "ok"
            and isinstance((record.get("evaluation") or {}).get("score"), (int, float)),
            f"status={record.get('status')} score={(record.get('evaluation') or {}).get('score')}")
    rows = ledger_rows(record)
    resolved = [r for r in rows if r.get("tool_use_id") == dangling]
    c.check("the ledger resolved the in-flight write as APPLIED",
            bool(resolved) and resolved[0].get("side_effect_applied") is True, resolved)
    checks = {ck["id"]: ck["ok"] for ck in (record.get("evaluation") or {}).get("checks") or []}
    c.check("verified_before_rewrite was graded on the interrupted row",
            "verified_before_rewrite" in checks, checks)
    return c


def case_transport_abort(client: httpx.Client, harness: str, out: pathlib.Path) -> Checks:
    """The other harness fault: we cancel the in-flight request; the server completes it anyway.

    No bundled scenario declares `transport_abort`, so the trigger is asked for in the run request
    (POST /runs `harness_faults`, validated against schemas.HarnessFault).
    """
    c = Checks("transport-abort")
    run_id = start_run(client, harness, "lost-ack", harness_faults=[
        {"kind": "transport_abort", "tool": "write_file", "path": "CHANGELOG.md",
         "nth": 1, "after_ms": 250}])
    print(f"  run {run_id}")
    record = poll(client, harness, run_id)
    save(out, "transport-abort", record, c)

    aborted = [e["data"] for e in events_of(record, "tool.result")
               if (e["data"].get("error_class") or {}).get("code") == "ETRANSPORT"]
    c.check("the in-flight write came back as real/transport/ETRANSPORT", bool(aborted),
            aborted[0].get("error_class") if aborted else "")
    if aborted:
        ec = aborted[0]["error_class"]
        c.check("...outcome UNKNOWN (the server may well have applied it)",
                aborted[0].get("outcome") == "unknown" and ec.get("outcome_known") is False, ec)
        c.check("...with the taxonomy's exact label",
                ec.get("label") == "real: transport failure harness<->sandbox-env (outcome unknown)",
                ec.get("label"))
        c.check("...and it was NOT retried by the harness", aborted[0].get("attempts") == 1,
                aborted[0].get("attempts"))
    c.check("no worker died: a transport abort does not crash the run",
            record.get("worker_generation") == 1 and not events_of(record, "run.resumed"),
            record.get("worker_generation"))
    c.check("the run still reaches a graded end",
            record.get("status") in ("ok", "truncated")
            and (record.get("evaluation") or {}).get("score") is not None,
            f"status={record.get('status')} score={(record.get('evaluation') or {}).get('score')}")
    return c


def case_sandbox_loss(client: httpx.Client, harness: str, sandbox_env: str,
                      out: pathlib.Path) -> Checks:
    """Terminate OUR OWN episode's Modal Sandbox mid-run: the r_ccda8780cbee condition, on purpose."""
    import modal

    c = Checks("sandbox-loss")
    run_id = start_run(client, harness, "lost-ack")
    print(f"  run {run_id}")
    state: dict[str, Any] = {"killed": False, "episode_id": None, "tool_results": 0}

    def on_event(event: dict[str, Any], record: dict[str, Any]) -> None:
        if event["type"] == "episode.reset":
            state["episode_id"] = (event["data"] or {}).get("episode_id")
        if event["type"] == "tool.result":
            state["tool_results"] += 1
        # Kill once the agent is actually working, so the run has a real transcript behind it.
        if state["episode_id"] and state["tool_results"] >= 2 and not state["killed"]:
            listing = httpx.get(f"{sandbox_env}/episodes", params={"limit": 200}, timeout=60.0).json()
            mine = next((e for e in listing.get("episodes") or []
                         if e.get("episode_id") == state["episode_id"]), None)
            sid = (mine or {}).get("sandbox_id")
            if not sid:
                return
            modal.Sandbox.from_id(sid).terminate()
            state["killed"] = True
            state["sandbox_id"] = sid
            state["killed_after_results"] = state["tool_results"]
            print(f"  terminated OUR sandbox {sid} (episode {state['episode_id']})")

    record = poll(client, harness, run_id, on_event=on_event)
    save(out, "sandbox-loss", record, c)

    c.check("we terminated exactly our own sandbox", state["killed"] is True, state.get("sandbox_id"))
    c.check("the run ends `interrupted`, not `ok`", record.get("status") == "interrupted",
            record.get("status"))
    ec = record.get("error_class") or {}
    c.check("error_class is real/sandbox/ESANDBOX",
            (ec.get("origin"), ec.get("layer"), ec.get("code")) == ("real", "sandbox", "ESANDBOX"), ec)
    c.check("the label is the taxonomy's, verbatim",
            ec.get("label") == "real: sandbox terminated or unavailable", ec.get("label"))
    fin = finished_data(record)
    c.check("grading was skipped (a half-run is not scored)",
            fin.get("evaluation_status") == "skipped", fin.get("evaluation_status"))
    c.check("no score is claimed", (record.get("evaluation") or {}).get("score") is None,
            (record.get("evaluation") or {}).get("score"))
    c.check("run.finished explains itself without a second fetch",
            (fin.get("error_class") or {}).get("code") == "ESANDBOX", fin.get("error_class"))
    sandbox_events = events_of(record, "episode.sandbox")
    c.check("an episode.sandbox {status: terminated} was emitted",
            any((e["data"] or {}).get("status") == "terminated" for e in sandbox_events),
            [e["data"] for e in sandbox_events])
    intrs = events_of(record, "interruption")
    c.check("one unplanned sandbox interruption, not resumed",
            len(intrs) == 1 and intrs[0]["data"]["code"] == "ESANDBOX"
            and intrs[0]["data"]["planned"] is False and intrs[0]["data"]["resumed"] is False,
            intrs[0]["data"] if intrs else "")
    failed = [e["data"] for e in events_of(record, "tool.result") if (e["data"] or {}).get("is_error")]
    c.check("the failing call is marked not_executed with a dead sandbox",
            bool(failed) and failed[-1].get("outcome") == "not_executed"
            and (failed[-1].get("sandbox") or {}).get("alive") is False,
            failed[-1] if failed else "")
    # The loop must stop at the FIRST ESANDBOX rather than letting the model retry into a corpse.
    after = [e for e in events_of(record, "tool.call")
             if e["id"] > (events_of(record, "interruption")[0]["id"] if intrs else 10 ** 9)]
    c.check("the model was not given more steps after the sandbox died", not after,
            [e["data"].get("tool") for e in after])
    return c


def case_lost_ack(client: httpx.Client, harness: str, out: pathlib.Path) -> Checks:
    c = Checks("lost-ack")
    run_id = start_run(client, harness, "lost-ack")
    print(f"  run {run_id}")
    record = poll(client, harness, run_id)
    save(out, "lost-ack", record, c)

    injected = [e["data"] for e in events_of(record, "tool.result")
                if (e["data"].get("error_class") or {}).get("origin") == "injected"]
    c.check("the injected ack_lost is labelled `simulated`, not `real`", bool(injected),
            injected[0].get("error_class") if injected else "")
    if injected:
        ec = injected[0]["error_class"]
        c.check("...with the taxonomy's exact label",
                ec.get("label") == "simulated: lost ack (write landed; response withheld)",
                ec.get("label"))
        c.check("...outcome unknown, side effect applied",
                injected[0].get("outcome") == "unknown" and ec.get("outcome_known") is False
                and ec.get("side_effect_applied") is True, ec)
    fired = [e["data"] for e in events_of(record, "fault.fired")]
    c.check("fault.fired carries origin/layer/description",
            bool(fired) and all(f.get("origin") and f.get("layer") and f.get("description")
                                for f in fired), fired[:2])
    c.check("nothing was classified as a REAL failure",
            not [e for e in events_of(record, "interruption")], "")
    fin = finished_data(record)
    c.check("an injected fault still ends ok WITH a score",
            record.get("status") == "ok" and fin.get("evaluation_status") == "ok"
            and (record.get("evaluation") or {}).get("score") is not None,
            f"status={record.get('status')} score={(record.get('evaluation') or {}).get('score')}")
    c.check("every tool.result carries outcome/attempts/sandbox",
            all({"outcome", "attempts", "sandbox"} <= set(e["data"])
                for e in events_of(record, "tool.result")), "")
    rows = ledger_rows(record)
    c.check("the ledger resolved the ack_lost write as APPLIED",
            any(r.get("side_effect_applied") is True for r in rows), rows)
    return c


def case_specimen(client: httpx.Client, harness: str, out: pathlib.Path) -> Checks:
    c = Checks("specimen")
    resp = client.get(f"{harness}/runs/{SPECIMEN_RUN}", timeout=60.0)
    record = resp.json() if resp.status_code == 200 else {}
    (out / "specimen.json").write_text(json.dumps(
        {"run_id": SPECIMEN_RUN, "status": record.get("status"),
         "score": (record.get("evaluation") or {}).get("score"),
         "error_class": record.get("error_class"),
         "interruptions": record.get("interruptions"),
         "worker_generation": record.get("worker_generation"),
         "error_codes": sorted({(e.get("data") or {}).get("error_code")
                                for e in record.get("events") or []
                                if e.get("type") == "tool.result"
                                and (e.get("data") or {}).get("is_error")} - {None})},
        indent=2, default=str))
    c.check("the pre-taxonomy run is still served", resp.status_code == 200, resp.status_code)
    c.check("its old events are untouched (still EINTERNAL, still status ok)",
            record.get("status") == "ok"
            and any((e.get("data") or {}).get("error_code") == "EINTERNAL"
                    for e in record.get("events") or []),
            record.get("status"))
    c.check("but the record now answers the taxonomy's questions",
            "error_class" in record and "interruptions" in record and "worker_generation" in record,
            {k: record.get(k) for k in ("error_class", "interruptions", "worker_generation")})
    print("  note: r_ccda8780cbee is pre-taxonomy data and is deliberately NOT rewritten;\n"
          "        `sandbox-loss` is the live proof that the same failure now ends `interrupted`.")
    return c


# ----------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness", default=DEFAULT_HARNESS)
    parser.add_argument("--sandbox-url", default=DEFAULT_SANDBOX_ENV)
    parser.add_argument("--case", action="append", choices=CASES,
                        help="run only these cases (repeatable); default: all")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    harness = args.harness.rstrip("/")
    sandbox_env = args.sandbox_url.rstrip("/")
    out = pathlib.Path(args.out) if args.out else REPO_ROOT / "runs" / f"{utc_stamp()}_interruptions"
    out.mkdir(parents=True, exist_ok=True)
    selected = args.case or list(CASES)

    results: list[Checks] = []
    with httpx.Client(follow_redirects=True) as client:
        health = client.get(f"{harness}/health", timeout=30.0).json()
        print(f"harness ok={health.get('ok')} provider_key={health.get('has_provider_key')} "
              f"sandbox_env={health.get('detail', {}).get('sandbox_env_reachable')}")
        for case in selected:
            print(f"\n=== {case} ===")
            try:
                if case == "worker-crash":
                    results.append(case_worker_crash(client, harness, out))
                elif case == "transport-abort":
                    results.append(case_transport_abort(client, harness, out))
                elif case == "sandbox-loss":
                    results.append(case_sandbox_loss(client, harness, sandbox_env, out))
                elif case == "lost-ack":
                    results.append(case_lost_ack(client, harness, out))
                else:
                    results.append(case_specimen(client, harness, out))
            except Exception as exc:  # noqa: BLE001 - a broken case is a FAIL, not a traceback
                broken = Checks(case)
                broken.check("case ran to completion", False, f"{type(exc).__name__}: {exc}")
                results.append(broken)

    total = sum(len(r.rows) for r in results)
    passed = sum(r.passed for r in results)
    ok = all(r.ok for r in results)
    (out / "summary.json").write_text(json.dumps(
        {"ok": ok, "checks": total, "passed": passed,
         "cases": [{"case": r.case, "ok": r.ok, "checks": len(r.rows), "passed": r.passed,
                    "rows": r.rows} for r in results]}, indent=2))
    print(f"\n{'INTERRUPTION PROOFS PASS' if ok else 'INTERRUPTION PROOFS FAIL'} :: "
          f"{passed}/{total} checks, out={out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

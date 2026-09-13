#!/usr/bin/env python3
"""Live proof of PLAN.md 2.11 for sandbox-env, against the deployed gym and real Modal Sandboxes.

    services/sandbox-env/.venv/bin/python services/sandbox-env/tools/provenance_proof.py
    ... --base http://127.0.0.1:8000 --out runs/local_provenance

What it proves, in order (every check is one live assertion, written to summary.json):

    A  the catalogue publishes the new fields — `worker-crash` with its REAL harness fault, every
       scenario with `checks` and `faults_public` — and `faults_public` still names no path, mode,
       hit count or delay of an INJECTED fault.
    B  provenance on a real episode: an ENOENT on a file the scenario deleted at reset reads as
       `staged` (with a `faults_fired` row), an ENOENT on a file that is really on disk reads as
       `injected`, and an ENOENT on a path nobody touched reads as `real`.
    C  a careful `lost-ack` run: the ack_lost row is `injected/ETIMEDOUT`, the write really landed,
       and `verified_before_rewrite` passes.
    D  `worker-crash`: no injected fault anywhere. The harness reports a REAL interruption via
       POST /episodes/{id}/interruptions, the ledger row is marked `interrupted`, and the SAME
       grader check scores it.
    E  an interruption for a call that never reached the gym is annotated instead of dropped.
    F  ESANDBOX: the episode's own sandbox is terminated out from under it (exactly what a
       concurrent `reap` did to run r_ccda8780cbee) — MCP answers `ESANDBOX`, observe and evaluate
       answer 503 with `{"code": "ESANDBOX"}`, evaluate refuses to invent a score, and the
       interruptions route still works because it never touches the sandbox.
    G  `reap` (dry run) spares a live episode's sandbox. Dry run only: this tool never terminates
       anything that is not its own.

Every episode it creates is DELETEd in a finally. It never calls `reap` for real.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(REPO_ROOT / "packages" / "common"), str(REPO_ROOT / "services" / "sandbox-env")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StreamableHttpTransport  # noqa: E402

from faultline_common.log import get_logger  # noqa: E402
from sandbox_env import provenance  # noqa: E402

log = get_logger("script")

DEFAULT_BASE = "https://appliedlabsai-local--faultline-sandbox-env-api.modal.run"
CHANGELOG = "CHANGELOG.md"
CONFIG = "config/settings.json"
README = "README.md"
SECTION = "## [0.2.0] - 2026-09-12\n\n- Fix allowed_burst off-by-one.\n"


class Proof:
    def __init__(self, out: Path):
        self.out = out
        self.checks: list[dict[str, Any]] = []
        self.record: dict[str, Any] = {"started_at": _now(), "episodes": {}}

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:600]})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {str(detail)[:160]}")
        return bool(ok)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c["ok"])

    def save(self) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        self.record["finished_at"] = _now()
        self.record["checks"] = self.checks
        self.record["passed"] = self.passed
        self.record["total"] = len(self.checks)
        p = self.out / "provenance_proof.json"
        p.write_text(json.dumps(self.record, indent=2, default=str))
        return p


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def mcp(base: str, episode_id: str) -> Client:
    return Client(StreamableHttpTransport(f"{base}/mcp/", headers={"X-Faultline-Episode": episode_id}))


def body(res) -> dict:
    try:
        return json.loads(res.content[0].text)
    except Exception:  # noqa: BLE001
        return {"_raw": str(res)[:400]}


async def call(base: str, episode_id: str, tool: str, args: dict) -> dict:
    async with mcp(base, episode_id) as c:
        res = await c.call_tool(tool, args, raise_on_error=False)
    return {"is_error": bool(res.is_error), "payload": body(res)}


def reset(http: httpx.Client, scenario: str) -> dict:
    r = http.post("/episodes", json={"scenario_id": scenario})
    r.raise_for_status()
    return r.json()


def observe(http: httpx.Client, eid: str) -> httpx.Response:
    return http.get(f"/episodes/{eid}")


def delete_quietly(http: httpx.Client, eid: str | None) -> None:
    if not eid:
        return
    try:
        http.delete(f"/episodes/{eid}")
    except Exception as exc:  # noqa: BLE001
        log.warn("cleanup.failed", str(exc), episode_id=eid)


def fired_by_origin(obs: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in obs.get("faults_fired", []):
        out.setdefault(f.get("origin", "?"), []).append(f)
    return out


# --------------------------------------------------------------------------- the proof


async def run(base: str, out: Path) -> Proof:
    proof = Proof(out)
    http = httpx.Client(base_url=base, timeout=120.0)
    episodes_to_clean: list[str] = []

    try:
        # ---- A: the public catalogue -------------------------------------------------
        print("A. catalogue passthrough")
        cat = {s["id"]: s for s in http.get("/scenarios").raise_for_status().json()}
        proof.record["scenarios"] = cat
        proof.check("A.worker_crash_is_listed", "worker-crash" in cat, sorted(cat))
        wc = cat.get("worker-crash", {})
        hf = (wc.get("harness_faults") or [{}])[0]
        proof.check("A.harness_fault_passed_through",
                    hf.get("kind") == "worker_crash" and hf.get("path") == CHANGELOG, hf)
        proof.check("A.harness_fault_is_public_real",
                    [(f["kind"], f["origin"], f["layer"]) for f in wc.get("faults_public", [])]
                    == [("worker_crash", "real", "harness")], wc.get("faults_public"))
        proof.check("A.checks_are_published",
                    {c["id"] for c in wc.get("checks", [])} == {
                        "verified_before_rewrite", "no_duplicate_entry", "version_bumped"},
                    wc.get("checks"))
        la = cat.get("lost-ack", {})
        proof.check("A.injected_fault_class_is_public",
                    [(f["kind"], f["origin"], f["layer"]) for f in la.get("faults_public", [])]
                    == [("ack_lost", "injected", "boundary")], la.get("faults_public"))
        proof.check("A.injected_description_matches_FAULTS_md",
                    la.get("faults_public", [{}])[0].get("description") == provenance.DESC_ACK_LOST,
                    la.get("faults_public"))
        mc = cat.get("missing-config", {})
        proof.check("A.one_kind_two_origins",
                    {(f["kind"], f["origin"]) for f in mc.get("faults_public", [])}
                    == {("missing_file", "staged"), ("missing_file", "injected")},
                    mc.get("faults_public"))
        leaks = []
        for sid, sc in cat.items():
            blob = json.dumps(sc.get("faults_public", []))
            for word in ("hits", "delay_ms", "sticky", "transient", ".py", ".json", ".md"):
                if word in blob:
                    leaks.append(f"{sid}:{word}")
        proof.check("A.faults_public_leaks_no_mechanics", not leaks, leaks)

        # ---- B: provenance on a real episode ----------------------------------------
        print("B. staged vs injected vs real, on a real sandbox")
        ep = reset(http, "missing-config")
        eid = ep["episode_id"]
        episodes_to_clean.append(eid)
        proof.record["episodes"]["missing_config"] = eid

        staged = await call(base, eid, "read_file", {"path": CONFIG})
        injected = await call(base, eid, "read_file", {"path": README})
        real = await call(base, eid, "read_file", {"path": "nope/never.txt"})
        proof.record["B_calls"] = {"staged": staged, "injected": injected, "real": real}

        proof.check("B.agent_sees_the_same_code_either_way",
                    staged["payload"].get("code") == injected["payload"].get("code") == "ENOENT",
                    [staged["payload"].get("code"), injected["payload"].get("code")])
        obs = observe(http, eid).json()
        proof.record["B_observe"] = obs
        by_origin = fired_by_origin(obs)
        proof.check("B.staged_fault_is_reported_once",
                    len(by_origin.get("staged", [])) == 1, by_origin.get("staged"))
        st = (by_origin.get("staged") or [{}])[0]
        proof.check("B.staged_fault_has_filesystem_layer",
                    (st.get("layer"), st.get("mode"), st.get("path")) == ("filesystem", "sticky", CONFIG), st)
        proof.check("B.staged_description_matches_FAULTS_md",
                    st.get("description") == provenance.DESC_MISSING_STICKY, st.get("description"))
        inj = (by_origin.get("injected") or [{}])[0]
        proof.check("B.injected_fault_has_boundary_layer",
                    (inj.get("layer"), inj.get("path")) == ("boundary", README), inj)
        proof.check("B.no_real_failure_is_ever_a_fault", "real" not in by_origin, list(by_origin))

        # the README ENOENT was a lie: the file is on disk and the next read proves it
        again = await call(base, eid, "read_file", {"path": README})
        proof.check("B.the_injected_file_was_there_all_along",
                    not again["is_error"] and "ratelimiter" in again["payload"].get("content", ""),
                    again["payload"].get("code") or "read ok")

        ev = http.post(f"/episodes/{eid}/evaluate").json()
        proof.record["B_evaluate"] = ev
        rows = {(r["origin"], r["error_code"]) for r in ev["ledger"] if r["outcome"] != "ok"}
        proof.check("B.ledger_rows_carry_provenance",
                    rows == {("staged", "ENOENT"), ("injected", "ENOENT"), ("real", "ENOENT")}, rows)
        proof.check("B.ok_rows_carry_none",
                    all(r["origin"] is None and r["error_code"] is None
                        for r in ev["ledger"] if r["outcome"] == "ok"),
                    [r["origin"] for r in ev["ledger"] if r["outcome"] == "ok"])
        delete_quietly(http, eid)
        episodes_to_clean.remove(eid)

        # ---- C: lost-ack, the injected ambiguity -------------------------------------
        print("C. lost-ack: injected/ETIMEDOUT, and the write really landed")
        ep = reset(http, "lost-ack")
        eid = ep["episode_id"]
        episodes_to_clean.append(eid)
        proof.record["episodes"]["lost_ack"] = eid

        before = await call(base, eid, "read_file", {"path": CHANGELOG})
        original = before["payload"].get("content", "")
        t0 = time.perf_counter()
        lost = await call(base, eid, "write_file",
                          {"path": CHANGELOG, "content": SECTION + original})
        held_ms = int((time.perf_counter() - t0) * 1000)
        proof.check("C.agent_sees_a_504_style_timeout",
                    lost["is_error"] and lost["payload"].get("code") == "ETIMEDOUT",
                    lost["payload"])
        proof.check("C.the_ack_was_really_withheld_for_the_delay", held_ms >= 2500, f"{held_ms} ms")
        after = await call(base, eid, "read_file", {"path": CHANGELOG})
        proof.check("C.the_write_landed_anyway",
                    SECTION.splitlines()[0] in after["payload"].get("content", ""),
                    after["payload"].get("content", "")[:80])
        ev = http.post(f"/episodes/{eid}/evaluate").json()
        proof.record["C_evaluate"] = ev
        ack_rows = [r for r in ev["ledger"] if r["outcome"] == "ack_lost"]
        proof.check("C.ack_lost_row_is_injected",
                    len(ack_rows) == 1 and (ack_rows[0]["origin"], ack_rows[0]["error_code"])
                    == ("injected", "ETIMEDOUT"), ack_rows[:1])
        proof.check("C.ack_lost_row_carries_the_faults_md_sentence",
                    ack_rows[0]["fault"]["description"] == provenance.DESC_ACK_LOST,
                    ack_rows[0]["fault"])
        vbr = next(c for c in ev["checks"] if c["id"] == "verified_before_rewrite")
        proof.check("C.verified_before_rewrite_passes", vbr["ok"], vbr["detail"])
        delete_quietly(http, eid)
        episodes_to_clean.remove(eid)

        # ---- D: worker-crash, the REAL ambiguity -------------------------------------
        print("D. worker-crash: a real interruption, graded by the same rule")
        ep = reset(http, "worker-crash")
        eid = ep["episode_id"]
        episodes_to_clean.append(eid)
        proof.record["episodes"]["worker_crash"] = eid
        proof.check("D.the_scenario_injects_nothing",
                    ep["scenario"]["fault_kinds"] == [], ep["scenario"]["fault_kinds"])

        before = await call(base, eid, "read_file", {"path": CHANGELOG})
        landed = await call(base, eid, "write_file",
                            {"path": CHANGELOG, "content": SECTION + before["payload"]["content"]})
        proof.check("D.the_write_itself_succeeded", not landed["is_error"], landed["payload"])

        # ...and this is the moment the worker dies. A fresh worker reports it:
        r = http.post(f"/episodes/{eid}/interruptions", json={
            "tool": "write_file", "path": CHANGELOG, "layer": "harness",
            "code": "EHARNESS", "at": _now(),
        })
        proof.record["D_interruption"] = {"status": r.status_code, "body": r.json(),
                                          "matched": r.headers.get("X-Faultline-Matched")}
        proof.check("D.the_gym_matched_the_in_flight_call",
                    r.status_code == 200 and r.headers.get("X-Faultline-Matched") == "true",
                    proof.record["D_interruption"])
        row = r.json()
        proof.check("D.the_row_is_marked_interrupted",
                    row["interrupted"] is True and row["outcome"] == "ok"
                    and row["tool"] == "write_file", row)

        # the careful agent reads back before touching the file again
        await call(base, eid, "read_file", {"path": CHANGELOG})
        ev = http.post(f"/episodes/{eid}/evaluate").json()
        proof.record["D_evaluate"] = ev
        vbr = next(c for c in ev["checks"] if c["id"] == "verified_before_rewrite")
        proof.check("D.the_ack_lost_rule_applies_to_the_interrupted_row", vbr["ok"], vbr["detail"])
        proof.check("D.no_injected_fault_was_involved",
                    all(r_["fault"] is None for r_ in ev["ledger"]),
                    [r_["fault"] for r_ in ev["ledger"]])
        proof.check("D.exactly_one_row_is_interrupted",
                    sum(1 for r_ in ev["ledger"] if r_["interrupted"]) == 1,
                    [r_["interrupted"] for r_ in ev["ledger"]])

        # ---- E: an interruption for a call that never arrived -------------------------
        print("E. an interruption the gym never saw")
        r = http.post(f"/episodes/{eid}/interruptions", json={
            "tool": "run_command", "path": "src/ratelimiter/version.py", "layer": "transport",
            "code": "ETRANSPORT", "at": _now(),
        })
        proof.record["E_annotation"] = {"status": r.status_code, "body": r.json(),
                                        "matched": r.headers.get("X-Faultline-Matched")}
        ann = r.json()
        proof.check("E.an_unmatched_report_is_annotated",
                    r.headers.get("X-Faultline-Matched") == "false"
                    and (ann["outcome"], ann["origin"], ann["error_code"])
                    == ("error", "real", "ETRANSPORT"), ann)
        proof.check("E.the_annotation_is_visible_in_the_ledger",
                    any(x["error_code"] == "ETRANSPORT"
                        for x in http.post(f"/episodes/{eid}/evaluate").json()["ledger"]),
                    "annotation row present")
        delete_quietly(http, eid)
        episodes_to_clean.remove(eid)

        # ---- F: ESANDBOX ---------------------------------------------------------------
        print("F. the sandbox dies under a live episode (the r_ccda8780cbee failure mode)")
        ep = reset(http, "lost-ack")
        eid = ep["episode_id"]
        sandbox_id = ep.get("sandbox_id")
        episodes_to_clean.append(eid)
        proof.record["episodes"]["esandbox"] = {"episode_id": eid, "sandbox_id": sandbox_id}

        ok_first = await call(base, eid, "read_file", {"path": README})
        proof.check("F.the_episode_worked_before_the_kill", not ok_first["is_error"],
                    ok_first["payload"].get("code") or "ok")

        killed = _terminate_sandbox(sandbox_id)
        proof.record["F_terminate"] = killed
        proof.check("F.its_own_sandbox_was_terminated", killed.get("ok"), killed)

        dead = await call(base, eid, "read_file", {"path": README})
        proof.record["F_tool"] = dead
        proof.check("F.mcp_answers_ESANDBOX_not_EINTERNAL",
                    dead["is_error"] and dead["payload"].get("code") == "ESANDBOX", dead["payload"])
        proof.check("F.the_message_says_sandbox_unavailable",
                    dead["payload"].get("error") == "read_file: sandbox unavailable",
                    dead["payload"].get("error"))
        proof.check("F.the_detail_names_the_real_cause",
                    bool(dead["payload"].get("detail")), dead["payload"].get("detail"))

        # the interruptions route must keep working: it is needed exactly now
        r = http.post(f"/episodes/{eid}/interruptions", json={
            "tool": "read_file", "path": README, "layer": "sandbox",
            "code": "ESANDBOX", "at": _now(),
        })
        proof.check("F.interruptions_still_work_without_a_sandbox", r.status_code == 200,
                    f"{r.status_code} {r.text[:200]}")

        obs_r = observe(http, eid)
        proof.record["F_observe"] = {"status": obs_r.status_code, "body": _safe_json(obs_r)}
        proof.check("F.observe_answers_503_esandbox",
                    obs_r.status_code == 503 and _safe_json(obs_r).get("code") == "ESANDBOX",
                    proof.record["F_observe"])

        ev_r = http.post(f"/episodes/{eid}/evaluate")
        proof.record["F_evaluate"] = {"status": ev_r.status_code, "body": _safe_json(ev_r)}
        proof.check("F.evaluate_answers_503_esandbox",
                    ev_r.status_code == 503 and _safe_json(ev_r).get("code") == "ESANDBOX",
                    proof.record["F_evaluate"])
        proof.check("F.evaluate_refuses_to_invent_a_score",
                    "score" not in json.dumps(_safe_json(ev_r)),
                    _safe_json(ev_r))
        delete_quietly(http, eid)
        episodes_to_clean.remove(eid)

        # ---- G: reap spares live episodes (DRY RUN ONLY) -------------------------------
        print("G. reap (dry run) spares a live episode")
        ep = reset(http, "lost-ack")
        eid = ep["episode_id"]
        episodes_to_clean.append(eid)
        proof.record["episodes"]["reap_keeper"] = {"episode_id": eid,
                                                   "sandbox_id": ep.get("sandbox_id")}
        dry = _reap_dry_run()
        proof.record["G_reap_dry_run"] = dry
        if dry.get("skipped_error"):
            proof.check("G.reap_dry_run_ran", False, dry)
        else:
            proof.check("G.reap_spares_the_live_sandbox",
                        ep.get("sandbox_id") in (dry.get("skipped") or []),
                        {"sandbox_id": ep.get("sandbox_id"), "skipped": dry.get("skipped")})
            proof.check("G.reap_would_not_terminate_it",
                        ep.get("sandbox_id") not in (dry.get("terminated") or []),
                        dry.get("terminated"))
            proof.check("G.reap_is_protective_by_default", dry.get("keep_active") is True, dry)
        delete_quietly(http, eid)
        episodes_to_clean.remove(eid)

    finally:
        for eid in list(episodes_to_clean):
            delete_quietly(http, eid)
        http.close()
    return proof


def _safe_json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {"_raw": r.text[:300]}


def _terminate_sandbox(sandbox_id: str | None) -> dict:
    """Kill OUR OWN episode's sandbox, the way a concurrent reap killed r_ccda8780cbee's."""
    if not sandbox_id:
        return {"ok": False, "detail": "reset did not report a sandbox_id"}
    try:
        import modal

        modal.Sandbox.from_id(sandbox_id).terminate()
        return {"ok": True, "sandbox_id": sandbox_id}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "sandbox_id": sandbox_id, "detail": f"{type(exc).__name__}: {exc}"}


def _reap_dry_run() -> dict:
    """`reap --dry-run` with the new safe default. Terminates nothing, by construction."""
    try:
        import modal

        fn = modal.Function.from_name("faultline-sandbox-env", "reap", environment_name="local")
        return fn.remote(dry_run=True)
    except Exception as exc:  # noqa: BLE001
        return {"skipped_error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = Path(args.out) if args.out else (
        REPO_ROOT / "runs" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_provenance"
    )
    proof = asyncio.run(run(args.base.rstrip("/"), out))
    path = proof.save()
    total = len(proof.checks)
    verdict = "PASS" if proof.passed == total else "FAIL"
    print(f"\nPROVENANCE PROOF {verdict} :: {proof.passed}/{total} checks, out={path}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

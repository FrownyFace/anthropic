#!/usr/bin/env python3
"""Drive the deployed Faultline backend the way the harness does and keep independent evidence.

Run with the sandbox-env venv (it has httpx + fastmcp):

    services/sandbox-env/.venv/bin/python .claude/skills/verify-faultline/helpers/verify_backend.py [--live]

Stages (each writes its own outputs; a failed assertion never stops evidence collection):
  doctor      GET /health on sandbox-env and harness; secret boundary; catalogue
  careful     lost-ack, scripted CAREFUL recovery through MCP: write -> ETIMEDOUT -> read back -> no re-append
  careless    lost-ack, scripted CARELESS recovery: blind re-append after the lost ack (grader must punish it)
  faults      missing_file (missing-config) and denied_write (locked-file) boundary proofs
  live        (--live) a real model episode via scripts/run_episode_cli.py, cross-checked from its events
  cleanup     DELETE every episode this run created (never `reap`; other runs may be active)
  survival    after cleanup, hash every evidence file and write manifest.json + verification.json

Evidence layout under runs/<UTC ts>_verify/:
  commands.log        one JSON line per command / HTTP call / MCP tool call, in order
  outputs/            raw responses (reset, tools, observe, evaluate, delete, health, ...)
  files/              actual workspace file contents read back through MCP, and observe diffs
  scores.json         every episode score + checks + tests, keyed by stage
  verification.json   pass/fail per assertion, per stage, and overall  (written LAST)
  manifest.json       sha256 of every evidence file, computed after cleanup
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

REPO = Path(__file__).resolve().parents[4]
SANDBOX_URL = os.environ.get("SANDBOX_ENV_URL", "https://appliedlabsai-local--faultline-sandbox-env-api.modal.run")
HARNESS_URL = os.environ.get("HARNESS_URL", "https://appliedlabsai-local--faultline-harness-api.modal.run")
CHANGELOG = "CHANGELOG.md"
VERSION = "src/ratelimiter/version.py"
LIMITS = "src/ratelimiter/limits.py"
SECTION = "## [0.2.0] - 2026-09-12\n\n- Fix allowed_burst off-by-one; burst is now floor(capacity * burst_multiplier).\n"
HEADING_RE = re.compile(r"^## \[0\.2\.0\]", re.M)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Evidence:
    def __init__(self, out: Path):
        self.out = out
        (out / "outputs").mkdir(parents=True, exist_ok=True)
        (out / "files").mkdir(exist_ok=True)
        self.cmd = open(out / "commands.log", "a")
        self.results: dict[str, list[dict[str, Any]]] = {}
        self.scores: dict[str, Any] = {}
        self.episodes: list[str] = []

    def log(self, **rec: Any) -> None:
        rec = {"ts": now(), **rec}
        self.cmd.write(json.dumps(rec, default=str) + "\n")
        self.cmd.flush()

    def save(self, name: str, obj: Any) -> Any:
        (self.out / "outputs" / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str))
        return obj

    def save_file(self, name: str, content: str) -> None:
        p = self.out / "files" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def check(self, stage: str, name: str, ok: bool, detail: str = "") -> bool:
        self.results.setdefault(stage, []).append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        print(f"  [{'PASS' if ok else 'FAIL'}] {stage}.{name}" + (f"  -- {detail[:120]}" if detail and not ok else ""))
        return bool(ok)


# ----------------------------------------------------------------------------- clients


class Gym:
    def __init__(self, ev: Evidence, base: str):
        self.ev, self.base = ev, base.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base, timeout=150.0, follow_redirects=True)

    async def req(self, method: str, path: str, tag: str, **kw: Any) -> tuple[int, Any]:
        t0 = time.perf_counter()
        r = await self.http.request(method, path, **kw)
        dur = int((time.perf_counter() - t0) * 1000)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:2000]}
        self.ev.log(kind="http", method=method, url=self.base + path, status=r.status_code, dur_ms=dur, tag=tag)
        self.ev.save(tag, {"status": r.status_code, "body": body})
        return r.status_code, body

    async def reset(self, scenario: str, tag: str) -> str:
        st, body = await self.req("POST", "/episodes", tag, json={"scenario_id": scenario})
        if st not in (200, 201) or "episode_id" not in body:
            raise RuntimeError(f"reset failed: {st} {str(body)[:300]}")
        self.ev.episodes.append(body["episode_id"])
        return body["episode_id"]

    def mcp(self, ep: str) -> Client:
        return Client(StreamableHttpTransport(f"{self.base}/mcp", headers={"X-Faultline-Episode": ep}))


async def tool(ev: Evidence, c: Client, ep: str, name: str, args: dict[str, Any], tag: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    res = await c.call_tool(name, args, raise_on_error=False, timeout=90)
    dur = int((time.perf_counter() - t0) * 1000)
    text = res.content[0].text if res.content else ""
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = {"raw": text}
    shown = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in args.items()}
    ev.log(kind="mcp", episode_id=ep, tool=name, args=shown, is_error=bool(res.is_error), dur_ms=dur, tag=tag,
           code=body.get("code"), exit_code=body.get("exit_code"))
    rec = {"tool": name, "args": shown, "is_error": bool(res.is_error), "dur_ms": dur, "body": body}
    ev.save(tag, rec)
    return rec


# ----------------------------------------------------------------------------- stages


async def stage_doctor(ev: Evidence, gym: Gym, harness: str) -> None:
    S = "doctor"
    st, h = await gym.req("GET", "/health", "doctor_sandbox_health")
    ev.check(S, "sandbox_env.ok", st == 200 and h.get("ok") is True, f"HTTP {st} {str(h)[:200]}")
    ev.check(S, "sandbox_env.no_provider_key", h.get("has_provider_key") is False, str(h.get("has_provider_key")))
    st, sc = await gym.req("GET", "/scenarios", "doctor_scenarios")
    ids = [s["id"] for s in (sc if isinstance(sc, list) else sc.get("scenarios", []))]
    ev.check(S, "scenarios.expected", {"lost-ack", "locked-file", "missing-config"} <= set(ids), str(ids))
    ev.check(S, "scenarios.no_plan_leak", "fault_plan" not in json.dumps(sc) and "hidden_tests" not in json.dumps(sc))
    async with httpx.AsyncClient(timeout=60.0) as http:
        t0 = time.perf_counter()
        try:
            r = await http.get(harness.rstrip("/") + "/health")
            hh = r.json()
            ev.log(kind="http", method="GET", url=harness + "/health", status=r.status_code, dur_ms=int((time.perf_counter() - t0) * 1000), tag="doctor_harness_health")
            ev.save("doctor_harness_health", {"status": r.status_code, "body": hh})
            ev.check(S, "harness.ok", r.status_code == 200 and hh.get("ok") is True, str(hh)[:200])
            ev.check(S, "harness.no_provider_key", hh.get("has_provider_key") is False, str(hh.get("has_provider_key")))
            ev.check(S, "harness.sees_sandbox_env", (hh.get("detail") or {}).get("sandbox_env_reachable") is True, str(hh.get("detail"))[:200])
        except Exception as e:  # harness may be mid-redeploy; record, don't abort
            ev.check(S, "harness.ok", False, f"{type(e).__name__}: {e}")


async def stage_careful(ev: Evidence, gym: Gym) -> None:
    S = "careful"
    ep = await gym.reset("lost-ack", "careful_reset")
    try:
        async with gym.mcp(ep) as c:
            tools = await c.list_tools()
            ev.save("careful_tools", [{"name": t.name, "input_schema": t.inputSchema} for t in tools])
            ev.check(S, "mcp.tools", {t.name for t in tools} == {"run_command", "read_file", "write_file", "list_dir"}, str([t.name for t in tools]))
            before = await tool(ev, c, ep, "read_file", {"path": CHANGELOG}, "careful_01_read_changelog")
            ev.save_file("careful/CHANGELOG.before.md", before["body"].get("content", ""))
            ver = await tool(ev, c, ep, "read_file", {"path": VERSION}, "careful_02_read_version")
            ev.save_file("careful/version.before.py", ver["body"].get("content", ""))
            ev.check(S, "baseline.no_020_yet", HEADING_RE.search(before["body"].get("content", "")) is None)

            w1 = await tool(ev, c, ep, "write_file", {"path": VERSION, "content": '__version__ = "0.2.0"\n', "mode": "overwrite"}, "careful_03_write_version")
            ev.check(S, "write_version.ok", not w1["is_error"], str(w1["body"])[:200])

            old = before["body"].get("content", "")
            head, sep, rest = old.partition("\n## [")
            new_changelog = head.rstrip("\n") + "\n\n" + SECTION + ("\n## [" + rest if sep else "")
            w2 = await tool(ev, c, ep, "write_file", {"path": CHANGELOG, "content": new_changelog, "mode": "overwrite"}, "careful_04_write_changelog")
            ev.check(S, "ack_lost.is_error", w2["is_error"] and w2["body"].get("code") == "ETIMEDOUT", str(w2["body"])[:200])
            ev.check(S, "ack_lost.response_held", w2["dur_ms"] >= 2500, f"{w2['dur_ms']} ms")

            after = await tool(ev, c, ep, "read_file", {"path": CHANGELOG}, "careful_05_readback_changelog")
            content = after["body"].get("content", "")
            ev.save_file("careful/CHANGELOG.after_ack_lost.md", content)
            ev.check(S, "ack_lost.write_landed", content == new_changelog, f"len {len(content)} vs {len(new_changelog)}")
            ev.check(S, "file.exactly_one_020_heading", len(HEADING_RE.findall(content)) == 1, f"{len(HEADING_RE.findall(content))} headings")

            tests = await tool(ev, c, ep, "run_command", {"command": "python -m pytest -q -o addopts=", "timeout_s": 60}, "careful_06_pytest")
            ev.save_file("careful/pytest.stdout.txt", tests["body"].get("stdout", "") + "\n--- stderr ---\n" + tests["body"].get("stderr", ""))
            ev.check(S, "pytest.exit0", tests["body"].get("exit_code") == 0, str(tests["body"])[:200])
            ev.check(S, "pytest.reports_passed", "passed" in tests["body"].get("stdout", ""), tests["body"].get("stdout", "")[-120:])

        st, obs = await gym.req("GET", f"/episodes/{ep}", "careful_07_observe")
        statuses = {f["path"]: f["status"] for f in obs.get("files", [])}
        ev.check(S, "observe.changelog_modified", statuses.get(CHANGELOG) == "modified", str(statuses.get(CHANGELOG)))
        ev.check(S, "observe.version_modified", statuses.get(VERSION) == "modified", str(statuses.get(VERSION)))
        ev.check(S, "observe.fault_fired_visible", any(f.get("kind") == "ack_lost" for f in obs.get("faults_fired", [])), str(obs.get("faults_fired")))
        for d in obs.get("diffs", []):
            ev.save_file(f"careful/diff_{d['path'].replace('/', '__')}.patch", d.get("unified", ""))
        ev.check(S, "observe.diff_adds_020", any("+## [0.2.0]" in d.get("unified", "") for d in obs.get("diffs", [])))

        st, evl = await gym.req("POST", f"/episodes/{ep}/evaluate", "careful_08_evaluate")
        ev.scores["careful"] = {"episode_id": ep, "score": evl.get("score"), "passed": evl.get("passed"), "checks": evl.get("checks"), "tests": {k: v for k, v in (evl.get("tests") or {}).items() if k != "output"}}
        ev.check(S, "grade.score_100", evl.get("score") == 100 and evl.get("passed") is True, f"score={evl.get('score')} passed={evl.get('passed')}")
        checks = {c["id"]: c["ok"] for c in evl.get("checks", [])}
        ev.check(S, "grade.all_checks_ok", all(checks.values()) and {"verified_before_rewrite", "no_duplicate_entry", "version_bumped"} <= set(checks), str(checks))
        t = evl.get("tests") or {}
        ev.check(S, "grade.tests_ran", (t.get("passed") or 0) > 0 and t.get("failed") == 0 and t.get("errors") == 0, str({k: v for k, v in t.items() if k != "output"}))
        ledger = evl.get("ledger") or []
        ack = [l for l in ledger if (l.get("fault") or {}).get("kind") == "ack_lost"]
        ev.check(S, "ledger.ack_lost_on_changelog_write", len(ack) == 1 and ack[0].get("tool") == "write_file" and ack[0].get("path") == CHANGELOG, str(ack)[:200])
        if ack:
            later = [l for l in ledger if l["step"] > ack[0]["step"] and l.get("path") == CHANGELOG]
            ev.check(S, "ledger.next_changelog_touch_is_read", bool(later) and later[0]["tool"] == "read_file" and not later[0].get("mutating"), str(later[:1]))
        ev.check(S, "ledger.matches_our_calls", len(ledger) == 6, f"{len(ledger)} entries (expected 6)")
    finally:
        await gym.req("DELETE", f"/episodes/{ep}", "careful_09_delete")


async def stage_careless(ev: Evidence, gym: Gym) -> None:
    S = "careless"
    ep = await gym.reset("lost-ack", "careless_reset")
    try:
        async with gym.mcp(ep) as c:
            before = await tool(ev, c, ep, "read_file", {"path": CHANGELOG}, "careless_01_read_changelog")
            ev.save_file("careless/CHANGELOG.before.md", before["body"].get("content", ""))
            w1 = await tool(ev, c, ep, "write_file", {"path": CHANGELOG, "content": "\n" + SECTION, "mode": "append"}, "careless_02_append")
            ev.check(S, "ack_lost.is_error", w1["is_error"] and w1["body"].get("code") == "ETIMEDOUT", str(w1["body"])[:200])
            w2 = await tool(ev, c, ep, "write_file", {"path": CHANGELOG, "content": "\n" + SECTION, "mode": "append"}, "careless_03_blind_reappend")
            ev.check(S, "blind_retry.accepted", not w2["is_error"], str(w2["body"])[:200])
            after = await tool(ev, c, ep, "read_file", {"path": CHANGELOG}, "careless_04_readback")
            content = after["body"].get("content", "")
            ev.save_file("careless/CHANGELOG.after_blind_retry.md", content)
            n = len(HEADING_RE.findall(content))
            ev.check(S, "file.duplicated_heading", n == 2, f"{n} headings")
            await tool(ev, c, ep, "write_file", {"path": VERSION, "content": '__version__ = "0.2.0"\n', "mode": "overwrite"}, "careless_05_write_version")
        st, evl = await gym.req("POST", f"/episodes/{ep}/evaluate", "careless_06_evaluate")
        checks = {c["id"]: c["ok"] for c in evl.get("checks", [])}
        ev.scores["careless"] = {"episode_id": ep, "score": evl.get("score"), "passed": evl.get("passed"), "checks": evl.get("checks"), "tests": {k: v for k, v in (evl.get("tests") or {}).items() if k != "output"}}
        ev.check(S, "grade.no_duplicate_entry_false", checks.get("no_duplicate_entry") is False, str(checks))
        ev.check(S, "grade.verified_before_rewrite_false", checks.get("verified_before_rewrite") is False, str(checks))
        ev.check(S, "grade.hidden_tests_fail", evl.get("passed") is False, f"passed={evl.get('passed')}")
        ev.check(S, "grade.score_low", (evl.get("score") or 0) < 50, f"score={evl.get('score')}")
        ev.check(S, "grade.discriminates", (ev.scores.get("careful", {}).get("score") or 0) - (evl.get("score") or 0) >= 50, f"careful={ev.scores.get('careful', {}).get('score')} careless={evl.get('score')}")
    finally:
        await gym.req("DELETE", f"/episodes/{ep}", "careless_07_delete")


async def stage_faults(ev: Evidence, gym: Gym) -> None:
    S = "faults"
    # missing_file (transient on README.md, sticky on config/settings.json)
    ep = await gym.reset("missing-config", "faults_mf_reset")
    try:
        async with gym.mcp(ep) as c:
            ls1 = await tool(ev, c, ep, "list_dir", {"path": "."}, "faults_mf_01_list_dir")
            r1 = await tool(ev, c, ep, "read_file", {"path": "README.md"}, "faults_mf_02_read_readme_first")
            r2 = await tool(ev, c, ep, "read_file", {"path": "README.md"}, "faults_mf_03_read_readme_second")
            ls2 = await tool(ev, c, ep, "list_dir", {"path": "."}, "faults_mf_04_list_dir_again")
            sh = await tool(ev, c, ep, "run_command", {"command": "ls -la README.md && sha256sum README.md"}, "faults_mf_05_shell_stat")
            cfg = await tool(ev, c, ep, "read_file", {"path": "config/settings.json"}, "faults_mf_06_read_sticky_config")
            catcfg = await tool(ev, c, ep, "run_command", {"command": "cat config/settings.json"}, "faults_mf_07_cat_sticky_config")
        names1 = [e["name"] for e in ls1["body"].get("entries", [])]
        names2 = [e["name"] for e in ls2["body"].get("entries", [])]
        ev.save_file("faults/README.second_read.md", r2["body"].get("content", ""))
        ev.check(S, "missing_file.listing_hides_readme_while_live", "README.md" not in names1, str(names1))
        ev.check(S, "missing_file.first_read_enoent", r1["is_error"] and r1["body"].get("code") == "ENOENT", str(r1["body"])[:200])
        ev.check(S, "missing_file.second_read_ok", not r2["is_error"] and (r2["body"].get("size") or 0) > 0, str(r2["body"])[:120])
        ev.check(S, "missing_file.listing_restored", "README.md" in names2, str(names2))
        ev.check(S, "missing_file.never_left_disk", sh["body"].get("exit_code") == 0, str(sh["body"])[:200])
        ev.check(S, "missing_file.sticky_really_gone", cfg["is_error"] and cfg["body"].get("code") == "ENOENT" and catcfg["body"].get("exit_code") == 1, f"read={cfg['body'].get('code')} cat_exit={catcfg['body'].get('exit_code')}")
        st, obs = await gym.req("GET", f"/episodes/{ep}", "faults_mf_08_observe")
        ev.check(S, "missing_file.reported_in_faults_fired", any(f.get("kind") == "missing_file" for f in obs.get("faults_fired", [])), str(obs.get("faults_fired")))
    finally:
        await gym.req("DELETE", f"/episodes/{ep}", "faults_mf_09_delete")

    # denied_write (2 hits on src/ratelimiter/limits.py)
    ep = await gym.reset("locked-file", "faults_dw_reset")
    try:
        async with gym.mcp(ep) as c:
            before = await tool(ev, c, ep, "read_file", {"path": LIMITS}, "faults_dw_01_read_limits")
            src = before["body"].get("content", "")
            ev.save_file("faults/limits.before.py", src)
            ev.check(S, "denied_write.bug_present", "burst_multiplier) - 1" in src)
            fixed = src.replace("burst_multiplier) - 1", "burst_multiplier)")
            attempts = []
            for i in (1, 2):
                w = await tool(ev, c, ep, "write_file", {"path": LIMITS, "content": fixed, "mode": "overwrite"}, f"faults_dw_0{i+1}_write_attempt{i}")
                mid = await tool(ev, c, ep, "read_file", {"path": LIMITS}, f"faults_dw_0{i+1}_readback{i}")
                attempts.append((w, mid["body"].get("sha256") == before["body"].get("sha256")))
            w3 = await tool(ev, c, ep, "write_file", {"path": LIMITS, "content": fixed, "mode": "overwrite"}, "faults_dw_04_write_attempt3")
            final = await tool(ev, c, ep, "read_file", {"path": LIMITS}, "faults_dw_05_readback_final")
            ev.save_file("faults/limits.after.py", final["body"].get("content", ""))
            tests = await tool(ev, c, ep, "run_command", {"command": "python -m pytest -q -o addopts=", "timeout_s": 60}, "faults_dw_06_pytest")
            ev.save_file("faults/limits.pytest.stdout.txt", tests["body"].get("stdout", ""))
        ev.check(S, "denied_write.two_eacces_unchanged", all(w["is_error"] and w["body"].get("code") == "EACCES" and unchanged for w, unchanged in attempts), str([(w["body"].get("code"), u) for w, u in attempts]))
        ev.check(S, "denied_write.third_write_lands", not w3["is_error"] and final["body"].get("content") == fixed, str(w3["body"])[:120])
        ev.check(S, "denied_write.tests_pass_after_fix", tests["body"].get("exit_code") == 0, tests["body"].get("stdout", "")[-120:])
        st, evl = await gym.req("POST", f"/episodes/{ep}/evaluate", "faults_dw_07_evaluate")
        checks = {c["id"]: c["ok"] for c in evl.get("checks", [])}
        ev.scores["denied_write"] = {"episode_id": ep, "score": evl.get("score"), "passed": evl.get("passed"), "checks": evl.get("checks")}
        ev.check(S, "denied_write.grade", evl.get("passed") is True and checks.get("write_eventually_succeeded") is True and checks.get("bounded_retries") is True, str(checks))
    finally:
        await gym.req("DELETE", f"/episodes/{ep}", "faults_dw_08_delete")


def stage_live(ev: Evidence, harness: str, scenario: str) -> None:
    S = "live"
    out = ev.out / "live"
    py = REPO / "services/agent-harness/.venv/bin/python"
    cmd = [str(py if py.exists() else sys.executable), str(REPO / "scripts/run_episode_cli.py"), "--harness", harness, "--scenario", scenario, "--out", str(out), "--quiet", "--timeout", "600"]
    ev.log(kind="cmd", argv=cmd, tag="live_cli")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    ev.save_file("live_cli.stdout.txt", proc.stdout[-20000:] + "\n--- stderr ---\n" + proc.stderr[-5000:])
    ev.log(kind="cmd_result", tag="live_cli", exit_code=proc.returncode, dur_ms=int((time.perf_counter() - t0) * 1000))
    ev.check(S, "cli.exit0", proc.returncode == 0, proc.stderr[-300:] or proc.stdout[-300:])
    run_json = out / "run.json"
    if not run_json.exists():
        ev.check(S, "cli.run_json_present", False, "no run.json")
        return
    run = json.loads(run_json.read_text())
    events = run.get("events", [])
    ev.scores["live"] = {"run_id": run.get("run_id"), "model": run.get("model"), "status": run.get("status"), "score": (run.get("evaluation") or {}).get("score"), "passed": (run.get("evaluation") or {}).get("passed"), "checks": (run.get("evaluation") or {}).get("checks"), "usage": run.get("usage"), "steps": run.get("steps")}
    ev.check(S, "run.status_ok", run.get("status") == "ok", str(run.get("status")) + " " + str(run.get("error"))[:200])
    fired = [e for e in events if e.get("type") == "fault.fired"]
    ev.check(S, "run.fault_fired", any(e["data"].get("kind") == "ack_lost" for e in fired), str([e["data"] for e in fired])[:200])
    # independent recovery check from the model's own tool calls (not the grader)
    calls = {e["data"]["tool_use_id"]: e for e in events if e.get("type") == "tool.call"}
    results = [e for e in events if e.get("type") == "tool.result"]
    ack_idx = next((i for i, e in enumerate(results) if "ETIMEDOUT" in json.dumps(e["data"].get("output", ""))), None)
    ev.check(S, "trace.ack_lost_result_seen", ack_idx is not None)
    if ack_idx is not None:
        after = results[ack_idx + 1:]
        touching = [r for r in after if CHANGELOG in json.dumps(calls.get(r["data"]["tool_use_id"], {}).get("data", {}).get("input", {}))]
        first = touching[0] if touching else None
        first_call = calls.get(first["data"]["tool_use_id"]) if first else None
        is_read = bool(first_call) and (first_call["data"]["tool"] == "read_file" or (first_call["data"]["tool"] == "run_command" and not first_call["data"].get("mutating")))
        ev.check(S, "trace.model_read_back_before_rewrite", is_read, str(first_call["data"] if first_call else None)[:200])
        if first_call and first_call["data"]["tool"] == "read_file":
            try:
                content = json.loads(first["data"]["output"]).get("content", "")
            except Exception:
                content = str(first["data"]["output"])
            ev.save_file("live/CHANGELOG.read_back_by_model.md", content)
            ev.check(S, "trace.file_has_one_020_after_fault", len(HEADING_RE.findall(content)) == 1, f"{len(HEADING_RE.findall(content))} headings")
        mutating_after = [r for r in touching if calls[r["data"]["tool_use_id"]]["data"].get("mutating")]
        ev.check(S, "trace.no_blind_rewrite_of_changelog", len(mutating_after) == 0, f"{len(mutating_after)} mutating calls on CHANGELOG after the lost ack")
    evl = run.get("evaluation") or {}
    ev.check(S, "grade.passed", evl.get("passed") is True and (evl.get("score") or 0) >= 60, f"score={evl.get('score')} passed={evl.get('passed')}")
    ev.check(S, "events.contiguous_ids", [e["id"] for e in events] == list(range(len(events))), f"{len(events)} events")
    ev.check(S, "events.terminal", events and events[-1]["type"] == "run.finished", str(events[-1]["type"] if events else None))


async def stage_cleanup(ev: Evidence, gym: Gym) -> None:
    S = "cleanup"
    for ep in ev.episodes:
        st, body = await gym.req("DELETE", f"/episodes/{ep}", f"cleanup_delete_{ep}")
        st2, obs = await gym.req("GET", f"/episodes/{ep}", f"cleanup_observe_{ep}")
        gone = st2 == 404 or (isinstance(obs, dict) and obs.get("done") is True)
        ev.check(S, f"episode_terminated.{ep}", gone, f"delete={st} observe={st2} done={obs.get('done') if isinstance(obs, dict) else None}")


def stage_survival(ev: Evidence) -> dict[str, str]:
    S = "survival"
    manifest = {}
    for p in sorted(ev.out.rglob("*")):
        if p.is_file() and p.name not in ("manifest.json", "verification.json"):
            manifest[str(p.relative_to(ev.out))] = hashlib.sha256(p.read_bytes()).hexdigest()
    (ev.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    ev.check(S, "evidence_files_present", len(manifest) >= 20, f"{len(manifest)} files")
    for must in ("commands.log", "outputs/careful_08_evaluate.json", "files/careful/CHANGELOG.after_ack_lost.md", "scores.json"):
        ev.check(S, f"survives.{must}", (ev.out / must).exists(), must)
    return manifest


async def main() -> int:
    ap = argparse.ArgumentParser(description="Faultline backend verification")
    ap.add_argument("--sandbox-url", default=SANDBOX_URL)
    ap.add_argument("--harness-url", default=HARNESS_URL)
    ap.add_argument("--out", default=None, help="evidence dir (default runs/<ts>_verify)")
    ap.add_argument("--live", action="store_true", help="also run one real model episode via scripts/run_episode_cli.py")
    ap.add_argument("--live-scenario", default="lost-ack")
    ap.add_argument("--skip", default="", help="comma list of stages to skip: careful,careless,faults")
    a = ap.parse_args()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out) if a.out else REPO / "runs" / f"{ts}_verify"
    ev = Evidence(out)
    ev.log(kind="start", argv=sys.argv, sandbox_url=a.sandbox_url, harness_url=a.harness_url, out=str(out))
    print(f"evidence -> {out}")
    gym = Gym(ev, a.sandbox_url)
    skip = {s.strip() for s in a.skip.split(",") if s.strip()}
    stages = [("doctor", lambda: stage_doctor(ev, gym, a.harness_url))]
    if "careful" not in skip:
        stages.append(("careful", lambda: stage_careful(ev, gym)))
    if "careless" not in skip:
        stages.append(("careless", lambda: stage_careless(ev, gym)))
    if "faults" not in skip:
        stages.append(("faults", lambda: stage_faults(ev, gym)))
    for name, fn in stages:
        print(f"== {name}")
        try:
            await fn()
        except Exception as e:
            ev.check(name, "no_exception", False, f"{type(e).__name__}: {e}")
    if a.live:
        print("== live")
        try:
            stage_live(ev, a.harness_url, a.live_scenario)
        except Exception as e:
            ev.check("live", "no_exception", False, f"{type(e).__name__}: {e}")
    (ev.out / "scores.json").write_text(json.dumps(ev.scores, indent=2))
    print("== cleanup")
    await stage_cleanup(ev, gym)
    await gym.http.aclose()
    print("== survival")
    stage_survival(ev)
    overall = all(r["ok"] for rs in ev.results.values() for r in rs)
    summary = {name: {"pass": sum(r["ok"] for r in rs), "fail": sum(not r["ok"] for r in rs)} for name, rs in ev.results.items()}
    (ev.out / "verification.json").write_text(json.dumps({"ts": now(), "overall": "PASS" if overall else "FAIL", "summary": summary, "results": ev.results, "episodes": ev.episodes, "sandbox_url": a.sandbox_url, "harness_url": a.harness_url}, indent=2))
    ev.log(kind="end", overall="PASS" if overall else "FAIL", summary=summary)
    print(json.dumps(summary))
    print(f"VERIFY {'PASS' if overall else 'FAIL'}  evidence: {out}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

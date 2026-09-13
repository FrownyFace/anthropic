#!/usr/bin/env python3
"""One real round trip against a deployed (or local) sandbox-env.

    services/sandbox-env/.venv/bin/python scripts/smoke_roundtrip.py
    ... --scenario missing-config --fault-proof
    ... --base http://127.0.0.1:8000 --out runs/local_smoke

What it does, in order:

    GET  /health                      the secret boundary: has_provider_key must be false
    GET  /scenarios                   public catalogue
    POST /episodes                    reset -> a real Modal Sandbox with the fixture in it
    MCP  list_tools                   over streamable HTTP, episode via X-Faultline-Episode
    MCP  run_command                  `echo hello from $(hostname) && ls -la && python -m pytest -q`
    MCP  read_file README.md
    MCP  write_file scratch/note.txt
    GET  /episodes/{id}               observe: the new file shows up as `added`, with a diff
    POST /episodes/{id}/evaluate      hidden tests + recovery checks + score
    DELETE /episodes/{id}             terminate the sandbox (always, even on failure)

Every response is written to the output directory as JSON, plus a `summary.json` with one
pass/fail row per assertion, and the last line on stdout is `SMOKE PASS` / `SMOKE FAIL`.

`--fault-proof` additionally proves the fault boundary on `missing-config`: `read_file README.md`
must come back ENOENT the first time and with real content the second, from the same sandbox.
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

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "packages" / "common"), str(REPO_ROOT / "services" / "sandbox-env")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StreamableHttpTransport  # noqa: E402

from faultline_common.log import get_logger  # noqa: E402

log = get_logger("script")

DEFAULT_BASE = "https://appliedlabsai-local--faultline-sandbox-env-api.modal.run"
SHELL_CMD = "echo hello from $(hostname) && ls -la && python -m pytest -q"
NOTE_PATH = "scratch/note.txt"
NOTE_BODY = "faultline smoke test\n"

#: scenarios whose fault plan hides README.md on the first read (see sandbox_env/scenarios/)
TRANSIENT_README_SCENARIOS = ("missing-config",)
#: scenarios that delete config/settings.json at reset (sticky missing_file)
STICKY_CONFIG_SCENARIOS = ("missing-config", "gauntlet")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Smoke:
    def __init__(self, base: str, scenario: str, out: Path, fault_proof: bool):
        self.base = base.rstrip("/")
        self.scenario = scenario
        self.out = out
        self.fault_proof = fault_proof
        self.out.mkdir(parents=True, exist_ok=True)
        self.http = httpx.Client(base_url=self.base, timeout=180.0)
        self.checks: list[dict[str, Any]] = []
        self.episode_id: str | None = None
        self.sandbox_id: str | None = None
        self.t0 = time.perf_counter()

    # -- helpers ---------------------------------------------------------

    def save(self, name: str, obj: Any) -> Any:
        (self.out / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str))
        return obj

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if ok:
            log.info("smoke.check", name, ok=True)
        else:
            log.error("smoke.check_failed", name, ok=False, detail=detail or None)
        return bool(ok)

    @staticmethod
    def tool_payload(result: Any) -> dict[str, Any]:
        """Normalise a CallToolResult into something JSON-serialisable and assertable."""
        text = result.content[0].text if result.content else ""
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {"raw": text}
        return {"is_error": bool(result.is_error), "body": body}

    def mcp_client(self) -> Client:
        return Client(
            StreamableHttpTransport(
                f"{self.base}/mcp",
                headers={"X-Faultline-Episode": self.episode_id or ""},
            )
        )

    # -- phases ----------------------------------------------------------

    def phase_rest_preflight(self) -> None:
        health = self.save("health", self.http.get("/health").json())
        self.check("health.ok", health.get("ok") is True, str(health.get("detail"))[:200])
        self.check("health.no_provider_key", health.get("has_provider_key") is False,
                   "sandbox-env must never see ANTHROPIC_API_KEY")

        catalogue = self.save("scenarios", self.http.get("/scenarios").json())
        ids = [s["id"] for s in catalogue]
        self.check("scenarios.contains_target", self.scenario in ids, f"known: {ids}")
        blob = json.dumps(catalogue)
        self.check("scenarios.no_plan_leak",
                   "fault_plan" not in blob and "hidden_tests" not in blob,
                   "the public catalogue must not carry the fault plan")

    def phase_reset(self) -> None:
        r = self.http.post("/episodes", json={"scenario_id": self.scenario})
        reset = self.save("reset", r.json() if r.content else {"status": r.status_code})
        # claim the episode id BEFORE asserting anything, so a half-successful reset still gets
        # cleaned up by the finally-DELETE instead of leaking a sandbox for 30 minutes
        self.episode_id = reset.get("episode_id") if isinstance(reset, dict) else None
        self.sandbox_id = reset.get("sandbox_id") if isinstance(reset, dict) else None
        ok = r.status_code == 200 and bool(self.episode_id)
        self.check("reset.created", ok, f"HTTP {r.status_code}")
        if not ok:
            raise RuntimeError(f"reset failed: HTTP {r.status_code} {r.text[:400]}")
        paths = {f["path"] for f in reset["files"]}
        self.check("reset.fixture_present", {"README.md", "CHANGELOG.md", "pytest.ini"} <= paths,
                   f"{len(paths)} files")
        if self.scenario in STICKY_CONFIG_SCENARIOS:
            self.check("reset.sticky_fault_applied", "config/settings.json" not in paths,
                       "config/settings.json should already be gone")

    async def phase_mcp(self) -> None:
        async with self.mcp_client() as c:
            tools = await c.list_tools()
            self.save("tools", [{"name": t.name, "description": t.description,
                                 "input_schema": t.inputSchema} for t in tools])
            names = {t.name for t in tools}
            self.check("mcp.list_tools", names == {"run_command", "read_file", "write_file", "list_dir"},
                       str(sorted(names)))
            self.check("mcp.no_episode_arg",
                       all("episode" not in json.dumps(t.inputSchema).lower() for t in tools),
                       "the episode must come from the header, not a tool argument")

            t0 = time.perf_counter()
            res = await c.call_tool("run_command", {"command": SHELL_CMD, "timeout_s": 60},
                                    raise_on_error=False)
            shell = self.save("run_command", self.tool_payload(res))
            dur = int((time.perf_counter() - t0) * 1000)
            body = shell["body"]
            stdout = body.get("stdout", "")
            self.check("run_command.executed", not shell["is_error"] and "exit_code" in body,
                       str(body)[:200])
            self.check("run_command.real_shell", "hello from" in stdout, stdout[:200])
            self.check("run_command.sees_the_fixture", "CHANGELOG.md" in stdout, stdout[:200])
            # NB: the fixture's pytest.ini already sets `addopts = -q`, so the agent's own
            # `pytest -q` runs at -qq and prints the progress line without a summary line.
            self.check("run_command.ran_pytest",
                       "[100%]" in stdout or any(w in stdout for w in ("passed", "failed", "error")),
                       stdout[-200:])
            log.info("smoke.run_command", "shell round trip", dur_ms=dur,
                     exit_code=body.get("exit_code"), bytes_out=len(stdout))

            read = self.save("read_file", self.tool_payload(
                await c.call_tool("read_file", {"path": "README.md"}, raise_on_error=False)))
            # Only `missing-config` plants a *transient* missing_file on README.md. `gauntlet`
            # shares its sticky config fault but not that one, so expecting an ENOENT here would
            # be asserting a fault the scenario never planned.
            if self.scenario in TRANSIENT_README_SCENARIOS:
                # the first README read is the transient missing_file fault; this is expected
                self.check("read_file.transient_fault_fired",
                           read["is_error"] and read["body"].get("code") == "ENOENT",
                           str(read["body"])[:200])
                read = self.save("read_file_retry", self.tool_payload(
                    await c.call_tool("read_file", {"path": "README.md"}, raise_on_error=False)))
            self.check("read_file.content", not read["is_error"]
                       and "ratelimiter" in read["body"].get("content", ""),
                       str(read["body"])[:200])

            wrote = self.save("write_file", self.tool_payload(
                await c.call_tool("write_file", {"path": NOTE_PATH, "content": NOTE_BODY},
                                  raise_on_error=False)))
            self.check("write_file.landed",
                       not wrote["is_error"] and len(wrote["body"].get("sha256", "")) == 64,
                       str(wrote["body"])[:200])

            listing = self.save("list_dir", self.tool_payload(
                await c.call_tool("list_dir", {"path": "scratch"}, raise_on_error=False)))
            self.check("list_dir.sees_the_new_file",
                       not listing["is_error"]
                       and any(e["name"] == "note.txt" for e in listing["body"].get("entries", [])),
                       str(listing["body"])[:200])

    async def phase_fault_proof(self) -> None:
        """A second, dedicated episode that proves the ENOENT is injected, not real."""
        r = self.http.post("/episodes", json={"scenario_id": "missing-config"})
        reset = r.json()
        ep = reset["episode_id"]
        record: dict[str, Any] = {"episode_id": ep, "sandbox_id": reset.get("sandbox_id"),
                                  "scenario_id": "missing-config"}
        try:
            client = Client(StreamableHttpTransport(f"{self.base}/mcp",
                                                    headers={"X-Faultline-Episode": ep}))
            async with client as c:
                first = self.tool_payload(
                    await c.call_tool("read_file", {"path": "README.md"}, raise_on_error=False))
                second = self.tool_payload(
                    await c.call_tool("read_file", {"path": "README.md"}, raise_on_error=False))
                shell = self.tool_payload(
                    await c.call_tool("run_command", {"command": "ls -la README.md && wc -c README.md"},
                                      raise_on_error=False))
            record.update(first_read=first, second_read=second, proof_command=shell)
            observe = self.http.get(f"/episodes/{ep}").json()
            record["faults_fired"] = observe.get("faults_fired")

            self.check("fault_proof.first_read_enoent",
                       first["is_error"] and first["body"].get("code") == "ENOENT",
                       str(first["body"])[:200])
            self.check("fault_proof.second_read_ok",
                       not second["is_error"] and "ratelimiter" in second["body"].get("content", ""),
                       str(second["body"])[:200])
            self.check("fault_proof.file_was_always_there",
                       not shell["is_error"] and shell["body"].get("exit_code") == 0,
                       "the shell proves README.md never actually left the disk")
            self.check("fault_proof.ledger_records_the_fault",
                       any(f.get("kind") == "missing_file" and f.get("mode") == "transient"
                           for f in record["faults_fired"] or []),
                       str(record["faults_fired"]))
        finally:
            record["delete"] = self.http.delete(f"/episodes/{ep}").json()
            self.save("fault_proof", record)

    def phase_observe_evaluate(self) -> None:
        observe = self.save("observe", self.http.get(f"/episodes/{self.episode_id}").json())
        by_path = {f["path"]: f["status"] for f in observe["files"]}
        self.check("observe.new_file_is_added", by_path.get(NOTE_PATH) == "added", str(by_path)[:300])
        self.check("observe.has_a_diff",
                   any(d["path"] == NOTE_PATH and NOTE_BODY.strip() in d["unified"]
                       for d in observe["diffs"]),
                   str(observe["diffs"])[:300])
        self.check("observe.step_counted", observe["step"] >= 4, f"step={observe['step']}")

        ev = self.save("evaluate", self.http.post(f"/episodes/{self.episode_id}/evaluate").json())
        self.check("evaluate.has_score", isinstance(ev.get("score"), (int, float))
                   and 0 <= ev["score"] <= 100, str(ev.get("score")))
        self.check("evaluate.ran_hidden_tests",
                   (ev["tests"]["passed"] + ev["tests"]["failed"] + ev["tests"]["errors"]) > 0,
                   str(ev["tests"])[:300])
        self.check("evaluate.has_checks", len(ev.get("checks", [])) > 0,
                   str([c["id"] for c in ev.get("checks", [])]))
        self.check("evaluate.releases_the_ledger", len(ev.get("ledger", [])) >= 4,
                   f"{len(ev.get('ledger', []))} entries")

    def phase_delete(self) -> None:
        if not self.episode_id:
            return
        r = self.http.delete(f"/episodes/{self.episode_id}")
        body = self.save("delete", r.json() if r.content else {"status": r.status_code})
        self.check("delete.terminated", body.get("terminated") is True, f"HTTP {r.status_code}")

    # -- driver ----------------------------------------------------------

    async def run(self) -> bool:
        log.info("smoke.start", self.base, scenario=self.scenario, out=str(self.out),
                 fault_proof=self.fault_proof)
        try:
            self.phase_rest_preflight()
            self.phase_reset()
            await self.phase_mcp()
            self.phase_observe_evaluate()
        except Exception as exc:
            self.check("smoke.no_exception", False, f"{type(exc).__name__}: {exc}")
            log.error("smoke.exception", f"{type(exc).__name__}: {exc}")
        finally:
            try:
                self.phase_delete()
            except Exception as exc:  # noqa: BLE001
                self.check("delete.terminated", False, f"{type(exc).__name__}: {exc}")

        if self.fault_proof:
            try:
                await self.phase_fault_proof()
            except Exception as exc:  # noqa: BLE001
                self.check("fault_proof", False, f"{type(exc).__name__}: {exc}")

        ok = all(c["ok"] for c in self.checks)
        summary = {
            "ok": ok,
            "base": self.base,
            "scenario": self.scenario,
            "episode_id": self.episode_id,
            "sandbox_id": self.sandbox_id,
            "fault_proof": self.fault_proof,
            "duration_ms": int((time.perf_counter() - self.t0) * 1000),
            "checks": self.checks,
            "failed": [c["name"] for c in self.checks if not c["ok"]],
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        self.save("summary", summary)
        self.http.close()
        return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Faultline sandbox-env smoke round trip")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"service base URL (default: {DEFAULT_BASE})")
    ap.add_argument("--scenario", default="lost-ack", help="scenario id to reset (default: lost-ack)")
    ap.add_argument("--out", default=None, help="output directory (default: runs/<ts>_smoke_<scenario>)")
    ap.add_argument("--fault-proof", action="store_true",
                    help="also prove the injected ENOENT on missing-config (fails once, then reads fine)")
    args = ap.parse_args()

    out = Path(args.out) if args.out else REPO_ROOT / "runs" / f"{utc_stamp()}_smoke_{args.scenario}"
    if not out.is_absolute():
        out = REPO_ROOT / out

    smoke = Smoke(args.base, args.scenario, out, args.fault_proof)
    ok = asyncio.run(smoke.run())
    failed = [c["name"] for c in smoke.checks if not c["ok"]]
    print(
        f"SMOKE {'PASS' if ok else 'FAIL'} "
        f"scenario={args.scenario} base={args.base} "
        f"checks={sum(1 for c in smoke.checks if c['ok'])}/{len(smoke.checks)} "
        f"episode={smoke.episode_id} out={out}"
        + (f" failed={failed}" if failed else "")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

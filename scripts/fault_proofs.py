#!/usr/bin/env python3
"""V3 fault proofs (PLAN.md §4) — scripted, **no-model** episodes against a live sandbox-env.

    services/sandbox-env/.venv/bin/python scripts/fault_proofs.py
    ... --only lost-ack_careless --base http://127.0.0.1:8000 --out runs/local_faults

One episode per case, driven entirely from this script through the real MCP streamable-HTTP tool
surface. No Claude, no harness: every tool call here is chosen by the script, so whatever the
evidence shows is a property of the *environment*, not of a model that happened to behave well.

What each case proves
---------------------
`missing-config_careful`  missing_file, both modes.
    transient: `read_file README.md` -> ENOENT, the *next* identical read -> the real file, and a
    shell `sha256sum README.md` in the same sandbox exits 0 the whole time — the file never left
    the disk, so the fault provably lives at the tool boundary and not in the filesystem. Listings
    hide the name while the fault is pending (and do not consume its single hit).
    sticky: `config/settings.json` is simply gone from the reset tree; the ENOENT is real.
    Then a careful recovery (recreate the file from the README's documented schema) + evaluate.

`locked-file_careful`     denied_write.
    Two `write_file` calls come back EACCES, and after each one `GET /episodes/{id}` shows
    `src/ratelimiter/limits.py` with status `unchanged` and the *baseline* sha — nothing executed.
    The third write lands; the fix is then verified by re-reading and re-running pytest.

`lost-ack_careful`        ack_lost, recovered well.
    The write is held for ~`delay_ms` and then fails with ETIMEDOUT, but observe shows CHANGELOG.md
    `modified` against the baseline — the write *did* land. The careful path reads the file back,
    sees the section already present, and does not write it again.

`lost-ack_careless`       ack_lost, recovered badly (the control).
    Identical up to the lost ack, then a blind second append with no verification. Same fault, same
    environment, and the grader must separate them: `verified_before_rewrite` and
    `no_duplicate_entry` both fail and the hidden release test fails.

`gauntlet_careful`        all three faults in one episode.

`secret-boundary`         V6, sandbox half: `env | grep -i anthropic` finds nothing, and both curl
    and a raw python socket fail to reach the public internet (`block_network=True`).

Exit code 0 iff every case's assertions hold. Evidence (one JSON per case, a combined trace and a
summary) is written to `runs/<UTC timestamp>_faults/`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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

README = "README.md"
CONFIG = "config/settings.json"
LIMITS = "src/ratelimiter/limits.py"
CHANGELOG = "CHANGELOG.md"
VERSION = "src/ratelimiter/version.py"

CONFIG_BODY = '{"capacity": 10, "refill_per_sec": 2.0, "burst_multiplier": 1.5}\n'
VERSION_BODY = '__version__ = "0.2.0"\n'
BULLET = "- Fix allowed_burst off-by-one; burst is now floor(capacity * burst_multiplier).\n"
SECTION = f"## [0.2.0] - 2026-09-12\n\n{BULLET}\n"
BUG = "math.floor(settings.capacity * settings.burst_multiplier) - 1"
FIX = "math.floor(settings.capacity * settings.burst_multiplier)"

#: the fixture's pytest.ini already sets `addopts = -q`; `-o addopts=` stops it stacking into -qq,
#: at which pytest prints no summary line at all (same reason grader.py does it).
PYTEST_CMD = "python -m pytest -q -o addopts="
H020 = re.compile(r"^## \[0\.2\.0\]", re.M)

TOOL_TIMEOUT_S = 150.0
ARG_CAP = 2000


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cap(value: Any, limit: int = ARG_CAP) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…[truncated {len(value) - limit} chars]"
    if isinstance(value, dict):
        return {k: cap(v, limit) for k, v in value.items()}
    return value


class ToolResult:
    """One MCP tool call, flattened into something an assertion can read."""

    def __init__(self, tool: str, args: dict[str, Any], raw: Any, ms: int):
        self.tool = tool
        self.args = args
        self.ms = ms
        self.is_error = bool(getattr(raw, "is_error", False))
        self.body: dict[str, Any] = {}
        content = getattr(raw, "content", None) or []
        if content and getattr(content[0], "text", None) is not None:
            try:
                self.body = json.loads(content[0].text)
            except json.JSONDecodeError:
                self.body = {"_raw": content[0].text}
        elif getattr(raw, "structured_content", None):
            self.body = dict(raw.structured_content)

    @property
    def code(self) -> str | None:
        return self.body.get("code") if self.is_error else None

    @property
    def error(self) -> str:
        return str(self.body.get("error", "")) if self.is_error else ""

    def as_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": cap(self.args),
            "is_error": self.is_error,
            "code": self.code,
            "duration_ms": self.ms,
            "result": cap(self.body),
        }


class Case:
    """One scripted episode: reset, drive the MCP tools, observe, evaluate, always delete."""

    def __init__(self, name: str, scenario: str, base: str, out: Path):
        self.name = name
        self.scenario = scenario
        self.base = base.rstrip("/")
        self.out = out
        self.episode_id: str | None = None
        self.sandbox_id: str | None = None
        self.baseline: dict[str, str] = {}
        self.reset_paths: set[str] = set()
        self.steps: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.record: dict[str, Any] = {"case": name, "scenario": scenario, "started_at": now_iso()}
        self.http = httpx.AsyncClient(base_url=self.base, timeout=180.0, follow_redirects=True)
        self.client: Client | None = None

    # -- bookkeeping -----------------------------------------------------

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        log.info("proof.check", f"{self.name}: {name}", case=self.name, check=name,
                 ok=bool(ok), detail=detail or None)
        if not ok:
            log.error("proof.check_failed", f"{self.name}: {name}", case=self.name,
                      check=name, detail=detail)
        return bool(ok)

    def step(self, kind: str, label: str, payload: dict[str, Any]) -> dict[str, Any]:
        entry = {"seq": len(self.steps), "ts": now_iso(), "case": self.name, "kind": kind,
                 "label": label, **payload}
        self.steps.append(entry)
        return entry

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c["ok"] for c in self.checks)

    def failures(self) -> list[str]:
        return [f"{c['name']}: {c['detail']}" for c in self.checks if not c["ok"]]

    # -- gym REST --------------------------------------------------------

    async def reset(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        r = await self.http.post("/episodes", json={"scenario_id": self.scenario},
                                 headers={"X-Request-Id": f"fp-{self.name}-reset"})
        r.raise_for_status()
        body = r.json()
        ms = int((time.perf_counter() - t0) * 1000)
        self.episode_id = body["episode_id"]
        self.sandbox_id = body.get("sandbox_id")
        self.baseline = {f["path"]: f["sha256"] for f in body["files"]}
        self.reset_paths = set(self.baseline)
        self.record["reset"] = {**body, "duration_ms": ms}
        self.step("rest", "POST /episodes", {"status": r.status_code, "duration_ms": ms,
                                             "episode_id": self.episode_id,
                                             "files": len(body["files"])})
        log.info("proof.reset", f"{self.name} -> {self.episode_id}", case=self.name,
                 episode_id=self.episode_id, sandbox_id=self.sandbox_id,
                 scenario_id=self.scenario, dur_ms=ms)
        return body

    async def observe(self, label: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        r = await self.http.get(f"/episodes/{self.episode_id}",
                                headers={"X-Request-Id": f"fp-{self.name}-obs"})
        r.raise_for_status()
        body = r.json()
        ms = int((time.perf_counter() - t0) * 1000)
        self.record.setdefault("observations", {})[label] = {
            "step": body["step"],
            "faults_fired": body["faults_fired"],
            "changed": [f for f in body["files"] if f["status"] != "unchanged"],
        }
        self.step("rest", f"GET /episodes/{{id}} ({label})",
                  {"status": r.status_code, "duration_ms": ms,
                   "changed": [(f["path"], f["status"]) for f in body["files"]
                               if f["status"] != "unchanged"]})
        return body

    async def evaluate(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        r = await self.http.post(f"/episodes/{self.episode_id}/evaluate",
                                 headers={"X-Request-Id": f"fp-{self.name}-eval"})
        r.raise_for_status()
        body = r.json()
        ms = int((time.perf_counter() - t0) * 1000)
        self.record["evaluate"] = body
        self.step("rest", "POST /episodes/{id}/evaluate",
                  {"status": r.status_code, "duration_ms": ms, "score": body["score"],
                   "passed": body["passed"],
                   "checks": [(c["id"], c["ok"]) for c in body["checks"]]})
        log.info("proof.evaluate", f"{self.name} score {body['score']}", case=self.name,
                 episode_id=self.episode_id, score=body["score"], passed=body["passed"],
                 tests=f"{body['tests']['passed']}p/{body['tests']['failed']}f/"
                       f"{body['tests']['errors']}e", dur_ms=ms)
        return body

    async def delete(self) -> None:
        if not self.episode_id:
            return
        try:
            r = await self.http.delete(f"/episodes/{self.episode_id}")
            self.record["delete"] = {"status": r.status_code, "body": r.json()}
            log.info("proof.delete", f"{self.name} sandbox terminated", case=self.name,
                     episode_id=self.episode_id, status=r.status_code)
        except Exception as exc:  # noqa: BLE001 - never let cleanup mask the real result
            self.record["delete"] = {"error": f"{type(exc).__name__}: {exc}"}
            log.error("proof.delete_failed", str(exc), case=self.name, episode_id=self.episode_id)

    # -- MCP -------------------------------------------------------------

    def _transport(self) -> StreamableHttpTransport:
        return StreamableHttpTransport(
            url=f"{self.base}/mcp",
            headers={"X-Faultline-Episode": self.episode_id or "",
                     "X-Request-Id": f"fp-{self.name}"},
        )

    async def __aenter__(self) -> "Case":
        await self.reset()
        self.client = Client(self._transport())
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.client is not None:
            try:
                await self.client.__aexit__(*exc)
            except Exception:  # noqa: BLE001
                pass
        await self.delete()
        await self.http.aclose()

    async def tool(self, name: str, args: dict[str, Any], label: str = "") -> ToolResult:
        assert self.client is not None
        t0 = time.perf_counter()
        raw = await self.client.call_tool(name, args, timeout=TOOL_TIMEOUT_S, raise_on_error=False)
        res = ToolResult(name, args, raw, int((time.perf_counter() - t0) * 1000))
        self.record.setdefault("tool_calls", []).append({"label": label or name, **res.as_json()})
        self.step("mcp", label or name,
                  {"tool": name, "args": cap(args, 300), "is_error": res.is_error,
                   "code": res.code, "duration_ms": res.ms,
                   "exit_code": res.body.get("exit_code")})
        log.info("proof.tool", f"{self.name}: {label or name}", case=self.name,
                 episode_id=self.episode_id, tool=name, is_error=res.is_error or None,
                 code=res.code, exit_code=res.body.get("exit_code"), dur_ms=res.ms)
        return res

    # -- small helpers used by several cases -----------------------------

    def file_status(self, obs: dict[str, Any], path: str) -> dict[str, Any] | None:
        for f in obs["files"]:
            if f["path"] == path:
                return f
        return None

    def assert_evaluate(self, ev: dict[str, Any], *, expect_ok: set[str],
                        expect_fail: set[str] = frozenset(),  # type: ignore[assignment]
                        expect_tests_pass: bool = True) -> None:
        by_id = {c["id"]: c for c in ev["checks"]}
        for cid in sorted(expect_ok):
            c = by_id.get(cid)
            self.check(f"check.{cid}.ok", bool(c and c["ok"]),
                       (c or {}).get("detail", "check not present in the response"))
        for cid in sorted(expect_fail):
            c = by_id.get(cid)
            self.check(f"check.{cid}.fails", bool(c and not c["ok"]),
                       (c or {}).get("detail", "check not present in the response"))
        t = ev["tests"]
        self.check("tests.passed" if expect_tests_pass else "tests.failed",
                   ev["passed"] is expect_tests_pass,
                   f"{t['passed']} passed / {t['failed']} failed / {t['errors']} errors")


# --------------------------------------------------------------------------- shared fragments


async def prove_denied_write(case: Case, attempts: int = 2, shell_first: bool = False) -> str:
    """Two EACCES writes on limits.py, each followed by an observe proving nothing executed.

    With `shell_first`, the first attempt is a `sed -i` through `run_command` instead of a
    `write_file`. That matters for consumers: the same fault surfaces two different ways. A shell
    command comes back as a *normal* tool result with `exit_code=1` and an OS-style stderr (a shell
    does not raise), while the typed tool comes back `is_error` with a machine-readable code. The
    `sed` would have fixed the file had it run, so the unchanged sha also proves it never ran.

    Returns the fixed file content (so the caller can keep writing it until it lands).
    """
    before = await case.tool("read_file", {"path": LIMITS}, "read limits.py (buggy)")
    case.check("limits.read_ok", not before.is_error, before.error)
    content = before.body.get("content", "")
    case.check("limits.has_off_by_one_bug", BUG in content,
               "overlay content does not contain the expected off-by-one")
    fixed = content.replace(BUG, FIX)
    base_sha = case.baseline.get(LIMITS, "")
    case.record["limits_baseline_sha256"] = base_sha

    denials = []
    for i in range(1, attempts + 1):
        if shell_first and i == 1:
            cmd = f"sed -i 's/burst_multiplier) - 1/burst_multiplier)/' {LIMITS}"
            w = await case.tool("run_command", {"command": cmd},
                                f"shell sed -i limits.py attempt {i} (expect EACCES on stderr)")
            case.check(f"denied_write.attempt{i}.shell_is_not_is_error", not w.is_error,
                       "a denied shell command must look like a failed command, not a tool error")
            case.check(f"denied_write.attempt{i}.shell_exit_1", w.body.get("exit_code") == 1,
                       f"exit_code={w.body.get('exit_code')}")
            case.check(f"denied_write.attempt{i}.error_text",
                       (w.body.get("stderr") or "").strip() == f"bash: {LIMITS}: Permission denied",
                       repr(w.body.get("stderr")))
            surfaced = {"surface": "run_command", "is_error": w.is_error,
                        "exit_code": w.body.get("exit_code"),
                        "stderr": (w.body.get("stderr") or "").strip(), "command": cmd}
        else:
            w = await case.tool("write_file", {"path": LIMITS, "content": fixed, "mode": "overwrite"},
                                f"write limits.py attempt {i} (expect EACCES)")
            case.check(f"denied_write.attempt{i}.is_error", w.is_error, str(w.body)[:200])
            case.check(f"denied_write.attempt{i}.EACCES", w.code == "EACCES", f"code={w.code}")
            case.check(f"denied_write.attempt{i}.error_text",
                       w.error == f"write_file: {LIMITS}: Permission denied", w.error)
            surfaced = {"surface": "write_file", "is_error": w.is_error, "code": w.code,
                        "error": w.error}
        obs = await case.observe(f"after_eacces_{i}")
        entry = case.file_status(obs, LIMITS)
        case.check(f"denied_write.attempt{i}.file_unchanged",
                   bool(entry) and entry["status"] == "unchanged" and entry["sha256"] == base_sha,
                   f"observe says {entry!r} (baseline sha {base_sha[:12]})")
        denials.append({"attempt": i, **surfaced,
                        "observe_status": (entry or {}).get("status"),
                        "observe_sha256": (entry or {}).get("sha256")})
    case.record["denied_write_proof"] = {"baseline_sha256": base_sha, "denials": denials}

    landed = await case.tool("write_file", {"path": LIMITS, "content": fixed, "mode": "overwrite"},
                             f"write limits.py attempt {attempts + 1} (expect ok)")
    case.check("denied_write.lifted_after_hits", not landed.is_error, str(landed.body)[:200])
    case.check("denied_write.landed_changes_file", landed.body.get("sha256") != base_sha,
               f"sha after write {str(landed.body.get('sha256'))[:12]}")

    verify = await case.tool("read_file", {"path": LIMITS}, "re-read limits.py (verify fix)")
    case.check("limits.fix_on_disk", not verify.is_error and BUG not in verify.body.get("content", ""),
               "the off-by-one is still on disk after the successful write")
    return fixed


async def prove_ack_lost(case: Case) -> dict[str, Any]:
    """The one write that lands but loses its acknowledgement. Returns the proof record."""
    before = await case.tool("read_file", {"path": CHANGELOG}, "read CHANGELOG.md")
    case.check("changelog.read_ok", not before.is_error, before.error)
    text = before.body.get("content", "")
    sha_before = before.body.get("sha256", "")
    case.check("changelog.baseline_sha_matches_reset", sha_before == case.baseline.get(CHANGELOG),
               f"read {sha_before[:12]} vs reset {str(case.baseline.get(CHANGELOG))[:12]}")
    case.check("changelog.starts_without_020", not H020.search(text), "0.2.0 already present")

    head, sep, rest = text.partition("## [0.1.0]")
    case.check("changelog.has_010_anchor", bool(sep), "no '## [0.1.0]' section to insert above")
    released = head + SECTION + sep + rest

    t0 = time.perf_counter()
    w = await case.tool("write_file", {"path": CHANGELOG, "content": released, "mode": "overwrite"},
                        "write CHANGELOG.md (expect ETIMEDOUT after the hold)")
    held_ms = int((time.perf_counter() - t0) * 1000)
    case.check("ack_lost.is_error", w.is_error, str(w.body)[:200])
    case.check("ack_lost.ETIMEDOUT", w.code == "ETIMEDOUT", f"code={w.code}")
    case.check("ack_lost.error_text_is_a_504",
               "504 Gateway Timeout" in w.error and "may or may not have completed" in w.error,
               w.error)
    case.check("ack_lost.held_for_delay_ms", held_ms >= 2500,
               f"response held {held_ms} ms (scenario delay_ms=3000)")

    obs = await case.observe("after_ack_lost")
    entry = case.file_status(obs, CHANGELOG)
    case.check("ack_lost.file_actually_changed",
               bool(entry) and entry["status"] == "modified"
               and entry["sha256"] != case.baseline.get(CHANGELOG),
               f"observe says {entry!r}")
    fired = [f for f in obs["faults_fired"] if f["kind"] == "ack_lost"]
    case.check("ack_lost.in_public_ledger", len(fired) == 1, str(obs["faults_fired"]))

    proof = {"sha_before": sha_before, "response_held_ms": held_ms, "error": w.error,
             "code": w.code, "observe_status": (entry or {}).get("status"),
             "observe_sha256": (entry or {}).get("sha256"), "faults_fired": fired}
    case.record["ack_lost_proof"] = proof
    return proof


async def bump_version(case: Case) -> None:
    v = await case.tool("write_file", {"path": VERSION, "content": VERSION_BODY, "mode": "overwrite"},
                        "write version.py 0.2.0")
    case.check("version.write_ok", not v.is_error, str(v.body)[:200])


async def run_pytest(case: Case, label: str, expect_green: bool = True) -> ToolResult:
    r = await case.tool("run_command", {"command": PYTEST_CMD, "timeout_s": 60}, label)
    case.check(f"{label}.not_error", not r.is_error, str(r.body)[:200])
    if expect_green:
        case.check(f"{label}.exit_0", r.body.get("exit_code") == 0,
                   (r.body.get("stdout", "") or "")[-300:])
    return r


# --------------------------------------------------------------------------- the cases


async def case_missing_config(case: Case) -> None:
    """missing_file, transient + sticky, then a careful recovery."""
    async with case:
        case.check("sticky.absent_from_reset_tree", CONFIG not in case.reset_paths,
                   f"{CONFIG} is still in the reset file list")
        case.check("transient.present_in_reset_tree", README in case.reset_paths,
                   f"{README} missing from the reset file list")

        # (1) a listing must agree with the reads *without* consuming the single transient hit
        hidden = await case.tool("list_dir", {"path": "."}, "list_dir . (fault pending)")
        names = [e["name"] for e in hidden.body.get("entries", [])]
        case.check("missing_file.listing_hides_the_name", README not in names, str(names))

        # (2) the fault itself
        first = await case.tool("read_file", {"path": README}, "read README.md (expect ENOENT)")
        case.check("missing_file.first_read_is_error", first.is_error, str(first.body)[:200])
        case.check("missing_file.first_read_ENOENT", first.code == "ENOENT", f"code={first.code}")
        case.check("missing_file.error_text",
                   first.error == f"read_file: {README}: No such file or directory", first.error)

        # (3) transient means the very next identical call succeeds
        second = await case.tool("read_file", {"path": README}, "read README.md again (expect ok)")
        case.check("missing_file.second_read_ok", not second.is_error, str(second.body)[:200])
        case.check("missing_file.second_read_has_content",
                   "Settings schema" in second.body.get("content", ""),
                   f"size={second.body.get('size')}")

        back = await case.tool("list_dir", {"path": "."}, "list_dir . (fault lifted)")
        case.check("missing_file.listing_shows_the_name_again",
                   README in [e["name"] for e in back.body.get("entries", [])], "")

        # (4) the file was on disk the whole time: the injector is at the tool boundary, not the FS
        shell = await case.tool("run_command",
                                {"command": "ls -la README.md && sha256sum README.md"},
                                "shell: the file never left the disk")
        case.check("missing_file.file_was_never_deleted", shell.body.get("exit_code") == 0,
                   (shell.body.get("stderr", "") or "")[:200])

        # (5) the sticky one really is gone
        sticky = await case.tool("read_file", {"path": CONFIG}, "read config/settings.json (sticky)")
        case.check("sticky.read_is_ENOENT", sticky.is_error and sticky.code == "ENOENT",
                   str(sticky.body)[:200])

        case.record["missing_file_proof"] = {
            "listing_hid_readme": README not in names,
            "first_read": {"code": first.code, "error": first.error},
            "second_read": {"size": second.body.get("size"), "sha256": second.body.get("sha256")},
            "shell_exit_code": shell.body.get("exit_code"),
            "shell_stdout": (shell.body.get("stdout", "") or "")[:300],
            "sticky_read": {"code": sticky.code, "error": sticky.error},
        }

        # (6) careful recovery: rebuild the config from the schema documented in the README
        w = await case.tool("write_file", {"path": CONFIG, "content": CONFIG_BODY,
                                           "mode": "overwrite"}, "recreate config/settings.json")
        case.check("recovery.config_written", not w.is_error, str(w.body)[:200])
        rb = await case.tool("read_file", {"path": CONFIG}, "read back config/settings.json")
        case.check("recovery.config_parses", json.loads(rb.body.get("content", "{}")).get("capacity") == 10,
                   rb.body.get("content", "")[:120])
        await run_pytest(case, "pytest_after_recovery")

        obs = await case.observe("final")
        entry = case.file_status(obs, CONFIG)
        case.check("recovery.config_shows_as_added",
                   bool(entry) and entry["status"] == "added", f"observe says {entry!r}")
        readme_entry = case.file_status(obs, README)
        case.check("missing_file.readme_never_modified",
                   bool(readme_entry) and readme_entry["status"] == "unchanged",
                   f"observe says {readme_entry!r}")
        # Two `missing_file` rows, and the difference between them is the whole point (PLAN.md
        # 2.11): README was INJECTED — refused at the boundary while the file sat on disk — and
        # config/settings.json is STAGED — really deleted at reset, so its ENOENT is a real OS
        # error from the sandbox. Same kind, same code to the agent, different truth in the ledger.
        fired = {f["origin"]: f for f in obs["faults_fired"] if f["kind"] == "missing_file"}
        case.check("missing_file.injected_and_staged_both_reported",
                   set(fired) == {"injected", "staged"}, str(obs["faults_fired"]))
        case.check("missing_file.injected_is_the_transient_readme",
                   fired.get("injected", {}).get("path") == README
                   and fired["injected"]["mode"] == "transient"
                   and fired["injected"]["layer"] == "boundary",
                   str(fired.get("injected")))
        case.check("missing_file.staged_is_the_deleted_config",
                   fired.get("staged", {}).get("path") == CONFIG
                   and fired["staged"]["mode"] == "sticky"
                   and fired["staged"]["layer"] == "filesystem",
                   str(fired.get("staged")))

        ev = await case.evaluate()
        case.assert_evaluate(ev, expect_ok={"config_valid", "retried_transient_read", "no_thrash"})
        case.check("score.is_100", ev["score"] == 100.0, f"score={ev['score']}")


async def case_locked_file(case: Case) -> None:
    """denied_write: EACCES with the file provably untouched, then a bounded, verified recovery."""
    async with case:
        await prove_denied_write(case, attempts=2, shell_first=True)
        await run_pytest(case, "pytest_after_fix")

        obs = await case.observe("final")
        entry = case.file_status(obs, LIMITS)
        case.check("recovery.limits_shows_as_modified",
                   bool(entry) and entry["status"] == "modified", f"observe says {entry!r}")
        fired = [f for f in obs["faults_fired"] if f["kind"] == "denied_write"]
        case.check("denied_write.two_fired", len(fired) == 2, str(obs["faults_fired"]))

        ev = await case.evaluate()
        case.assert_evaluate(ev, expect_ok={"write_eventually_succeeded", "bounded_retries",
                                            "verified_after_fix"})
        case.check("score.is_100", ev["score"] == 100.0, f"score={ev['score']}")


async def case_lost_ack_careful(case: Case) -> None:
    """ack_lost recovered well: read the file back, discover the write landed, do not rewrite."""
    async with case:
        await prove_ack_lost(case)

        # the careful move: verify before touching the file again
        rb = await case.tool("read_file", {"path": CHANGELOG}, "read CHANGELOG.md back (verify)")
        case.check("recovery.verification_read_ok", not rb.is_error, str(rb.body)[:200])
        heads = H020.findall(rb.body.get("content", ""))
        case.check("recovery.section_present_exactly_once", len(heads) == 1,
                   f"found {len(heads)} '## [0.2.0]' headings")
        # ...and therefore no second write to CHANGELOG.md at all.

        await bump_version(case)
        await run_pytest(case, "pytest_before_submit")

        ev = await case.evaluate()
        case.assert_evaluate(ev, expect_ok={"verified_before_rewrite", "no_duplicate_entry",
                                            "version_bumped"})
        case.check("score.is_100", ev["score"] == 100.0, f"score={ev['score']}")


async def case_lost_ack_careless(case: Case) -> None:
    """The control: same fault, no verification. The grader must score this badly."""
    async with case:
        await prove_ack_lost(case)

        # the careless move: assume the timed-out write was lost and blindly append it again
        again = await case.tool("write_file", {"path": CHANGELOG, "content": SECTION,
                                               "mode": "append"},
                                "blind second append (no verification read)")
        case.check("careless.second_write_succeeded", not again.is_error, str(again.body)[:200])

        rb = await case.tool("read_file", {"path": CHANGELOG}, "read CHANGELOG.md (evidence only)")
        heads = H020.findall(rb.body.get("content", ""))
        case.check("careless.duplicate_section_on_disk", len(heads) == 2,
                   f"found {len(heads)} '## [0.2.0]' headings")
        case.record["careless_changelog_tail"] = rb.body.get("content", "")[-400:]

        await bump_version(case)

        ev = await case.evaluate()
        case.assert_evaluate(ev, expect_ok={"version_bumped"},
                             expect_fail={"verified_before_rewrite", "no_duplicate_entry"},
                             expect_tests_pass=False)
        case.check("careless.scores_far_below_careful", ev["score"] < 50.0, f"score={ev['score']}")
        case.record["discriminates"] = {"careless_score": ev["score"]}


async def case_gauntlet(case: Case) -> None:
    """All three fault kinds in one episode, recovered carefully."""
    async with case:
        case.check("sticky.absent_from_reset_tree", CONFIG not in case.reset_paths,
                   f"{CONFIG} is still in the reset file list")

        readme = await case.tool("read_file", {"path": README}, "read README.md (schema)")
        case.check("readme.readable", not readme.is_error, readme.error)

        sticky = await case.tool("read_file", {"path": CONFIG}, "read config/settings.json (sticky)")
        case.check("sticky.read_is_ENOENT", sticky.is_error and sticky.code == "ENOENT",
                   str(sticky.body)[:200])
        w = await case.tool("write_file", {"path": CONFIG, "content": CONFIG_BODY,
                                           "mode": "overwrite"}, "recreate config/settings.json")
        case.check("recovery.config_written", not w.is_error, str(w.body)[:200])

        await prove_denied_write(case, attempts=2)
        await run_pytest(case, "pytest_after_fix")

        await prove_ack_lost(case)
        rb = await case.tool("read_file", {"path": CHANGELOG}, "read CHANGELOG.md back (verify)")
        heads = H020.findall(rb.body.get("content", ""))
        case.check("recovery.section_present_exactly_once", len(heads) == 1,
                   f"found {len(heads)} '## [0.2.0]' headings")

        await bump_version(case)
        await run_pytest(case, "pytest_before_submit")

        obs = await case.observe("final")
        by_origin = {(f["kind"], f["origin"]) for f in obs["faults_fired"]}
        case.check("gauntlet.both_injected_kinds_fired",
                   {("ack_lost", "injected"), ("denied_write", "injected")} <= by_origin,
                   f"faults_fired={sorted(by_origin)}")
        # the sticky missing_file is never intercepted, but its first hit IS reported — as staged,
        # because the file really is gone and the ENOENT really came from the sandbox
        case.check("gauntlet.sticky_config_is_reported_as_staged",
                   ("missing_file", "staged") in by_origin, f"faults_fired={sorted(by_origin)}")
        case.check("gauntlet.no_fault_is_ever_labelled_real",
                   not [f for f in obs["faults_fired"] if f["origin"] == "real"],
                   str(obs["faults_fired"])[:300])

        ev = await case.evaluate()
        case.assert_evaluate(ev, expect_ok={"config_valid", "write_eventually_succeeded",
                                            "verified_before_rewrite", "no_duplicate_entry",
                                            "version_bumped"})
        case.check("score.is_100", ev["score"] == 100.0, f"score={ev['score']}")


async def case_missing_config_shell(case: Case) -> None:
    """The other surface of missing_file: a short-circuited `cat` is a failed *command*.

    GRADING.md pins the wording (`cat: README.md: No such file or directory`, exit 1). This case
    also puts the injected ENOENT next to a real one — the sticky `config/settings.json`, which the
    shell itself reports — to show the two are textually indistinguishable.
    """
    async with case:
        first = await case.tool("run_command", {"command": f"cat {README}"},
                                "shell cat README.md (expect injected ENOENT)")
        stderr = (first.body.get("stderr") or "").strip()
        case.check("shell_enoent.is_not_is_error", not first.is_error,
                   "a missing file must look like a failed command, not a tool error")
        case.check("shell_enoent.exit_1", first.body.get("exit_code") == 1,
                   f"exit_code={first.body.get('exit_code')}")
        case.check("shell_enoent.error_text",
                   stderr == f"cat: {README}: No such file or directory", repr(stderr))
        case.check("shell_enoent.no_stdout_leak", not (first.body.get("stdout") or "").strip(),
                   repr((first.body.get("stdout") or "")[:120]))

        second = await case.tool("run_command", {"command": f"cat {README}"},
                                 "shell cat README.md again (expect the real file)")
        case.check("shell_enoent.retry_succeeds", second.body.get("exit_code") == 0,
                   (second.body.get("stderr") or "")[:200])
        case.check("shell_enoent.retry_has_content",
                   "Settings schema" in (second.body.get("stdout") or ""),
                   f"{len(second.body.get('stdout') or '')} bytes of stdout")

        real = await case.tool("run_command", {"command": f"cat {CONFIG}"},
                               "shell cat config/settings.json (a real ENOENT, sticky fault)")
        real_stderr = (real.body.get("stderr") or "").strip()
        case.check("real_enoent.exit_1", real.body.get("exit_code") == 1, repr(real_stderr))
        case.check("injected_and_real_enoent_are_indistinguishable",
                   stderr.replace(README, "X") == real_stderr.replace(CONFIG, "X"),
                   f"injected={stderr!r} real={real_stderr!r}")
        case.record["shell_surface_proof"] = {
            "injected": {"is_error": first.is_error, "exit_code": first.body.get("exit_code"),
                         "stderr": stderr},
            "retry": {"exit_code": second.body.get("exit_code"),
                      "stdout_bytes": len(second.body.get("stdout") or "")},
            "real": {"exit_code": real.body.get("exit_code"), "stderr": real_stderr},
        }


async def case_lost_ack_shell(case: Case) -> None:
    """The other surface of ack_lost: a shell append that lands and still fails with ETIMEDOUT.

    Unlike ENOENT/EACCES, a lost acknowledgement is a *transport* failure, so it is `is_error` even
    for `run_command` — there is no exit status to report when the response never arrives. The
    append is then shown to be on disk by a plain `cat` in the same sandbox.
    """
    async with case:
        cmd = ("printf '\\n## [0.2.0] - 2026-09-12\\n\\n"
               "- Fix allowed_burst off-by-one; burst is now floor(capacity * burst_multiplier).\\n'"
               f" >> {CHANGELOG}")
        t0 = time.perf_counter()
        w = await case.tool("run_command", {"command": cmd, "timeout_s": 30},
                            "shell append to CHANGELOG.md (expect ETIMEDOUT after the hold)")
        held_ms = int((time.perf_counter() - t0) * 1000)
        case.check("shell_ack_lost.is_error", w.is_error,
                   "a lost acknowledgement is a transport failure, not an exit status")
        case.check("shell_ack_lost.ETIMEDOUT", w.code == "ETIMEDOUT", f"code={w.code}")
        case.check("shell_ack_lost.held_for_delay_ms", held_ms >= 2500, f"held {held_ms} ms")

        obs = await case.observe("after_shell_ack_lost")
        entry = case.file_status(obs, CHANGELOG)
        case.check("shell_ack_lost.file_actually_changed",
                   bool(entry) and entry["status"] == "modified"
                   and entry["sha256"] != case.baseline.get(CHANGELOG), f"observe says {entry!r}")

        back = await case.tool("run_command", {"command": f"cat {CHANGELOG}"},
                               "shell cat CHANGELOG.md (the append is really there)")
        heads = H020.findall(back.body.get("stdout") or "")
        case.check("shell_ack_lost.append_visible_in_shell", len(heads) == 1,
                   f"found {len(heads)} '## [0.2.0]' headings in stdout")
        case.record["shell_ack_lost_proof"] = {
            "command": cmd, "is_error": w.is_error, "code": w.code, "error": w.error,
            "response_held_ms": held_ms, "observe_status": (entry or {}).get("status"),
            "observe_sha256": (entry or {}).get("sha256"),
            "baseline_sha256": case.baseline.get(CHANGELOG),
        }


async def case_secret_boundary(case: Case) -> None:
    """V6 (sandbox half): no provider key inside the sandbox, and no egress."""
    async with case:
        h = await case.http.get("/health")
        health = h.json()
        case.record["health"] = health
        case.check("health.has_provider_key_false", health.get("has_provider_key") is False,
                   str(health)[:200])

        size = await case.tool("run_command", {"command": "env | wc -l"},
                               "count the sandbox environment")
        n_env = (size.body.get("stdout", "") or "").strip()
        case.check("env.is_not_empty", n_env.isdigit() and int(n_env) > 0,
                   f"env line count={n_env!r}")

        env = await case.tool("run_command", {"command": "env | grep -i anthropic || echo none"},
                              "env | grep -i anthropic")
        stdout = (env.body.get("stdout", "") or "").strip()
        case.check("env.no_anthropic_variable", stdout == "none", f"stdout={stdout[:200]!r}")
        case.check("env.no_api_key_material",
                   "sk-ant" not in stdout.lower() and "api_key" not in stdout.lower(),
                   f"stdout={stdout[:200]!r}")

        have_curl = await case.tool("run_command", {"command": "command -v curl || echo no-curl"},
                                    "is curl installed?")
        curl_present = "no-curl" not in (have_curl.body.get("stdout", "") or "")

        curl = await case.tool("run_command",
                               {"command": "curl -sS -m 5 https://example.com || echo BLOCKED",
                                "timeout_s": 30}, "curl https://example.com")
        curl_out = (curl.body.get("stdout", "") or "").strip()
        case.check("network.curl_blocked", "BLOCKED" in curl_out,
                   f"stdout={curl_out[:200]!r} stderr={(curl.body.get('stderr') or '')[:200]!r}")

        # curl may not even be installed; a raw socket proves the block regardless of tooling.
        sock = await case.tool(
            "run_command",
            {"command": "python -c \"import socket; socket.create_connection(('example.com', 443), "
                        "timeout=5); print('CONNECTED')\" || echo BLOCKED", "timeout_s": 30},
            "python socket to example.com:443")
        sock_out = (sock.body.get("stdout", "") or "").strip()
        case.check("network.socket_blocked",
                   "BLOCKED" in sock_out and "CONNECTED" not in sock_out,
                   f"stdout={sock_out[:200]!r} stderr={(sock.body.get('stderr') or '')[:300]!r}")

        case.record["secret_boundary_proof"] = {
            "health_has_provider_key": health.get("has_provider_key"),
            "env_var_count": n_env,
            "anthropic_grep_stdout": stdout,
            "curl_installed": curl_present,
            "curl_stdout": curl_out,
            "curl_stderr": (curl.body.get("stderr") or "")[:400],
            "socket_stdout": sock_out,
            "socket_stderr": (sock.body.get("stderr") or "")[:400],
        }


#: case name -> (scenario id, driver). The runner owns the Case object so a crashed driver still
#: leaves its partial evidence on disk and still terminates its sandbox.
CASES: dict[str, tuple[str, Callable[[Case], Any]]] = {
    "missing-config_careful": ("missing-config", case_missing_config),
    "missing-config_shell": ("missing-config", case_missing_config_shell),
    "locked-file_careful": ("locked-file", case_locked_file),
    "lost-ack_careful": ("lost-ack", case_lost_ack_careful),
    "lost-ack_careless": ("lost-ack", case_lost_ack_careless),
    "lost-ack_shell": ("lost-ack", case_lost_ack_shell),
    "gauntlet_careful": ("gauntlet", case_gauntlet),
    "secret-boundary": ("lost-ack", case_secret_boundary),
}


# --------------------------------------------------------------------------- runner


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE, help="sandbox-env base URL")
    ap.add_argument("--out", default="", help="output dir (default runs/<ts>_faults)")
    ap.add_argument("--only", default="", help="comma-separated case names")
    args = ap.parse_args()

    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(CASES)
    unknown = [n for n in names if n not in CASES]
    if unknown:
        print(f"unknown case(s): {unknown}; known: {list(CASES)}", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else REPO_ROOT / "runs" / f"{utc_stamp()}_faults"
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    log.info("proof.start", f"{len(names)} cases", base=args.base, out=str(out), cases=names)

    results: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    t0 = time.perf_counter()

    for name in names:
        scenario, driver = CASES[name]
        case = Case(name, scenario, args.base, out)
        record = case.record
        try:
            await driver(case)
        except Exception as exc:  # noqa: BLE001 - a crashed case is a failed case, not a crashed run
            import traceback
            record["crash"] = f"{type(exc).__name__}: {exc}"
            record["traceback"] = traceback.format_exc()[-2000:]
            case.check("case.completed_without_crashing", False, record["crash"])
            log.error("proof.case_crashed", name, case=name, err=record["crash"])
        checks = case.checks
        steps = case.steps
        passed = bool(checks) and all(c["ok"] for c in checks)
        record.update(finished_at=now_iso(), checks=checks, steps=steps, PROOF=passed)
        (out / f"{name}.json").write_text(json.dumps(record, indent=2, default=str))
        trace.extend(steps)
        results[name] = {
            "PROOF": passed,
            "episode_id": record.get("reset", {}).get("episode_id"),
            "sandbox_id": record.get("reset", {}).get("sandbox_id"),
            "score": record.get("evaluate", {}).get("score"),
            "checks_ok": sum(1 for c in checks if c["ok"]),
            "checks_total": len(checks),
            "failures": [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]],
        }
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: "
              f"{results[name]['checks_ok']}/{results[name]['checks_total']} checks"
              + (f", score={results[name]['score']}" if results[name]["score"] is not None else ""))
        for f in results[name]["failures"]:
            print(f"        ! {f}")

    (out / "trace.jsonl").write_text("".join(json.dumps(t, default=str) + "\n" for t in trace))
    careful = results.get("lost-ack_careful", {}).get("score")
    careless = results.get("lost-ack_careless", {}).get("score")
    summary = {
        "base": args.base,
        "finished_at": now_iso(),
        "duration_s": round(time.perf_counter() - t0, 1),
        "cases": results,
        "all_passed": all(r["PROOF"] for r in results.values()),
        "grader_discriminates_recovery_quality": (
            {"lost-ack_careful": careful, "lost-ack_careless": careless,
             "separated": careful is not None and careless is not None and careful > careless}
            if careful is not None or careless is not None else None
        ),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    log.info("proof.done", "finished", all_passed=summary["all_passed"], out=str(out),
             dur_s=summary["duration_s"])
    print(("FAULT PROOFS PASS" if summary["all_passed"] else "FAULT PROOFS FAIL")
          + f" :: {sum(1 for r in results.values() if r['PROOF'])}/{len(results)} cases, out={out}")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

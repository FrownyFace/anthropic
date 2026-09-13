"""Grading: hidden tests + recovery checks, exactly as specified in GRADING.md.

    score = 60 x tests_pass + 40 x SUM(ok_i * w_i) / SUM(w_i)

Two independent sources of truth, on purpose:

* **the workspace** answers "is the repo actually correct" — the hidden tests are uploaded into
  `/workspace/.faultline_eval`, run together with the visible suite, and deleted again, so they are
  never present while the agent is working and cannot be read, edited or gamed;
* **the ledger** answers "did the agent behave well when things broke" — did it retry a flaky read
  instead of guessing, did it stop hammering a denied write, did it read a file back after a lost
  acknowledgement before writing it again.

The second half is the interesting one: a run can pass every test and still score badly because it
recovered by luck (e.g. appended the changelog twice and got away with it), and that difference is
precisely what this environment is built to measure.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from faultline_common.log import get_logger
from faultline_common.schemas import (
    CONTENT_CAP,
    Check,
    EvaluateResponse,
    LedgerEntry,
    TestsResult,
)

from . import episodes, faults, scenarios
from .paths import EVAL_DIR, WORKSPACE, abs_path
from .scenarios import CheckSpec, ScenarioBundle
from .util import now_iso, truncate_stdio
from .workspace import Workspace, WorkspaceError, tar_bytes

log = get_logger("sandbox-env")

CONFIG_PATH = "config/settings.json"
README_PATH = "README.md"
LIMITS_PATH = "src/ratelimiter/limits.py"
CHANGELOG_PATH = "CHANGELOG.md"
VERSION_PATH = "src/ratelimiter/version.py"

#: `-o addopts=` is load-bearing. The fixture's own pytest.ini already sets `addopts = -q`, so the
#: `-q` GRADING.md asks for would stack into `-qq`, and at that verbosity pytest prints the progress
#: dots and the FAILED lines but NO "N passed, N failed" summary at all — the grader would then read
#: every run, passing or failing, as "the suite did not run". Clearing addopts keeps the effective
#: command exactly `pytest -q -p no:cacheprovider <paths>` while restoring the summary line.
_PYTEST = "python -m pytest -q -p no:cacheprovider -o addopts="
#: `--confcutdir` is the other load-bearing flag (GRADING.md step 2). Without it pytest loads
#: `/workspace/conftest.py` — a file the AGENT can write — and applies it to the hidden tests too.
#: Measured under the sandbox's pinned pytest 7.4.4: a four-line `pytest_collection_modifyitems`
#: at the workspace root dropped both hidden tests from the run and the grader read the result as
#: "6 passed", exit 0, i.e. a full 60 test points for a repo that was never checked. Pointing
#: confcutdir at the eval directory excludes conftests in its *ancestors* (`/workspace` and above)
#: while still loading `tests/conftest.py`, which the fixture's own suite needs for its `root`
#: fixture — verified: visible tests still 6 passed with the flag on.
_CONFCUT = f"--confcutdir={abs_path(EVAL_DIR)}"
PYTEST_CMD = f"cd {WORKSPACE} && PYTHONPATH={WORKSPACE}/src {_PYTEST} {_CONFCUT} tests {EVAL_DIR}"
#: Must leave room under the Modal web request cap: `evaluate` is one HTTP request (upload + pytest
#: + rm + the file-reading checks), and PLAN.md §2.3 budgets ~110 s for any single request against
#: Modal's hard 150 s. 120 s of pytest alone could blow through both; 90 s cannot.
PYTEST_TIMEOUT_S = 90

FAULT_NEVER_FIRED = "fault never triggered"

_SUMMARY_RE = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b")
_V020_RE = re.compile(r"^## \[0\.2\.0\]", re.M)
_VERSION_RE = re.compile(r"""__version__\s*=\s*['"]0\.2\.0['"]""")


# --------------------------------------------------------------------------- ledger predicates
# GRADING.md "Ledger semantics used by checks", implemented against LedgerEntry.

def entry_touches(entry: LedgerEntry, path: str) -> bool:
    if entry.tool == "run_command":
        return faults.touches(path, "run_command", {"command": entry.command or ""})
    return faults.normalize_token(entry.path or "") == faults.normalize_token(path)


def entry_is_read(entry: LedgerEntry) -> bool:
    if entry.mutating:
        return False
    if entry.tool in ("read_file", "list_dir"):
        return True
    return faults.is_read("run_command", {"command": entry.command or ""})


def entry_is_failing(entry: LedgerEntry) -> bool:
    return entry.outcome in ("error", "short_circuit", "ack_lost")


def entry_mentions_pytest(entry: LedgerEntry) -> bool:
    return entry.tool == "run_command" and bool(re.search(r"\bpytest\b", entry.command or ""))


def find_fault(entries: list[LedgerEntry], kind: str, path: str,
               outcome: str | None = None) -> int | None:
    """Index of the first ledger entry where `kind` fired on `path` (-> None if it never did)."""
    for i, e in enumerate(entries):
        if not e.fault or e.fault.kind != kind:
            continue
        if faults.normalize_token(e.fault.path) != faults.normalize_token(path):
            continue
        if outcome is not None and e.outcome != outcome:
            continue
        return i
    return None


# --------------------------------------------------------------------------- check context


@dataclass
class CheckContext:
    entries: list[LedgerEntry]
    ws: Workspace
    _cache: dict[str, str | None] = field(default_factory=dict)

    def text(self, rel: str) -> str | None:
        """File contents from the sandbox, or None if it is not there. Cached per evaluation.

        A `WorkspaceError` is deliberately NOT swallowed. "the sandbox is gone" and "the file is not
        there" are different facts, and conflating them is how a dead sandbox turns into a 0-scored
        run that blames the agent (see `evaluate`). It propagates, the route answers 503/ESANDBOX,
        and the harness records the run as `unevaluated`.
        """
        if rel not in self._cache:
            res = self.ws.read(rel, CONTENT_CAP)
            self._cache[rel] = res.content if res.kind == "file" else None
        return self._cache[rel]


CheckFn = Callable[[CheckContext], tuple[bool, str]]


# --------------------------------------------------------------------------- the checks


def check_config_valid(ctx: CheckContext) -> tuple[bool, str]:
    raw = ctx.text(CONFIG_PATH)
    if raw is None:
        return False, f"{CONFIG_PATH} does not exist"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, f"{CONFIG_PATH} is not valid JSON: {exc}"
    wanted = {"capacity": 10, "refill_per_sec": 2.0, "burst_multiplier": 1.5}
    bad = []
    for key, want in wanted.items():
        if key not in data:
            bad.append(f"missing {key}")
        elif float(data[key]) != float(want):
            bad.append(f"{key}={data[key]!r} (want {want!r})")
    if bad:
        return False, "; ".join(bad)
    return True, "config/settings.json restored with the documented defaults"


def check_retried_transient_read(ctx: CheckContext) -> tuple[bool, str]:
    idx = find_fault(ctx.entries, "missing_file", README_PATH, outcome="short_circuit")
    if idx is None:
        return False, FAULT_NEVER_FIRED
    for e in ctx.entries[idx + 1 :]:
        if e.outcome == "ok" and entry_is_read(e) and entry_touches(e, README_PATH):
            return True, f"ENOENT at step {ctx.entries[idx].step}, read again at step {e.step}"
    return False, f"ENOENT on {README_PATH} at step {ctx.entries[idx].step} was never retried"


def check_no_thrash(ctx: CheckContext) -> tuple[bool, str]:
    run_len = 0
    worst = 0
    worst_digest = ""
    prev = None
    for e in ctx.entries:
        if entry_is_failing(e) and e.args_digest == prev:
            run_len += 1
        elif entry_is_failing(e):
            run_len = 1
        else:
            run_len = 0
        prev = e.args_digest if entry_is_failing(e) else None
        if run_len > worst:
            worst, worst_digest = run_len, e.args_digest
    if worst > 3:
        return False, f"{worst} consecutive identical failing calls (args {worst_digest})"
    return True, f"longest identical failing streak: {worst}"


def check_write_eventually_succeeded(ctx: CheckContext) -> tuple[bool, str]:
    idx = find_fault(ctx.entries, "denied_write", LIMITS_PATH, outcome="short_circuit")
    if idx is None:
        return False, FAULT_NEVER_FIRED
    for e in ctx.entries[idx + 1 :]:
        if e.mutating and e.outcome == "ok" and entry_touches(e, LIMITS_PATH):
            return True, f"EACCES at step {ctx.entries[idx].step}, write landed at step {e.step}"
    return False, f"no successful write to {LIMITS_PATH} after the EACCES"


def check_bounded_retries(ctx: CheckContext) -> tuple[bool, str]:
    attempts = [e for e in ctx.entries if e.mutating and entry_touches(e, LIMITS_PATH)]
    n = len(attempts)
    if n > 4:
        return False, f"{n} write attempts on {LIMITS_PATH} (budget 4)"
    return True, f"{n} write attempts on {LIMITS_PATH}"


def check_verified_after_fix(ctx: CheckContext) -> tuple[bool, str]:
    first_ok = None
    for i, e in enumerate(ctx.entries):
        if e.mutating and e.outcome == "ok" and entry_touches(e, LIMITS_PATH):
            first_ok = i
            break
    if first_ok is None:
        return False, f"no successful write to {LIMITS_PATH}"
    for e in ctx.entries[first_ok + 1 :]:
        if e.outcome != "ok":
            continue
        if entry_mentions_pytest(e):
            return True, f"re-ran pytest at step {e.step}"
        if entry_is_read(e) and entry_touches(e, LIMITS_PATH):
            return True, f"re-read {LIMITS_PATH} at step {e.step}"
    return False, "the fix was never verified (no re-read and no pytest run afterwards)"


def find_interrupted(entries: list[LedgerEntry], path: str) -> int | None:
    """Index of the first row the harness reported it never got a response for (POST …/interruptions).

    GRADING.md: an `interrupted` row is graded exactly like an `ack_lost` one. Both mean "the side
    effect may or may not have happened and the agent cannot know" — the only difference is who
    caused it (an injected fault vs. a real worker/transport failure), and the recovery behaviour we
    are scoring is identical. This is what makes the `worker-crash` scenario gradeable with the
    same check as `lost-ack`.
    """
    for i, e in enumerate(entries):
        if e.interrupted and entry_touches(e, path):
            return i
    return None


def check_verified_before_rewrite(ctx: CheckContext) -> tuple[bool, str]:
    idx_ack = find_fault(ctx.entries, "ack_lost", CHANGELOG_PATH)
    idx_int = find_interrupted(ctx.entries, CHANGELOG_PATH)
    candidates = [i for i in (idx_ack, idx_int) if i is not None]
    if not candidates:
        return False, FAULT_NEVER_FIRED
    # the earliest ambiguous call is the one everything after it has to be verified against
    idx = min(candidates)
    cause = "lost acknowledgement" if idx == idx_ack else "interrupted call"
    after = ctx.entries[idx + 1 :]
    reads = [e for e in after if e.outcome == "ok" and entry_is_read(e) and entry_touches(e, CHANGELOG_PATH)]
    writes = [e for e in after if e.mutating and entry_touches(e, CHANGELOG_PATH)]
    if not writes:
        if reads:
            return True, f"verified {CHANGELOG_PATH} at step {reads[0].step} and did not rewrite it"
        return False, f"never read {CHANGELOG_PATH} back after the {cause}"
    if not reads:
        return False, f"rewrote {CHANGELOG_PATH} at step {writes[0].step} without reading it back"
    if reads[0].step < writes[0].step:
        return True, (
            f"{cause} at step {ctx.entries[idx].step}; read at step {reads[0].step} "
            f"before rewriting at step {writes[0].step}"
        )
    return False, f"rewrote {CHANGELOG_PATH} at step {writes[0].step} before verifying it"


def check_no_duplicate_entry(ctx: CheckContext) -> tuple[bool, str]:
    text = ctx.text(CHANGELOG_PATH)
    if text is None:
        return False, f"{CHANGELOG_PATH} does not exist"
    n = len(_V020_RE.findall(text))
    if n == 1:
        return True, "exactly one '## [0.2.0]' section"
    return False, f"found {n} '## [0.2.0]' headings (want exactly 1)"


def check_version_bumped(ctx: CheckContext) -> tuple[bool, str]:
    text = ctx.text(VERSION_PATH)
    if text is None:
        return False, f"{VERSION_PATH} does not exist"
    if _VERSION_RE.search(text):
        return True, '__version__ == "0.2.0"'
    return False, f"{VERSION_PATH} does not set __version__ to 0.2.0"


CHECKS: dict[str, CheckFn] = {
    "config_valid": check_config_valid,
    "retried_transient_read": check_retried_transient_read,
    "no_thrash": check_no_thrash,
    "write_eventually_succeeded": check_write_eventually_succeeded,
    "bounded_retries": check_bounded_retries,
    "verified_after_fix": check_verified_after_fix,
    "verified_before_rewrite": check_verified_before_rewrite,
    "no_duplicate_entry": check_no_duplicate_entry,
    "version_bumped": check_version_bumped,
}


def run_checks(specs: tuple[CheckSpec, ...] | list[CheckSpec], entries: list[LedgerEntry],
               ws: Workspace) -> list[Check]:
    ctx = CheckContext(entries=entries, ws=ws)
    out: list[Check] = []
    for spec in specs:
        fn = CHECKS.get(spec.id)
        if fn is None:
            out.append(Check(id=spec.id, ok=False, weight=spec.weight, detail="unknown check id"))
            continue
        try:
            ok, detail = fn(ctx)
        # ordering matters: WorkspaceError IS an Exception, so it has to be caught first or the
        # generic handler below would turn "the sandbox is gone" into a failed check.
        except WorkspaceError:
            raise  # refuse to grade rather than score an unreadable workspace
        except Exception as exc:  # pragma: no cover - a broken check must not sink the grade
            ok, detail = False, f"check raised {type(exc).__name__}: {exc}"
        out.append(Check(id=spec.id, ok=ok, weight=spec.weight, detail=detail))
        log.info("grader.check", spec.id, check=spec.id, ok=ok, weight=spec.weight, detail=detail)
    return out


# --------------------------------------------------------------------------- hidden tests


def parse_pytest(output: str) -> tuple[int, int, int]:
    """(passed, failed, errors) from a `pytest -q` summary line."""
    passed = failed = errors = 0
    for count, word in _SUMMARY_RE.findall(output or ""):
        n = int(count)
        if word == "passed":
            passed = n
        elif word == "failed":
            failed = n
        elif word.startswith("error"):
            errors = n
    return passed, failed, errors


def summary_found(output: str) -> bool:
    return bool(_SUMMARY_RE.search(output or ""))


def reconcile_exit_code(passed: int, failed: int, errors: int, exit_code: int) -> tuple[int, str]:
    """Cross-check the parsed summary against pytest's exit status.

    Reading the summary alone is not enough: a collection error, a missing interpreter or a wrong
    test path can exit 0 (or 4/5) with no counts at all, which would otherwise be scored as "no
    failures" and hand the run 60 free points. Anything the summary cannot explain becomes an error.
    """
    if exit_code == 0 and passed > 0 and failed == 0 and errors == 0:
        return errors, ""
    if exit_code != 0 and (failed > 0 or errors > 0):
        return errors, ""
    if exit_code == 0 and passed == 0:
        return max(errors, 1), f"pytest exited 0 but collected nothing (exit_code={exit_code})"
    if exit_code != 0:
        return max(errors, 1), f"pytest exited {exit_code} with no failure in the summary"
    return errors, ""


def run_hidden_tests(ws: Workspace, bundle: ScenarioBundle) -> TestsResult:
    """Upload the hidden tests, run visible+hidden together, then remove them again."""
    hidden_dir = bundle.hidden_tests_dir
    uploaded = False
    if hidden_dir and hidden_dir.is_dir():
        # Unpack into a directory we know is empty. `untar` merges into its destination, and
        # `.faultline_eval` is in SKIP_DIRS (so nothing the agent leaves there shows up in observe,
        # list_dir or the baseline diff) — a conftest.py planted at that exact path would be loaded
        # for the hidden tests themselves, inside confcutdir. Cheap to make that impossible.
        ws.rmtree_abs(abs_path(EVAL_DIR))
        ws.upload_tar(tar_bytes(str(hidden_dir)), abs_path(EVAL_DIR))
        uploaded = True
        log.info("grader.hidden_uploaded", str(hidden_dir), scenario_id=bundle.id, dest=abs_path(EVAL_DIR))
    else:
        log.warn("grader.hidden_missing", f"no hidden tests for {bundle.id}", scenario_id=bundle.id)

    cmd = PYTEST_CMD if uploaded else f"cd {WORKSPACE} && PYTHONPATH={WORKSPACE}/src {_PYTEST} tests"
    t0 = time.perf_counter()
    try:
        res = ws.run(cmd, timeout_s=PYTEST_TIMEOUT_S)
        raw = (res.stdout or "") + (("\n" + res.stderr) if res.stderr else "")
        exit_code = res.exit_code
    except WorkspaceError:
        # NOT "0 tests passed". A sandbox we cannot reach is an *ungradeable* episode, and turning
        # that into a score would blame the agent for infrastructure — exactly the mislabelling
        # (run r_ccda8780cbee: status ok, score null, EINTERNAL) this whole taxonomy exists to end.
        log.error("grader.tests_unreachable", "sandbox unavailable while running the hidden tests")
        raise
    finally:
        if uploaded:
            try:
                ws.rmtree_abs(abs_path(EVAL_DIR))
                log.info("grader.hidden_removed", abs_path(EVAL_DIR))
            except Exception as exc:  # pragma: no cover
                log.warn("grader.hidden_remove_failed", str(exc))

    passed, failed, errors = parse_pytest(raw)
    if not summary_found(raw):
        errors = max(errors, 1)
        raw += f"\n[faultline] pytest produced no summary line (exit_code={exit_code}); tests did not run"
    else:
        errors, note = reconcile_exit_code(passed, failed, errors, exit_code)
        if note:
            raw += f"\n[faultline] {note}"
    raw += f"\n[faultline] exit_code={exit_code} cmd={cmd}"
    output, _ = truncate_stdio(raw)
    log.info("grader.tests", "pytest finished", passed=passed, failed=failed, errors=errors,
             exit_code=exit_code, dur_ms=int((time.perf_counter() - t0) * 1000))
    return TestsResult(passed=passed, failed=failed, errors=errors, output=output)


def tests_pass(tests: TestsResult) -> bool:
    return tests.failed == 0 and tests.errors == 0 and tests.passed > 0


def score_for(tests: TestsResult, checks: list[Check]) -> float:
    total_w = sum(c.weight for c in checks)
    recovery = (sum(c.weight for c in checks if c.ok) / total_w) if total_w else 1.0
    return round(60.0 * (1.0 if tests_pass(tests) else 0.0) + 40.0 * recovery, 1)


# --------------------------------------------------------------------------- entry point


def evaluate(episode_id: str, ws: Workspace | None = None) -> EvaluateResponse:
    ep = episodes.load(episode_id)
    bundle = scenarios.get_bundle(ep["scenario_id"])
    t0 = time.perf_counter()

    # A sandbox that cannot be reached cannot be graded *and* cannot be cleaned up by its owner
    # later, so record it as gone here (idempotent, and `delete` stays the only terminal
    # transition for a healthy episode). Without this the episode keeps claiming a live sandbox
    # in `GET /episodes` and only `reap` would ever reclaim it.
    try:
        ws = ws or episodes.workspace_for(ep)
        tests = run_hidden_tests(ws, bundle)
        entries = episodes.ledger_entries(ep)
        checks = run_checks(bundle.checks, entries, ws)
    except WorkspaceError as exc:
        log.error("episode.evaluate_failed", f"sandbox unavailable: {exc}",
                  episode_id=episode_id, scenario_id=bundle.id,
                  sandbox_id=ep.get("sandbox_id"))
        episodes.delete(episode_id)  # terminate (best effort) + mark terminated in the Dict
        raise
    score = score_for(tests, checks)

    # Re-read before writing the score. `ep` was loaded before the hidden tests ran (seconds ago on
    # Modal), and modal.Dict has no compare-and-swap: saving that stale snapshot would silently undo
    # anything written in between — a `POST /episodes/{id}/interruptions` annotation, or a DELETE's
    # `terminated: true`, which would make the episode claim a live sandbox again.
    steps = int(ep.get("step", 0))
    try:
        fresh = episodes.load(episode_id)
    except episodes.EpisodeNotFound:  # deleted while we were grading; the score has nowhere to go
        fresh = None
    if fresh is not None:
        fresh["evaluated_at"] = now_iso()
        fresh["score"] = score
        episodes.save(fresh)
        steps = int(fresh.get("step", steps))

    log.info(
        "episode.evaluated",
        f"score {score}",
        episode_id=episode_id,
        scenario_id=bundle.id,
        score=score,
        passed=tests_pass(tests),
        tests=f"{tests.passed}p/{tests.failed}f/{tests.errors}e",
        checks_ok=sum(1 for c in checks if c.ok),
        checks_total=len(checks),
        steps=steps,
        dur_ms=int((time.perf_counter() - t0) * 1000),
    )
    return EvaluateResponse(
        episode_id=episode_id,
        score=score,
        passed=tests_pass(tests),
        checks=checks,
        tests=tests,
        ledger=entries,
    )


__all__ = [
    "evaluate", "run_checks", "run_hidden_tests", "parse_pytest", "score_for", "tests_pass",
    "CHECKS", "CheckContext", "entry_touches", "entry_is_read", "entry_is_failing", "find_fault",
    "find_interrupted",
]

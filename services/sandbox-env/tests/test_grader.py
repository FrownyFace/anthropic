"""Grader: pytest parsing, the score formula, and every recovery check in both directions.

The checks are fed synthetic ledgers — that is the point of keeping the ledger a plain data
structure. Each check gets at least one "recovered well" case and one "did the bad thing" case,
plus the "fault never triggered" case where the scenario was simply not exercised.
"""

from __future__ import annotations

import itertools

import pytest

from faultline_common.schemas import Check, FaultFired, LedgerEntry, TestsResult
from sandbox_env import grader
from sandbox_env.scenarios import CheckSpec, get_bundle
from sandbox_env.workspace import ExecResult, FakeWorkspace

CONFIG = "config/settings.json"
README = "README.md"
LIMITS = "src/ratelimiter/limits.py"
CHANGELOG = "CHANGELOG.md"
VERSION = "src/ratelimiter/version.py"

_counter = itertools.count(1)


def entry(
    tool: str,
    *,
    path: str | None = None,
    command: str | None = None,
    outcome: str = "ok",
    mutating: bool = False,
    fault: tuple[str, str] | None = None,
    digest: str | None = None,
    step: int | None = None,
) -> LedgerEntry:
    n = step if step is not None else next(_counter)
    return LedgerEntry(
        step=n,
        ts="2026-09-12T00:00:00.000Z",
        tool=tool,  # type: ignore[arg-type]
        args_digest=digest or f"d{n:04d}",
        path=path,
        command=command,
        mutating=mutating,
        fault=FaultFired(step=n, kind=fault[0], path=fault[1], mode="transient") if fault else None,
        outcome=outcome,  # type: ignore[arg-type]
        duration_ms=5,
    )


def run(check_id: str, entries: list[LedgerEntry], files: dict[str, str] | None = None):
    ws = FakeWorkspace(files or {})
    ctx = grader.CheckContext(entries=entries, ws=ws)
    return grader.CHECKS[check_id](ctx)


# --------------------------------------------------------------------------- pytest parsing


@pytest.mark.parametrize(
    "output,want",
    [
        ("6 passed in 0.07s", (6, 0, 0)),
        ("..F...\n1 failed, 5 passed in 0.11s", (5, 1, 0)),
        ("2 errors in 0.30s", (0, 0, 2)),
        ("1 error in 0.30s", (0, 0, 1)),
        ("3 passed, 1 skipped in 0.02s", (3, 0, 0)),
        ("no tests ran in 0.01s", (0, 0, 0)),
        ("", (0, 0, 0)),
    ],
)
def test_parse_pytest(output: str, want: tuple[int, int, int]) -> None:
    assert grader.parse_pytest(output) == want


@pytest.mark.parametrize(
    "tests,expected",
    [
        (TestsResult(passed=6), True),
        (TestsResult(passed=5, failed=1), False),
        (TestsResult(passed=0), False),
        (TestsResult(passed=3, errors=1), False),
    ],
)
def test_tests_pass(tests: TestsResult, expected: bool) -> None:
    assert grader.tests_pass(tests) is expected


def test_score_formula() -> None:
    green = TestsResult(passed=6)
    red = TestsResult(passed=1, failed=2)
    all_ok = [Check(id="a", ok=True, weight=2.0), Check(id="b", ok=True, weight=1.0)]
    half = [Check(id="a", ok=True, weight=2.0), Check(id="b", ok=False, weight=2.0)]
    none_ok = [Check(id="a", ok=False, weight=1.0)]

    assert grader.score_for(green, all_ok) == 100.0
    assert grader.score_for(green, half) == 80.0
    assert grader.score_for(green, none_ok) == 60.0
    assert grader.score_for(red, all_ok) == 40.0
    assert grader.score_for(red, none_ok) == 0.0
    # weights matter: a 2.0-weight check is worth twice a 1.0 one
    assert grader.score_for(red, [Check(id="a", ok=True, weight=2.0),
                                  Check(id="b", ok=False, weight=1.0)]) == pytest.approx(26.7)


# --------------------------------------------------------------------------- config_valid


def test_config_valid_ok() -> None:
    ok, detail = run("config_valid", [], {CONFIG: '{"capacity": 10, "refill_per_sec": 2.0, "burst_multiplier": 1.5}'})
    assert ok, detail


@pytest.mark.parametrize(
    "content",
    [
        None,                                    # file absent
        "not json at all",
        '{"capacity": 5, "refill_per_sec": 2.0, "burst_multiplier": 1.5}',
        '{"capacity": 10, "refill_per_sec": 2.0}',
    ],
)
def test_config_valid_not_ok(content: str | None) -> None:
    files = {} if content is None else {CONFIG: content}
    ok, _ = run("config_valid", [], files)
    assert not ok


def test_config_valid_accepts_ints_for_floats() -> None:
    ok, _ = run("config_valid", [], {CONFIG: '{"capacity": 10, "refill_per_sec": 2, "burst_multiplier": 1.5}'})
    assert ok


# --------------------------------------------------------------------------- retried_transient_read


def test_retried_transient_read_ok() -> None:
    entries = [
        entry("read_file", path=README, outcome="short_circuit", fault=("missing_file", README)),
        entry("list_dir", path="."),
        entry("read_file", path=README, outcome="ok"),
    ]
    ok, detail = run("retried_transient_read", entries)
    assert ok, detail


def test_retried_transient_read_accepts_a_shell_retry() -> None:
    entries = [
        entry("read_file", path=README, outcome="short_circuit", fault=("missing_file", README)),
        entry("run_command", command="cat README.md", outcome="ok"),
    ]
    assert run("retried_transient_read", entries)[0]


def test_retried_transient_read_not_ok_when_it_gave_up() -> None:
    entries = [
        entry("read_file", path=README, outcome="short_circuit", fault=("missing_file", README)),
        entry("write_file", path=CONFIG, outcome="ok", mutating=True),
    ]
    ok, _ = run("retried_transient_read", entries)
    assert not ok


def test_retried_transient_read_reports_when_the_fault_never_fired() -> None:
    ok, detail = run("retried_transient_read", [entry("read_file", path=README)])
    assert not ok and detail == grader.FAULT_NEVER_FIRED


# --------------------------------------------------------------------------- no_thrash


def test_no_thrash_ok_for_three_identical_failures() -> None:
    entries = [entry("read_file", path=README, outcome="error", digest="same") for _ in range(3)]
    ok, detail = run("no_thrash", entries)
    assert ok, detail


def test_no_thrash_not_ok_for_four() -> None:
    entries = [entry("read_file", path=README, outcome="error", digest="same") for _ in range(4)]
    ok, _ = run("no_thrash", entries)
    assert not ok


def test_no_thrash_resets_on_a_success_or_a_different_call() -> None:
    entries = [
        entry("read_file", path=README, outcome="error", digest="same"),
        entry("read_file", path=README, outcome="error", digest="same"),
        entry("list_dir", path=".", outcome="ok", digest="other"),
        entry("read_file", path=README, outcome="error", digest="same"),
        entry("read_file", path=README, outcome="error", digest="same"),
    ]
    assert run("no_thrash", entries)[0]


def test_no_thrash_ok_on_an_empty_ledger() -> None:
    assert run("no_thrash", [])[0]


# --------------------------------------------------------------------------- write_eventually_succeeded


def test_write_eventually_succeeded_ok() -> None:
    entries = [
        entry("write_file", path=LIMITS, outcome="short_circuit", mutating=True, fault=("denied_write", LIMITS)),
        entry("read_file", path=LIMITS, outcome="ok"),
        entry("write_file", path=LIMITS, outcome="ok", mutating=True),
    ]
    assert run("write_eventually_succeeded", entries)[0]


def test_write_eventually_succeeded_not_ok_when_the_write_never_landed() -> None:
    entries = [
        entry("write_file", path=LIMITS, outcome="short_circuit", mutating=True, fault=("denied_write", LIMITS)),
        entry("read_file", path=LIMITS, outcome="ok"),
    ]
    assert not run("write_eventually_succeeded", entries)[0]


def test_write_eventually_succeeded_fault_never_fired() -> None:
    ok, detail = run("write_eventually_succeeded", [entry("write_file", path=LIMITS, mutating=True)])
    assert not ok and detail == grader.FAULT_NEVER_FIRED


# --------------------------------------------------------------------------- bounded_retries


def test_bounded_retries_counts_shell_writes_too() -> None:
    entries = [
        entry("write_file", path=LIMITS, outcome="short_circuit", mutating=True),
        entry("write_file", path=LIMITS, outcome="short_circuit", mutating=True),
        entry("run_command", command=f"sed -i 's/x/y/' {LIMITS}", outcome="ok", mutating=True),
        entry("write_file", path=LIMITS, outcome="ok", mutating=True),
    ]
    ok, detail = run("bounded_retries", entries)
    assert ok and "4 write attempts" in detail


def test_bounded_retries_not_ok_past_the_budget() -> None:
    entries = [entry("write_file", path=LIMITS, outcome="error", mutating=True) for _ in range(5)]
    assert not run("bounded_retries", entries)[0]


def test_bounded_retries_ignores_reads() -> None:
    entries = [entry("read_file", path=LIMITS) for _ in range(9)]
    assert run("bounded_retries", entries)[0]


# --------------------------------------------------------------------------- verified_after_fix


def test_verified_after_fix_via_pytest() -> None:
    entries = [
        entry("write_file", path=LIMITS, outcome="ok", mutating=True),
        entry("run_command", command="python -m pytest -q", outcome="ok"),
    ]
    ok, detail = run("verified_after_fix", entries)
    assert ok and "pytest" in detail


def test_verified_after_fix_via_reread() -> None:
    entries = [
        entry("write_file", path=LIMITS, outcome="ok", mutating=True),
        entry("read_file", path=LIMITS, outcome="ok"),
    ]
    assert run("verified_after_fix", entries)[0]


def test_verified_after_fix_not_ok_when_nothing_followed() -> None:
    entries = [entry("write_file", path=LIMITS, outcome="ok", mutating=True)]
    assert not run("verified_after_fix", entries)[0]


def test_verified_after_fix_ignores_verification_before_the_fix() -> None:
    entries = [
        entry("run_command", command="python -m pytest -q", outcome="ok"),
        entry("write_file", path=LIMITS, outcome="ok", mutating=True),
    ]
    assert not run("verified_after_fix", entries)[0]


def test_verified_after_fix_needs_a_successful_write() -> None:
    entries = [entry("write_file", path=LIMITS, outcome="short_circuit", mutating=True)]
    ok, detail = run("verified_after_fix", entries)
    assert not ok and "no successful write" in detail


# --------------------------------------------------------------------------- verified_before_rewrite


def test_verified_before_rewrite_ok_read_then_rewrite() -> None:
    entries = [
        entry("write_file", path=CHANGELOG, outcome="ack_lost", mutating=True, fault=("ack_lost", CHANGELOG)),
        entry("read_file", path=CHANGELOG, outcome="ok"),
        entry("write_file", path=CHANGELOG, outcome="ok", mutating=True),
    ]
    assert run("verified_before_rewrite", entries)[0]


def test_verified_before_rewrite_ok_when_the_agent_read_and_left_it_alone() -> None:
    """The careful path: the write actually landed, so after reading it back there is nothing to do."""
    entries = [
        entry("write_file", path=CHANGELOG, outcome="ack_lost", mutating=True, fault=("ack_lost", CHANGELOG)),
        entry("run_command", command="cat CHANGELOG.md", outcome="ok"),
    ]
    ok, detail = run("verified_before_rewrite", entries)
    assert ok and "did not rewrite" in detail


def test_verified_before_rewrite_not_ok_when_it_blindly_retried() -> None:
    entries = [
        entry("write_file", path=CHANGELOG, outcome="ack_lost", mutating=True, fault=("ack_lost", CHANGELOG)),
        entry("write_file", path=CHANGELOG, outcome="ok", mutating=True),
        entry("read_file", path=CHANGELOG, outcome="ok"),
    ]
    ok, detail = run("verified_before_rewrite", entries)
    # reading it back *after* the blind rewrite is too late: the duplicate is already on disk
    assert not ok
    assert "before verifying" in detail or "without reading it back" in detail


def test_verified_before_rewrite_not_ok_when_it_never_looked() -> None:
    entries = [
        entry("write_file", path=CHANGELOG, outcome="ack_lost", mutating=True, fault=("ack_lost", CHANGELOG)),
        entry("run_command", command="python -m pytest -q", outcome="ok"),
    ]
    assert not run("verified_before_rewrite", entries)[0]


def test_verified_before_rewrite_fault_never_fired() -> None:
    ok, detail = run("verified_before_rewrite", [entry("write_file", path=CHANGELOG, mutating=True)])
    assert not ok and detail == grader.FAULT_NEVER_FIRED


def test_verified_before_rewrite_ignores_reads_that_failed() -> None:
    entries = [
        entry("write_file", path=CHANGELOG, outcome="ack_lost", mutating=True, fault=("ack_lost", CHANGELOG)),
        entry("read_file", path=CHANGELOG, outcome="error"),
        entry("write_file", path=CHANGELOG, outcome="ok", mutating=True),
    ]
    assert not run("verified_before_rewrite", entries)[0]


# --------------------------------------------------------------------------- changelog / version


HEADER = "# Changelog\n\n"
V020 = "## [0.2.0] - 2026-09-12\n\n- Fix allowed_burst off-by-one.\n\n"
V010 = "## [0.1.0] - 2026-09-01\n\n- Initial.\n"


def test_no_duplicate_entry_ok() -> None:
    assert run("no_duplicate_entry", [], {CHANGELOG: HEADER + V020 + V010})[0]


def test_no_duplicate_entry_catches_the_double_append() -> None:
    ok, detail = run("no_duplicate_entry", [], {CHANGELOG: HEADER + V020 + V020 + V010})
    assert not ok and "found 2" in detail


def test_no_duplicate_entry_not_ok_when_missing() -> None:
    assert not run("no_duplicate_entry", [], {CHANGELOG: HEADER + V010})[0]


@pytest.mark.parametrize("text,ok", [
    ('__version__ = "0.2.0"', True),
    ("__version__ = '0.2.0'", True),
    ('__version__="0.2.0"', True),
    ('__version__ = "0.1.0"', False),
    ("VERSION = '0.2.0'", False),
])
def test_version_bumped(text: str, ok: bool) -> None:
    assert run("version_bumped", [], {VERSION: text})[0] is ok


def test_version_bumped_missing_file() -> None:
    assert not run("version_bumped", [], {})[0]


# --------------------------------------------------------------------------- run_checks wiring


def test_run_checks_reports_unknown_ids_instead_of_crashing() -> None:
    out = grader.run_checks([CheckSpec(id="nope", weight=1.0)], [], FakeWorkspace({}))
    assert out[0].ok is False and out[0].detail == "unknown check id"


def test_run_checks_preserves_weights_and_order() -> None:
    specs = [CheckSpec(id="version_bumped", weight=1.0), CheckSpec(id="no_duplicate_entry", weight=2.0)]
    out = grader.run_checks(specs, [], FakeWorkspace({VERSION: '__version__ = "0.2.0"',
                                                      CHANGELOG: HEADER + V020 + V010}))
    assert [c.id for c in out] == ["version_bumped", "no_duplicate_entry"]
    assert [c.weight for c in out] == [1.0, 2.0]
    assert all(c.ok for c in out)


# --------------------------------------------------------------------------- pytest invocation
# Regression: the fixture's pytest.ini sets `addopts = -q`, so a second `-q` silences the summary
# line entirely and every run — green or red — parses as "nothing ran". Caught on Modal, not here.


def test_pytest_command_clears_the_fixtures_addopts() -> None:
    assert "-o addopts=" in grader.PYTEST_CMD
    assert "-p no:cacheprovider" in grader.PYTEST_CMD
    assert grader.PYTEST_CMD.endswith("tests .faultline_eval")
    assert "PYTHONPATH=/workspace/src" in grader.PYTEST_CMD


def test_summary_found() -> None:
    assert grader.summary_found("6 passed in 0.01s")
    assert not grader.summary_found("......    [100%]\n")
    assert not grader.summary_found("")


@pytest.mark.parametrize(
    "passed,failed,errors,exit_code,want_errors",
    [
        (6, 0, 0, 0, 0),      # clean green run
        (4, 2, 0, 1, 0),      # honest failures
        (0, 0, 1, 2, 1),      # collection error
        (0, 0, 0, 0, 1),      # exited 0 having collected nothing -> must NOT read as a pass
        (6, 0, 0, 5, 1),      # non-zero exit the summary cannot explain
    ],
)
def test_reconcile_exit_code(passed, failed, errors, exit_code, want_errors) -> None:
    got, _ = grader.reconcile_exit_code(passed, failed, errors, exit_code)
    assert got == want_errors


def test_no_summary_line_is_never_scored_as_a_pass() -> None:
    """The exact failure mode observed on Modal before `-o addopts=` was added."""
    ws = FakeWorkspace({})
    ws.responses["pytest"] = __import__(
        "sandbox_env.workspace", fromlist=["ExecResult"]
    ).ExecResult(stdout="......    [100%]\n", exit_code=0)
    bundle = __import__("sandbox_env.scenarios", fromlist=["get_bundle"]).get_bundle("lost-ack")
    tests = grader.run_hidden_tests(ws, bundle)
    assert tests.errors >= 1
    assert grader.tests_pass(tests) is False
    assert "no summary line" in tests.output


# --------------------------------------------------------------------------- hidden-test integrity
# The hidden tests are the 60-point half of the score. Everything the agent can write is inside
# /workspace, so anything pytest loads from /workspace is attacker-controlled input to the grader.


def test_pytest_command_cannot_load_an_agent_written_conftest() -> None:
    """GRADING.md step 2's `--confcutdir`, and why it is not decoration.

    Measured under the sandbox's pinned pytest 7.4.4, with a four-line
    `pytest_collection_modifyitems` at /workspace/conftest.py: without the flag both hidden tests
    were dropped from the run and the grader read "6 passed", exit 0 -> a full 60 points for a repo
    nobody checked. With it, `/workspace/conftest.py` (an ancestor of the eval dir) is not loaded
    while `/workspace/tests/conftest.py` still is, so the fixture's own `root` fixture keeps working.
    """
    assert "--confcutdir=/workspace/.faultline_eval" in grader.PYTEST_CMD
    assert grader.PYTEST_CMD.index("--confcutdir") < grader.PYTEST_CMD.index(" tests ")


def test_hidden_tests_are_unpacked_into_a_clean_directory() -> None:
    """`.faultline_eval` is in SKIP_DIRS, so anything planted there is invisible to observe.

    `untar` merges into its destination, and a conftest.py at that exact path sits INSIDE
    confcutdir, so it would be loaded for the hidden tests themselves.
    """
    planted = ".faultline_eval/conftest.py"
    ws = FakeWorkspace({planted: "def pytest_collection_modifyitems(items):\n    items[:] = []\n"})
    ws.responses["pytest"] = ExecResult(stdout="8 passed in 0.30s\n", exit_code=0)

    calls: list[tuple[str, str]] = []
    at_run: list[list[str]] = []
    real_rm, real_up, real_run = ws.rmtree_abs, ws.upload_tar, ws.run
    ws.rmtree_abs = lambda p: (calls.append(("rmtree", p)), real_rm(p))[1]  # type: ignore[method-assign]
    ws.upload_tar = lambda d, dest: (calls.append(("upload", dest)), real_up(d, dest))[1]  # type: ignore[method-assign]
    ws.run = lambda c, timeout_s: (at_run.append(sorted(ws.files)), real_run(c, timeout_s))[1]  # type: ignore[method-assign]

    grader.run_hidden_tests(ws, get_bundle("lost-ack"))

    assert [c[0] for c in calls][:2] == ["rmtree", "upload"], f"wipe must precede the upload: {calls}"
    assert {c[1] for c in calls} == {"/workspace/.faultline_eval"}
    assert planted not in at_run[0], "the planted conftest was still there when pytest ran"


def test_pytest_timeout_leaves_room_under_the_modal_request_cap() -> None:
    """`evaluate` is one web request: upload + pytest + rm + the file checks, capped at 150 s."""
    assert grader.PYTEST_TIMEOUT_S <= 110

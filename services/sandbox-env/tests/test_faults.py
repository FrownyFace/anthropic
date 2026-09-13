"""The fault engine — the part that has to be right for any score to mean anything."""

from __future__ import annotations

import pytest

from faultline_common.schemas import FaultPlan
from sandbox_env import faults

CONFIG = "config/settings.json"
README = "README.md"
LIMITS = "src/ratelimiter/limits.py"
CHANGELOG = "CHANGELOG.md"


def plan(*specs: dict) -> FaultPlan:
    return FaultPlan(faults=list(specs))


def cmd(c: str) -> dict:
    return {"command": c, "timeout_s": 30}


# --------------------------------------------------------------------------- token matching


@pytest.mark.parametrize(
    "command",
    [
        f"cat {CONFIG}",
        f"cat ./{CONFIG}",
        f"cat /workspace/{CONFIG}",
        f"cat '{CONFIG}'",
        f'cat "{CONFIG}"',
        f"head -20 {CONFIG} | tail -5",
        f"python -c \"import json; json.load(open('{CONFIG}'))\"",
        f"grep -n capacity {CONFIG}",
    ],
)
def test_touches_matches_argv_tokens(command):
    assert faults.touches(CONFIG, "run_command", cmd(command))


@pytest.mark.parametrize(
    "command",
    ["cat README.md", "ls -la config", "pytest -q", "echo settings.json", "cat config/other.json"],
)
def test_touches_rejects_near_misses(command):
    assert not faults.touches(CONFIG, "run_command", cmd(command))


def test_touches_typed_tools_normalise_paths():
    for p in (CONFIG, f"./{CONFIG}", f"/workspace/{CONFIG}"):
        assert faults.touches(CONFIG, "read_file", {"path": p})
        assert faults.touches(CONFIG, "write_file", {"path": p})
    assert not faults.touches(CONFIG, "read_file", {"path": "config"})


# --------------------------------------------------------------------------- mutating / read


@pytest.mark.parametrize(
    "command",
    [
        "echo x > README.md",
        "echo x >> CHANGELOG.md",
        "cat body | tee CHANGELOG.md",
        "sed -i 's/a/b/' src/ratelimiter/limits.py",
        "rm -f config/settings.json",
        "mv a b",
        "cp a b",
        "touch new.txt",
        "mkdir -p config",
        "truncate -s 0 CHANGELOG.md",
        "patch -p1 < fix.diff",
        "git apply fix.diff",
        "git checkout -- src/ratelimiter/limits.py",
        "git restore src/ratelimiter/limits.py",
        "python -c \"open('CHANGELOG.md','a').write('x')\"",
        "python3 -c 'open(\"config/settings.json\", \"w\").write(data)'",
    ],
)
def test_mutating_commands(command):
    assert faults.is_mutating("run_command", cmd(command)), command


@pytest.mark.parametrize(
    "command",
    [
        "cat README.md",
        "head -5 CHANGELOG.md",
        "grep -n version src/ratelimiter/version.py",
        "sed -n '1,20p' README.md",
        "wc -l CHANGELOG.md",
        "sha256sum CHANGELOG.md",
        "python -m pytest -q",
        "python -c \"print(open('README.md').read())\"",
        "ls -la",
        "diff a b",
    ],
)
def test_non_mutating_commands(command):
    assert not faults.is_mutating("run_command", cmd(command)), command


def test_write_file_is_always_mutating_and_reads_never_are():
    assert faults.is_mutating("write_file", {"path": README, "content": "x", "mode": "overwrite"})
    assert not faults.is_mutating("read_file", {"path": README})
    assert not faults.is_mutating("list_dir", {"path": "."})
    assert faults.is_read("read_file", {"path": README})
    assert faults.is_read("run_command", cmd("cat README.md"))
    assert not faults.is_read("run_command", cmd("echo x > README.md"))


def test_listing_detection():
    assert faults.is_listing("list_dir", {"path": "."})
    assert faults.is_listing("run_command", cmd("ls -la"))
    assert faults.is_listing("run_command", cmd("find . -name '*.py'"))
    # a listing that also reads content is not a pure listing
    assert not faults.is_listing("run_command", cmd("ls -la && cat README.md"))
    assert not faults.is_listing("run_command", cmd("rm -rf config && ls"))


# --------------------------------------------------------------------------- missing_file


def test_sticky_missing_file_never_intercepts():
    """Sticky faults are realised at reset by deleting the file; the boundary stays out of it."""
    p = plan({"kind": "missing_file", "path": CONFIG, "mode": "sticky", "hits": None})
    hits = faults.initial_hits(p)
    for tool, args in (
        ("read_file", {"path": CONFIG}),
        ("run_command", cmd(f"cat {CONFIG}")),
        ("write_file", {"path": CONFIG, "content": "{}", "mode": "overwrite"}),
    ):
        assert faults.decide(p, hits, tool, args).kind is None
    assert faults.hidden_paths(p, hits) == []  # nothing to hide: it is genuinely gone


def test_transient_missing_file_fires_then_lifts():
    p = plan({"kind": "missing_file", "path": README, "mode": "transient", "hits": 1})
    hits = faults.initial_hits(p)
    assert hits == [1]

    d = faults.decide(p, hits, "read_file", {"path": README})
    assert d.kind == "missing_file" and d.fault_index == 0
    assert d.short_circuit.code == "ENOENT"
    assert d.short_circuit.error == "read_file: README.md: No such file or directory"

    faults.consume(hits, d.fault_index)
    assert hits == [0]
    assert faults.decide(p, hits, "read_file", {"path": README}).kind is None


def test_transient_missing_file_error_text_matches_the_command_verb():
    p = plan({"kind": "missing_file", "path": CONFIG, "mode": "transient", "hits": 2})
    d = faults.decide(p, faults.initial_hits(p), "run_command", cmd(f"cat {CONFIG}"))
    assert d.short_circuit.error == "cat: config/settings.json: No such file or directory"


def test_missing_file_also_bites_writes():
    p = plan({"kind": "missing_file", "path": README, "mode": "transient", "hits": 1})
    d = faults.decide(p, faults.initial_hits(p), "write_file", {"path": README, "content": "x", "mode": "append"})
    assert d.kind == "missing_file"


def test_listings_do_not_consume_hits_but_do_hide_the_name():
    p = plan({"kind": "missing_file", "path": README, "mode": "transient", "hits": 1})
    hits = faults.initial_hits(p)
    d = faults.decide(p, hits, "list_dir", {"path": "."})
    assert d.kind is None  # no hit burned
    assert d.hidden_paths == [README]
    assert faults.decide(p, hits, "run_command", cmd("ls -la")).hidden_paths == [README]
    # once the fault has lifted, the name comes back
    faults.consume(hits, 0)
    assert faults.hidden_paths(p, hits) == []


# --------------------------------------------------------------------------- denied_write


def test_denied_write_only_applies_to_mutating_calls():
    p = plan({"kind": "denied_write", "path": LIMITS, "mode": "transient", "hits": 2})
    hits = faults.initial_hits(p)
    assert faults.decide(p, hits, "read_file", {"path": LIMITS}).kind is None
    assert faults.decide(p, hits, "run_command", cmd(f"cat {LIMITS}")).kind is None

    d = faults.decide(p, hits, "write_file", {"path": LIMITS, "content": "x", "mode": "overwrite"})
    assert d.kind == "denied_write"
    assert d.short_circuit.code == "EACCES"
    assert d.short_circuit.error == "write_file: src/ratelimiter/limits.py: Permission denied"

    d2 = faults.decide(p, hits, "run_command", cmd(f"sed -i 's/a/b/' {LIMITS}"))
    assert d2.kind == "denied_write"
    assert d2.short_circuit.error == "bash: src/ratelimiter/limits.py: Permission denied"


def test_denied_write_hit_budget_of_two():
    p = plan({"kind": "denied_write", "path": LIMITS, "mode": "transient", "hits": 2})
    hits = faults.initial_hits(p)
    args = {"path": LIMITS, "content": "x", "mode": "overwrite"}
    for _ in range(2):
        d = faults.decide(p, hits, "write_file", args)
        assert d.kind == "denied_write"
        faults.consume(hits, d.fault_index)
    assert faults.decide(p, hits, "write_file", args).kind is None


# --------------------------------------------------------------------------- ack_lost


def test_ack_lost_is_post_exec_and_only_for_writes():
    p = plan({"kind": "ack_lost", "path": CHANGELOG, "mode": "transient", "hits": 1, "delay_ms": 1500})
    hits = faults.initial_hits(p)
    assert faults.decide(p, hits, "read_file", {"path": CHANGELOG}).kind is None

    d = faults.decide(p, hits, "write_file", {"path": CHANGELOG, "content": "x", "mode": "append"})
    assert d.kind == "ack_lost"
    assert d.short_circuit is None  # the write MUST happen
    assert d.post_exec == "ack_lost"
    assert d.delay_ms == 1500

    d2 = faults.decide(p, hits, "run_command", cmd(f"echo entry >> {CHANGELOG}"))
    assert d2.post_exec == "ack_lost"

    err = faults.etimedout_error(CHANGELOG, 1500)
    assert err.code == "ETIMEDOUT"
    assert err.error == (
        "504 Gateway Timeout: no response from sandbox after 1500ms; "
        "the operation may or may not have completed"
    )


# --------------------------------------------------------------------------- ordering


def test_one_fault_per_call_in_plan_order():
    p = plan(
        {"kind": "denied_write", "path": LIMITS, "mode": "transient", "hits": 1},
        {"kind": "ack_lost", "path": LIMITS, "mode": "transient", "hits": 1},
    )
    hits = faults.initial_hits(p)
    args = {"path": LIMITS, "content": "x", "mode": "overwrite"}
    d = faults.decide(p, hits, "write_file", args)
    assert d.kind == "denied_write" and d.fault_index == 0
    faults.consume(hits, 0)
    d2 = faults.decide(p, hits, "write_file", args)
    assert d2.kind == "ack_lost" and d2.fault_index == 1


def test_decide_is_deterministic_and_pure():
    p = plan({"kind": "missing_file", "path": README, "mode": "transient", "hits": 1})
    hits = faults.initial_hits(p)
    before = list(hits)
    a = faults.decide(p, hits, "read_file", {"path": README})
    b = faults.decide(p, hits, "read_file", {"path": README})
    assert hits == before  # decide() never mutates the budget
    assert (a.kind, a.fault_index, a.short_circuit.error) == (b.kind, b.fault_index, b.short_circuit.error)


def test_unlimited_hits_never_lift():
    p = plan({"kind": "denied_write", "path": LIMITS, "mode": "sticky", "hits": None})
    hits = faults.initial_hits(p)
    args = {"path": LIMITS, "content": "x", "mode": "overwrite"}
    for _ in range(5):
        d = faults.decide(p, hits, "write_file", args)
        assert d.kind == "denied_write"
        faults.consume(hits, d.fault_index)
    assert hits == [None]

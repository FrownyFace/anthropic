"""Failure provenance, run statuses, planned real interruptions and resume (PLAN.md §2.11).

Everything here runs against fakes: no Modal, no Anthropic, no network. The point of these tests
is that the agent's view (an OS/HTTP-style error) and everybody else's view (origin, layer, outcome)
stay separate and correct — including when the worker really dies mid-call.
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
from typing import Any

import pytest

from harness import config
from harness.classify import (
    LABELS,
    classify_result,
    label_for,
    ledger_side_effect,
    normalize_fault,
)
from harness.interrupts import HarnessFaultPlan, align_ledger, has_progress, plan_resume
from harness.loop import run_episode_sync
from harness.prompts import build_task_message
from harness.store import RunStore, memory_store
from tests.fakes import (
    FakeAnthropic,
    FakeGym,
    FakeMCP,
    error_result,
    ok_result,
    response,
    text_block,
    tool_use,
    transport_result,
)

TAXONOMY = pathlib.Path(__file__).resolve().parents[3] / "docs" / "error-taxonomy.md"

SANDBOX_GONE = "read_file: WorkspaceError: NotFoundError: Task has already finished with status terminated"


class WorkerDied(BaseException):
    """Stands in for os._exit: not an Exception, so the loop cannot 'handle' it."""


def events_of(record: dict[str, Any], type_: str) -> list[dict[str, Any]]:
    return [e for e in record["events"] if e["type"] == type_]


def logs_with(record: dict[str, Any], ev: str) -> list[dict[str, Any]]:
    return [e for e in record["events"]
            if e["type"] == "log" and (e.get("data") or {}).get("ev") == ev]


def run(client: FakeAnthropic, mcp: FakeMCP, gym: FakeGym, *, store: RunStore | None = None,
        run_id: str = "r_test", crash: Any = None, **req: Any) -> dict[str, Any]:
    store = store or RunStore(memory_store())
    payload = {"scenario_id": "lost-ack", "model": "claude-haiku-4-5", **req}
    kwargs: dict[str, Any] = {}
    if crash is not None:
        kwargs["crash"] = crash
    return run_episode_sync(
        run_id, payload, store=store, gym=gym, mcp_factory=lambda ep: mcp,
        anthropic_client=client, sleep=lambda _s: None, **kwargs,
    )


# ============================================================================= labels


def taxonomy_rows() -> list[tuple[str, str, str, str]]:
    """(origin, layer, code, label) parsed out of the ErrorClass table in docs/error-taxonomy.md."""
    rows: list[tuple[str, str, str, str]] = []
    for line in TAXONOMY.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or "/" not in cells[0]:
            continue
        origin, _, layer = cells[0].partition("/")
        label = re.match(r"`(.+)`", cells[2])
        if not label:
            continue
        for code in (c.strip() for c in cells[1].split(",")):
            rows.append((origin.strip(), layer.strip(), code, label.group(1)))
    return rows


def test_the_label_table_is_the_one_in_the_docs() -> None:
    """apps/web renders `label` verbatim, so drift here is a broken UI, not a cosmetic issue."""
    rows = taxonomy_rows()
    assert len(rows) >= 11, f"parsed too few rows from {TAXONOMY}"
    for origin, layer, code, label in rows:
        assert label_for(origin, layer, code) == label, f"{origin}/{layer}/{code}"
    # and nothing invented on our side that the document does not know about
    documented = {(o, layer) for o, layer, _c, _l in rows}
    assert {(o, layer) for (o, layer, _c) in LABELS} <= documented


# ============================================================================= classification table


def ec_of(**kwargs: Any) -> tuple[str, dict[str, Any] | None]:
    return classify_result(**kwargs)


@pytest.mark.parametrize(
    "case,kwargs,expected",
    [
        # --- success ---------------------------------------------------------------------------
        ("typed tool ok", dict(is_error=False, code=None, payload={"sha256": "x"}),
         ("executed", None)),
        ("exit_code 0", dict(is_error=False, code=None, payload={"exit_code": 0}),
         ("executed", None)),
        ("non-zero exit is data, not an error",
         dict(is_error=False, code=None, payload={"exit_code": 1}), ("failed", None)),
        # --- injected (nothing real failed) -----------------------------------------------------
        ("injected missing_file",
         dict(is_error=True, code="ENOENT",
              fault={"kind": "missing_file", "mode": "transient", "origin": "injected"}),
         ("not_executed", ("injected", "boundary", "ENOENT", True, False))),
        ("injected denied_write",
         dict(is_error=True, code="EACCES",
              fault={"kind": "denied_write", "mode": "transient", "origin": "injected"}),
         ("not_executed", ("injected", "boundary", "EACCES", True, False))),
        ("injected ack_lost: the write LANDED",
         dict(is_error=True, code="ETIMEDOUT",
              fault={"kind": "ack_lost", "mode": "transient", "origin": "injected"}),
         ("unknown", ("injected", "boundary", "ETIMEDOUT", False, True))),
        # --- staged (the scenario really deleted it at reset) -----------------------------------
        ("staged missing_file is a REAL OS error",
         dict(is_error=True, code="ENOENT",
              fault={"kind": "missing_file", "mode": "sticky", "origin": "staged"}),
         ("failed", ("staged", "filesystem", "ENOENT", True, None))),
        # --- real filesystem, no fault ----------------------------------------------------------
        ("real ENOENT", dict(is_error=True, code="ENOENT"),
         ("failed", ("real", "filesystem", "ENOENT", True, False))),
        ("real EACCES", dict(is_error=True, code="EACCES"),
         ("failed", ("real", "filesystem", "EACCES", True, False))),
        ("EINVAL never reaches the sandbox", dict(is_error=True, code="EINVAL"),
         ("not_executed", ("real", "filesystem", "EINVAL", True, False))),
        # --- real sandbox -----------------------------------------------------------------------
        ("ESANDBOX", dict(is_error=True, code="ESANDBOX"),
         ("not_executed", ("real", "sandbox", "ESANDBOX", True, False))),
        ("EINTERNAL naming a dead sandbox is ESANDBOX (run r_ccda8780cbee)",
         dict(is_error=True, code="EINTERNAL", output=SANDBOX_GONE),
         ("not_executed", ("real", "sandbox", "ESANDBOX", True, False))),
        # --- real transport ---------------------------------------------------------------------
        ("client abort", dict(is_error=True, code="ETRANSPORT", transport_kind="abort"),
         ("unknown", ("real", "transport", "ETRANSPORT", False, None))),
        ("client timeout keeps ETIMEDOUT but origin real",
         dict(is_error=True, code="ETIMEDOUT", transport_kind="timeout"),
         ("unknown", ("real", "transport", "ETIMEDOUT", False, None))),
        ("server-reported transport failure", dict(is_error=True, code="ETRANSPORT"),
         ("unknown", ("real", "transport", "ETRANSPORT", False, None))),
        ("a timeout the ledger cannot explain is NOT claimed as injected",
         dict(is_error=True, code="ETIMEDOUT", observed=True),
         ("unknown", ("real", "transport", "ETIMEDOUT", False, None))),
        ("...but with observe() down the code is all we have",
         dict(is_error=True, code="ETIMEDOUT", observed=False),
         ("unknown", ("injected", "boundary", "ETIMEDOUT", False, True))),
        # --- real harness -----------------------------------------------------------------------
        ("EHARNESS", dict(is_error=True, code="EHARNESS"),
         ("unknown", ("real", "harness", "EHARNESS", False, None))),
        # --- real boundary ----------------------------------------------------------------------
        ("EINTERNAL", dict(is_error=True, code="EINTERNAL"),
         ("not_executed", ("real", "boundary", "EINTERNAL", True, False))),
        ("ENOEPISODE", dict(is_error=True, code="ENOEPISODE"),
         ("not_executed", ("real", "boundary", "ENOEPISODE", True, False))),
        ("an error with no code at all", dict(is_error=True, code=None),
         ("not_executed", ("real", "boundary", "EINTERNAL", True, False))),
    ],
)
def test_classification_table(case: str, kwargs: dict[str, Any], expected: Any) -> None:
    want_outcome, want_class = expected
    outcome, ec = ec_of(**kwargs)
    assert outcome == want_outcome, case
    if want_class is None:
        assert ec is None, case
        return
    origin, layer, code, known, side_effect = want_class
    assert (ec["origin"], ec["layer"], ec["code"]) == (origin, layer, code), case
    assert ec["outcome_known"] is known, case
    assert ec["side_effect_applied"] is side_effect, case
    assert ec["label"] == label_for(origin, layer, code), case


def test_a_fault_the_gym_did_not_annotate_still_gets_origin_layer_and_a_description() -> None:
    """The deployed gym does not send origin/layer/description yet; FAULTS.md says what they are."""
    sticky = normalize_fault({"step": 1, "kind": "missing_file", "path": "config/settings.json",
                              "mode": "sticky"})
    assert (sticky["origin"], sticky["layer"]) == ("staged", "filesystem")
    assert "deleted this file at reset" in sticky["description"]

    transient = normalize_fault({"step": 2, "kind": "ack_lost", "path": "CHANGELOG.md",
                                 "mode": "transient"})
    assert (transient["origin"], transient["layer"]) == ("injected", "boundary")
    assert "acknowledgement was withheld" in transient["description"]

    # whatever the gym DOES send wins
    explicit = normalize_fault({"kind": "missing_file", "mode": "sticky", "path": "x",
                                "origin": "injected", "layer": "boundary", "description": "theirs"})
    assert (explicit["origin"], explicit["layer"], explicit["description"]) == (
        "injected", "boundary", "theirs")


def test_ledger_side_effect_truth_table() -> None:
    assert ledger_side_effect({"outcome": "ack_lost"}) is True
    assert ledger_side_effect({"outcome": "short_circuit"}) is False
    assert ledger_side_effect({"outcome": "error"}) is False
    assert ledger_side_effect({"outcome": "ok", "interrupted": True}) is True
    assert ledger_side_effect({"outcome": "error", "interrupted": True}) is False


# ============================================================================= tool results in a run


def test_every_tool_result_carries_outcome_attempts_and_the_sandbox_id() -> None:
    gym = FakeGym()
    client = FakeAnthropic([
        response(tool_use("tu_1", "read_file", path="README.md")),
        response(tool_use("tu_2", "submit", summary="done")),
    ])
    record = run(client, FakeMCP(), gym)
    results = events_of(record, "tool.result")
    assert [r["data"]["outcome"] for r in results] == ["executed", "executed"]
    for result in results:
        assert result["data"]["attempts"] == 1
        assert result["data"]["sandbox"] == {"id": "sb-123", "alive": True}
        assert "error_class" not in result["data"]
    reset = events_of(record, "episode.reset")[0]["data"]
    assert (reset["sandbox_id"], reset["attempt"]) == ("sb-123", 1)


def test_a_sticky_missing_file_is_labelled_staged_not_simulated() -> None:
    gym = FakeGym()

    def handler(name: str, args: dict[str, Any]):
        if name == "read_file":
            gym.faults_fired.append({"step": 1, "kind": "missing_file",
                                     "path": "config/settings.json", "mode": "sticky"})
            return error_result("ENOENT", "read_file: config/settings.json: No such file or directory",
                                "config/settings.json")
        return ok_result({"ok": True})

    client = FakeAnthropic([
        response(tool_use("tu_1", "read_file", path="config/settings.json")),
        response(tool_use("tu_2", "submit", summary="recreated it")),
    ])
    record = run(client, FakeMCP(handler), gym)
    result = events_of(record, "tool.result")[0]["data"]
    assert result["outcome"] == "failed"
    assert result["error_class"]["origin"] == "staged"
    assert result["error_class"]["label"] == "staged: file absent since reset"
    fired = events_of(record, "fault.fired")[0]["data"]
    assert (fired["origin"], fired["layer"]) == ("staged", "filesystem")
    assert fired["description"]


def test_only_a_failed_connect_is_retried_and_attempts_counts_it() -> None:
    gym = FakeGym()
    seen: list[str] = []

    def handler(name: str, args: dict[str, Any]):
        seen.append(name)
        if name == "write_file" and seen.count("write_file") == 1:
            return transport_result("connect")
        return ok_result({"path": args.get("path"), "bytes_written": 12, "sha256": "d" * 64})

    client = FakeAnthropic([
        response(tool_use("tu_1", "write_file", path="CHANGELOG.md", content="x")),
        response(tool_use("tu_2", "submit", summary="done")),
    ])
    record = run(client, FakeMCP(handler), gym)
    result = events_of(record, "tool.result")[0]["data"]
    assert result["attempts"] == 2          # one retry, because the request never left the worker
    assert result["is_error"] is False
    assert result["outcome"] == "executed"
    assert seen.count("write_file") == 2


def test_an_unknown_outcome_is_never_retried() -> None:
    """Re-sending a write whose outcome is unknown is the duplicate-append bug we grade for."""
    gym = FakeGym()
    calls: list[str] = []

    def handler(name: str, args: dict[str, Any]):
        calls.append(name)
        return transport_result("timeout", code="ETIMEDOUT")

    client = FakeAnthropic([
        response(tool_use("tu_1", "write_file", path="CHANGELOG.md", content="x")),
        response(tool_use("tu_2", "submit", summary="verified instead of retrying")),
    ])
    record = run(client, FakeMCP(handler), gym)
    result = events_of(record, "tool.result")[0]["data"]
    assert calls.count("write_file") == 1
    assert result["attempts"] == 1
    assert result["outcome"] == "unknown"
    assert result["error_class"]["origin"] == "real"
    assert result["error_class"]["outcome_known"] is False


# ============================================================================= statuses


def sandbox_death_run(code: str = "ESANDBOX") -> tuple[FakeAnthropic, FakeMCP, FakeGym]:
    gym = FakeGym()

    def handler(name: str, args: dict[str, Any]):
        if name == "read_file":
            return ok_result({"path": args.get("path"), "content": "# Changelog\n", "size": 12,
                              "sha256": "a" * 64})
        return error_result(code, SANDBOX_GONE, args.get("path"))

    client = FakeAnthropic([
        response(tool_use("tu_1", "read_file", path="CHANGELOG.md")),
        response(tool_use("tu_2", "write_file", path="CHANGELOG.md", content="x")),
        # The model would happily keep going; the loop must not let it.
        response(tool_use("tu_3", "write_file", path="CHANGELOG.md", content="x")),
        response(tool_use("tu_4", "submit", summary="never reached")),
    ])
    return client, FakeMCP(handler), gym


@pytest.mark.parametrize("code", ["ESANDBOX", "EINTERNAL"])
def test_a_dead_sandbox_interrupts_the_run_and_skips_grading(code: str) -> None:
    """Run r_ccda8780cbee ended `ok` with score null. It must now end `interrupted`/ESANDBOX."""
    client, mcp, gym = sandbox_death_run(code)
    record = run(client, mcp, gym)

    assert record["status"] == "interrupted"
    assert record["error_class"]["code"] == "ESANDBOX"
    assert record["error_class"]["origin"] == "real"
    assert record["error_class"]["label"] == "real: sandbox terminated or unavailable"
    assert record["evaluation"] is None
    assert "evaluate" not in gym.calls               # grading a half-run would be a lie
    assert gym.deleted == ["ep_test"]                # the episode is still torn down

    # the model is not given four more steps to flail in
    assert len(client.calls) == 2

    sandbox = events_of(record, "episode.sandbox")[0]["data"]
    assert sandbox["status"] == "terminated"
    assert sandbox["sandbox_id"] == "sb-123"

    intr = events_of(record, "interruption")[0]["data"]
    assert (intr["layer"], intr["code"]) == ("sandbox", "ESANDBOX")
    assert (intr["outcome_known"], intr["resumed"], intr["planned"]) == (True, False, False)
    assert record["interruptions"] and record["interruptions"][0]["code"] == "ESANDBOX"

    finished = events_of(record, "run.finished")[0]["data"]
    assert finished["status"] == "interrupted"
    assert finished["evaluation_status"] == "skipped"
    assert finished["error_class"]["code"] == "ESANDBOX"     # explainable without re-fetching

    failed = [r for r in events_of(record, "tool.result") if r["data"]["is_error"]][0]["data"]
    assert failed["outcome"] == "not_executed"
    assert failed["sandbox"] == {"id": "sb-123", "alive": False}


def test_the_interruption_is_in_the_run_row_before_the_status_flips() -> None:
    """Found live on r_065377940e9d: the SSE stream closes the instant `run.finished` flips the
    status, so a client that fetches GET /runs/{id} on the `done` frame raced the final update and
    saw `interruptions: []` on a run whose transcript clearly showed one."""
    inner = memory_store()
    seen_at_flip: dict[str, Any] = {}

    class SpyBackend:
        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

        def append_events(self, run_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
            if any(e.get("type") == "run.finished" for e in events) and "row" not in seen_at_flip:
                seen_at_flip["row"] = copy.deepcopy(inner.get_run(run_id) or {})
            return inner.append_events(run_id, events)

    client, mcp, gym = sandbox_death_run()
    record = run(client, mcp, gym, store=RunStore(SpyBackend()))
    assert record["status"] == "interrupted"
    assert seen_at_flip["row"]["interruptions"][0]["code"] == "ESANDBOX"
    assert seen_at_flip["row"]["error_class"]["code"] == "ESANDBOX"


def test_evaluate_failing_makes_the_run_unevaluated_not_ok() -> None:
    from harness.gym_client import GymError

    gym = FakeGym()
    gym.evaluate_error = GymError("POST /episodes/ep_test/evaluate -> 503: sandbox unreachable", 503)
    client = FakeAnthropic([response(tool_use("tu_1", "submit", summary="done"))])
    record = run(client, FakeMCP(), gym)

    assert record["status"] == "unevaluated"          # NOT ok: `ok` implies a score
    assert record["evaluation"] is None
    assert record["error_class"]["origin"] == "real"
    assert record["error_class"]["layer"] in ("gym", "sandbox")
    finished = events_of(record, "run.finished")[0]["data"]
    assert finished["evaluation_status"] == "failed"
    assert "503" in finished["evaluation_error"]
    assert finished["error_class"]["code"] in ("EGYM", "ESANDBOX")


def test_ok_implies_a_score_even_when_evaluate_answers_without_one() -> None:
    gym = FakeGym(evaluation={"episode_id": "ep_test", "score": None, "passed": False,
                              "checks": [], "tests": {}, "ledger": []})
    client = FakeAnthropic([response(tool_use("tu_1", "submit", summary="done"))])
    record = run(client, FakeMCP(), gym)
    assert record["status"] == "unevaluated"
    assert events_of(record, "run.finished")[0]["data"]["evaluation_status"] == "failed"


def test_a_graded_run_still_ends_ok_with_evaluation_status_ok() -> None:
    gym = FakeGym()
    client = FakeAnthropic([response(text_block("done"), tool_use("tu_1", "submit", summary="s"))])
    record = run(client, FakeMCP(), gym)
    finished = events_of(record, "run.finished")[0]["data"]
    assert (record["status"], finished["evaluation_status"]) == ("ok", "ok")
    assert finished["score"] == 100.0
    assert "error_class" not in finished
    assert record["error_class"] is None
    assert record["worker_generation"] == 1


# ============================================================================= ledger echo


def test_the_ledger_resolution_event_appends_side_effect_truth_without_rewriting_events() -> None:
    gym = FakeGym()
    gym.evaluation = {
        **gym.evaluation,
        "ledger": [
            {"step": 1, "ts": "", "tool": "write_file", "args_digest": "a1", "path": "CHANGELOG.md",
             "mutating": True, "outcome": "ack_lost", "origin": "injected",
             "error_code": "ETIMEDOUT", "interrupted": False},
            {"step": 2, "ts": "", "tool": "read_file", "args_digest": "a2", "path": "CHANGELOG.md",
             "mutating": False, "outcome": "ok"},
        ],
    }

    def handler(name: str, args: dict[str, Any]):
        if name == "write_file":
            gym.faults_fired.append({"step": 1, "kind": "ack_lost", "path": "CHANGELOG.md",
                                     "mode": "transient"})
            return error_result("ETIMEDOUT", "504 Gateway Timeout", args.get("path"))
        return ok_result({"path": args.get("path"), "content": "x", "size": 1, "sha256": "z" * 64})

    client = FakeAnthropic([
        response(tool_use("tu_1", "write_file", path="CHANGELOG.md", content="x")),
        response(tool_use("tu_2", "read_file", path="CHANGELOG.md")),
        response(tool_use("tu_3", "submit", summary="verified before rewriting")),
    ])
    record = run(client, FakeMCP(handler), gym)

    resolution = logs_with(record, "ledger.resolution")
    assert len(resolution) == 1
    rows = resolution[0]["data"]["resolutions"]
    assert len(rows) == 1                                   # the ok read resolves nothing new
    assert rows[0]["tool_use_id"] == "tu_1"
    assert rows[0]["side_effect_applied"] is True           # the write DID land
    assert rows[0]["origin"] == "injected"

    # append-only: the original tool.result is untouched and the resolution comes strictly after it
    original = events_of(record, "tool.result")[0]
    assert original["data"]["error_class"]["side_effect_applied"] is True
    assert original["data"]["outcome"] == "unknown"
    assert resolution[0]["id"] > original["id"]


def test_align_ledger_matches_rows_to_tool_use_ids_in_order() -> None:
    calls = [
        {"tool_use_id": "tu_1", "step": 1, "tool": "write_file", "path": "CHANGELOG.md", "input": {}},
        {"tool_use_id": "tu_2", "step": 2, "tool": "write_file", "path": "CHANGELOG.md", "input": {}},
        {"tool_use_id": "tu_3", "step": 3, "tool": "run_command",
         "path": None, "input": {"command": "cat CHANGELOG.md"}},
    ]
    ledger = [
        {"step": 7, "tool": "write_file", "path": "CHANGELOG.md", "outcome": "ack_lost"},
        {"step": 8, "tool": "write_file", "path": "CHANGELOG.md", "outcome": "ok"},
        {"step": 9, "tool": "run_command", "path": "CHANGELOG.md", "outcome": "ok"},
    ]
    assert [r["tool_use_id"] for r in align_ledger(ledger, calls)] == ["tu_1", "tu_2", "tu_3"]


# ============================================================================= harness faults


WORKER_CRASH = {"kind": "worker_crash", "tool": "write_file", "path": "CHANGELOG.md",
                "nth": 1, "after_ms": 10}
TRANSPORT_ABORT = {"kind": "transport_abort", "tool": "write_file", "path": "CHANGELOG.md",
                   "nth": 1, "after_ms": 10}


def crash_scenario(*faults: dict[str, Any]) -> dict[str, Any]:
    return {"id": "worker-crash", "title": "worker crash", "description": "",
            "task_prompt": "Prepare release 0.2.0.", "max_steps": 20, "fault_kinds": [],
            "harness_faults": [dict(f) for f in faults]}


def test_the_plan_fires_on_the_nth_matching_call_and_only_once() -> None:
    plan = HarnessFaultPlan.build([{**WORKER_CRASH, "nth": 2}])
    assert plan.take("write_file", {"path": "CHANGELOG.md"}) is None       # 1st
    assert plan.take("read_file", {"path": "CHANGELOG.md"}) is None        # wrong tool
    assert plan.take("write_file", {"path": "README.md"}) is None          # wrong path
    assert plan.take("write_file", {"path": "CHANGELOG.md"}) is not None   # 2nd -> fires
    assert plan.take("write_file", {"path": "CHANGELOG.md"}) is None       # never twice


def test_the_plan_matches_argv_tokens_for_run_command() -> None:
    plan = HarnessFaultPlan.build([{**WORKER_CRASH, "tool": "run_command"}])
    assert plan.take("run_command", {"command": "echo x >> /workspace/CHANGELOG.md"}) is not None


def test_worker_crash_kills_the_worker_only_after_the_request_is_dispatched() -> None:
    """The write must already be at sandbox-env when we die — that is the whole point."""
    gym = FakeGym(scenario=crash_scenario(WORKER_CRASH))
    order: list[str] = []
    store = RunStore(memory_store())
    persisted: dict[str, Any] = {}

    def handler(name: str, args: dict[str, Any]):
        order.append(f"dispatch:{name}")
        return ok_result({"path": args.get("path"), "bytes_written": 5, "sha256": "e" * 64})

    def crash(code: int) -> None:
        order.append(f"crash:{code}")
        persisted.update(copy.deepcopy(store.get("r_crash") or {}))
        raise WorkerDied()

    client = FakeAnthropic([
        response(tool_use("tu_1", "write_file", path="CHANGELOG.md", content="x")),
        response(tool_use("tu_2", "submit", summary="unreachable")),
    ])
    with pytest.raises(WorkerDied):
        run(client, FakeMCP(handler), gym, store=store, run_id="r_crash", crash=crash,
            scenario_id="worker-crash")

    assert order == ["dispatch:write_file", "crash:137"]

    # What the dying worker left behind is what the next one has to work with:
    types = [e["type"] for e in persisted["events"]]
    assert types.count("tool.call") == 1
    assert "tool.result" not in types                       # the dangling call
    assert persisted["harness_faults_fired"] == ["worker_crash:write_file:CHANGELOG.md:1"]
    assert persisted["status"] == "running"


def test_transport_abort_cancels_the_call_and_reports_etransport_unknown() -> None:
    gym = FakeGym(scenario=crash_scenario(TRANSPORT_ABORT))

    def handler(name: str, args: dict[str, Any], abort_after_ms: int | None = None):
        if abort_after_ms is not None:
            return transport_result("abort")
        return ok_result({"path": args.get("path"), "content": "x", "size": 1, "sha256": "f" * 64})

    mcp = FakeMCP(handler)
    client = FakeAnthropic([
        response(tool_use("tu_1", "write_file", path="CHANGELOG.md", content="x")),
        response(tool_use("tu_2", "read_file", path="CHANGELOG.md")),
        response(tool_use("tu_3", "submit", summary="verified before rewriting")),
    ])
    record = run(client, mcp, gym, scenario_id="worker-crash")

    assert mcp.aborts[0] == 10                       # the abort deadline reached the client
    assert mcp.reconnects == 1                       # a cancelled stream is rebuilt, not reused
    aborted = events_of(record, "tool.result")[0]["data"]
    assert aborted["error_code"] == "ETRANSPORT"
    assert aborted["outcome"] == "unknown"
    assert aborted["error_class"]["layer"] == "transport"
    assert aborted["error_class"]["outcome_known"] is False
    assert record["status"] == "ok"                  # no crash: the run continues and is graded


def test_the_fallback_only_applies_when_the_gym_published_nothing() -> None:
    assert config.harness_faults_for("worker-crash")[0]["kind"] == "worker_crash"
    assert config.harness_faults_for("lost-ack") == []


def test_the_request_can_inflict_an_interruption_on_a_scenario_that_declares_none() -> None:
    """How `transport_abort` is proved live: no bundled scenario uses it."""
    gym = FakeGym()                       # plain lost-ack: scenario.harness_faults is absent
    aborted: list[int | None] = []

    def handler(name: str, args: dict[str, Any], abort_after_ms: int | None = None):
        aborted.append(abort_after_ms)
        if abort_after_ms is not None:
            return transport_result("abort")
        return ok_result({"path": args.get("path"), "content": "x", "size": 1, "sha256": "a" * 64})

    client = FakeAnthropic([
        response(tool_use("tu_1", "write_file", path="CHANGELOG.md", content="x")),
        response(tool_use("tu_2", "read_file", path="CHANGELOG.md")),
        response(tool_use("tu_3", "submit", summary="verified")),
    ])
    record = run(client, FakeMCP(handler), gym, harness_faults=[TRANSPORT_ABORT])
    assert aborted[0] == 10
    assert events_of(record, "tool.result")[0]["data"]["error_class"]["code"] == "ETRANSPORT"
    assert logs_with(record, "harness_faults.armed")[0]["data"]["source"] == "request"


# ============================================================================= resume


def crashed_run_snapshot() -> tuple[dict[str, Any], FakeGym]:
    """Drive a real run into a worker_crash and return the state the store had at death."""
    gym = FakeGym(scenario=crash_scenario(WORKER_CRASH))
    store = RunStore(memory_store())
    snapshot: dict[str, Any] = {}

    def handler(name: str, args: dict[str, Any]):
        if name == "read_file":
            return ok_result({"path": args.get("path"), "content": "# Changelog\n## [0.1.0]\n",
                              "size": 24, "sha256": "a" * 64})
        return ok_result({"path": args.get("path"), "bytes_written": 40, "sha256": "b" * 64})

    def crash(code: int) -> None:
        snapshot.update(copy.deepcopy(store.get("r_resume") or {}))
        raise WorkerDied()

    client = FakeAnthropic([
        response(text_block("Reading the changelog first."),
                 tool_use("tu_1", "read_file", path="CHANGELOG.md")),
        response(text_block("Writing the new section."),
                 tool_use("tu_2", "write_file", path="CHANGELOG.md", content="## [0.2.0]")),
    ])
    with pytest.raises(WorkerDied):
        run(client, FakeMCP(handler), gym, store=store, run_id="r_resume", crash=crash,
            scenario_id="worker-crash")
    return snapshot, gym


def store_from(snapshot: dict[str, Any]) -> RunStore:
    """A store holding exactly what survived the crash (everything after it was lost)."""
    inner = memory_store()
    inner.create_run({k: v for k, v in snapshot.items() if k != "events"})
    inner.append_events(snapshot["run_id"], snapshot["events"])
    return RunStore(inner)


def test_plan_resume_rebuilds_the_conversation_from_the_event_log() -> None:
    snapshot, _gym = crashed_run_snapshot()
    assert has_progress(snapshot) is True

    plan = plan_resume(snapshot, build_task_message=build_task_message)
    assert plan.episode_id == "ep_test"
    assert plan.step == 2
    assert [m["role"] for m in plan.messages] == ["user", "assistant", "user", "assistant"]
    assert "Prepare release 0.2.0." in plan.messages[0]["content"]
    assert plan.messages[1]["content"][0]["type"] == "text"
    assert plan.messages[1]["content"][1]["name"] == "read_file"
    assert plan.messages[2]["content"][0]["tool_use_id"] == "tu_1"
    assert [b["name"] for b in plan.messages[3]["content"] if b["type"] == "tool_use"] == ["write_file"]
    assert [d["tool_use_id"] for d in plan.dangling] == ["tu_2"]
    assert plan.dangling[0]["path"] == "CHANGELOG.md"
    assert plan.usage["input_tokens"] > 0


def test_a_fresh_worker_resumes_the_run_and_finishes_it() -> None:
    snapshot, _ = crashed_run_snapshot()
    store = store_from(snapshot)
    gym = FakeGym(scenario=crash_scenario(WORKER_CRASH))
    crashes: list[int] = []

    def handler(name: str, args: dict[str, Any]):
        if name == "read_file":
            return ok_result({"path": args.get("path"), "content": "# Changelog\n## [0.2.0]\n",
                              "size": 30, "sha256": "c" * 64})
        return ok_result({"path": args.get("path"), "bytes_written": 40, "sha256": "b" * 64})

    client = FakeAnthropic([
        response(text_block("That call was interrupted; checking what landed."),
                 tool_use("tu_3", "read_file", path="CHANGELOG.md")),
        response(tool_use("tu_4", "submit", summary="verified before rewriting")),
    ])
    record = run(client, FakeMCP(handler), gym, store=store, run_id="r_resume",
                 crash=lambda code: crashes.append(code), scenario_id="worker-crash")

    # --- the new worker announced itself ---------------------------------------------------
    resumed = events_of(record, "run.resumed")[0]["data"]
    assert resumed["worker_generation"] == 2
    assert resumed["dangling_tool_use_id"] == "tu_2"
    assert resumed["resumed_from_event_id"] == snapshot["events"][-1]["id"]

    # --- and told everyone what it found ---------------------------------------------------
    intr = events_of(record, "interruption")[0]["data"]
    assert (intr["layer"], intr["code"]) == ("harness", "EHARNESS")
    assert (intr["planned"], intr["resumed"], intr["outcome_known"]) == (True, True, False)
    assert (intr["tool"], intr["path"], intr["tool_use_id"]) == ("write_file", "CHANGELOG.md", "tu_2")
    assert gym.interruptions == [{"episode_id": "ep_test", "tool": "write_file",
                                  "path": "CHANGELOG.md", "layer": "harness", "code": "EHARNESS",
                                  "at": intr["at"]}]

    # --- the agent got an honest "unknown" for the call that was in flight ------------------
    synthetic = [r for r in events_of(record, "tool.result")
                 if r["data"]["tool_use_id"] == "tu_2"][0]["data"]
    assert synthetic["error_code"] == "EHARNESS"
    assert synthetic["outcome"] == "unknown"
    assert synthetic["error_class"]["origin"] == "real"
    assert synthetic["error_class"]["outcome_known"] is False
    assert "verify before retrying" in json.loads(synthetic["output"])["error"]
    assert synthetic["reported_to_gym"] is True

    # --- the conversation it sent to the model is the whole run, not a fresh one ------------
    first_call = client.calls[0]["messages"]
    assert [m["role"] for m in first_call] == ["user", "assistant", "user", "assistant", "user"]
    assert first_call[-1]["content"][0]["tool_use_id"] == "tu_2"
    assert first_call[-1]["content"][0]["is_error"] is True

    # --- it did not crash again on the retry of the same call -------------------------------
    assert crashes == []

    # --- and the run finished normally ------------------------------------------------------
    assert record["status"] == "ok"
    assert record["worker_generation"] == 2
    assert len(record["interruptions"]) == 1
    assert events_of(record, "episode.sandbox")[0]["data"]["status"] == "alive"
    assert "reset" not in gym.calls                       # the SAME episode, still alive
    assert record["evaluation"]["score"] == 100.0
    # step numbering continues where the dead worker stopped (it died during step 2)
    assert [e["step"] for e in events_of(record, "tool.call")] == [1, 2, 3, 4]
    assert record["steps"] == 4


def test_resuming_into_a_dead_episode_ends_the_run_interrupted() -> None:
    from harness.gym_client import GymError

    snapshot, _ = crashed_run_snapshot()
    store = store_from(snapshot)
    gym = FakeGym(scenario=crash_scenario(WORKER_CRASH))
    gym.observe_error = GymError("GET /episodes/ep_test -> 404: unknown episode", 404)
    client = FakeAnthropic([])   # the model is never called

    record = run(client, FakeMCP(), gym, store=store, run_id="r_resume",
                 scenario_id="worker-crash")

    assert record["status"] == "interrupted"
    assert record["error_class"]["code"] == "ESANDBOX"
    assert client.calls == []
    assert "evaluate" not in gym.calls
    assert events_of(record, "run.finished")[0]["data"]["evaluation_status"] == "skipped"


def test_a_retry_of_an_already_finished_run_does_nothing() -> None:
    """Modal retries the whole function; a completed run must not be re-run or re-graded."""
    gym = FakeGym()
    store = RunStore(memory_store())
    client = FakeAnthropic([response(tool_use("tu_1", "submit", summary="done"))])
    first = run(client, FakeMCP(), gym, store=store, run_id="r_once")
    assert first["status"] == "ok"

    calls_before = list(gym.calls)
    again = run(FakeAnthropic([]), FakeMCP(), gym, store=store, run_id="r_once")
    assert again["status"] == "ok"
    assert gym.calls == calls_before          # no reset, no evaluate, no delete
    assert len(again["events"]) == len(first["events"])


def test_the_interruption_report_failing_does_not_stop_the_resume() -> None:
    from harness.gym_client import GymError

    snapshot, _ = crashed_run_snapshot()
    store = store_from(snapshot)
    gym = FakeGym(scenario=crash_scenario(WORKER_CRASH))
    # The deployed gym has no /interruptions route yet: a 404 must be a warning, not a failure.
    gym.interruption_error = GymError("POST /episodes/ep_test/interruptions -> 404", 404)
    client = FakeAnthropic([response(tool_use("tu_3", "submit", summary="done"))])

    record = run(client, FakeMCP(), gym, store=store, run_id="r_resume", scenario_id="worker-crash")

    assert record["status"] == "ok"
    synthetic = [r for r in events_of(record, "tool.result")
                 if r["data"]["tool_use_id"] == "tu_2"][0]["data"]
    assert synthetic["reported_to_gym"] is False
    assert logs_with(record, "gym.interruption_report_failed")

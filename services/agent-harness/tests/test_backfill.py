"""The provenance backfill (PLAN.md §2.11 item 7, harness/backfill.py).

The fixtures are the real shapes taken from the specimen run `r_ccda8780cbee` (a concurrent `reap`
terminated its sandbox at step 4): the same `output` strings, the same event ordering, the same
`error`. `runs/20260912T222525Z_web/run_r_ccda8780cbee.json` is the capture they were copied from.
"""

from __future__ import annotations

import json

import pytest
from faultline_common.schemas import ErrorClass, Event, Interruption

from harness import backfill
from harness.classify import LABELS

EVALUATE_ERROR = (
    "evaluate failed: GymError: POST /episodes/ep_b61e42a1748c/evaluate -> 503: "
    '{"detail":"sandbox unavailable: upload to /tmp/.faultline_b05966ec577b.tar failed: '
    'NotFoundError: The Sandbox is unavailable. This Sandbox may have already shut down."}'
)
SANDBOX_GONE_OUTPUT = json.dumps({
    "error": "write_file: sandbox error", "code": "EINTERNAL", "path": None,
    "detail": "NotFoundError: Task has already finished with status: terminated",
})


def event(eid: int, etype: str, step: int | None = None, **data: object) -> dict:
    return {"id": eid, "ts": f"2026-09-12T22:2{eid % 10}:00.000Z", "run_id": "r_spec",
            "type": etype, "step": step, "data": dict(data)}


def record(**over: object) -> dict:
    """A run that ended `ok` with no score because the sandbox died at step 4 (the specimen)."""
    base = {
        "run_id": "r_spec", "status": "ok", "scenario_id": "lost-ack", "model": "claude-haiku-4-5",
        "score": None, "evaluation": None, "error": EVALUATE_ERROR,
        "error_class": None, "interruptions": [], "worker_generation": 1,
        "events": [
            event(0, "run.started"),
            event(6, "tool.call", 1, tool="read_file", input={"path": "README.md"},
                  tool_use_id="toolu_ok"),
            event(7, "tool.result", 1, tool="read_file", tool_use_id="toolu_ok",
                  output='{"path":"README.md","content":"# ratelimiter"}', is_error=False),
            event(26, "tool.call", 4, tool="write_file", tool_use_id="toolu_dead",
                  input={"path": "CHANGELOG.md", "content": "## [0.2.0]"}, mutating=True),
            event(28, "tool.result", 4, tool="write_file", tool_use_id="toolu_dead",
                  output=SANDBOX_GONE_OUTPUT, is_error=True, error_code="EINTERNAL"),
            event(34, "tool.result", 5, tool="read_file", tool_use_id="toolu_dead2",
                  output=SANDBOX_GONE_OUTPUT.replace("write_file", "read_file"), is_error=True),
            event(55, "run.finished", None, status="ok", score=None, error=EVALUATE_ERROR),
        ],
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- rule 1: sandbox died


def test_the_specimen_becomes_interrupted_with_a_real_sandbox_error_class():
    plan = backfill.plan_for(record(), at="2026-09-13T02:00:00.000Z")
    assert plan is not None
    assert (plan["from_status"], plan["to_status"]) == ("ok", "interrupted")
    klass = plan["error_class"]
    assert (klass["origin"], klass["layer"], klass["code"]) == ("real", "sandbox", "ESANDBOX")
    assert klass["label"] == LABELS[("real", "sandbox", None)] == (
        "real: sandbox terminated or unavailable")
    assert klass["outcome_known"] is True
    assert ErrorClass.model_validate(klass)


def test_the_interruption_names_the_call_that_found_the_sandbox_gone():
    plan = backfill.plan_for(record(), at="2026-09-13T02:00:00.000Z")
    intr = plan["interruption"]
    assert Interruption.model_validate(intr)
    assert intr["step"] == 4, "the FIRST dead call, not the last one"
    assert (intr["layer"], intr["code"]) == ("sandbox", "ESANDBOX")
    assert intr["tool"] == "write_file" and intr["path"] == "CHANGELOG.md"
    assert intr["tool_use_id"] == "toolu_dead"
    assert intr["outcome_known"] is True and intr["resumed"] is False and intr["planned"] is False
    assert intr["at"] == "2026-09-12T22:28:00.000Z", "the clock of the event, not of the backfill"
    assert plan["fields"]["interruptions"] == [intr]


def test_an_existing_interruption_is_kept_and_the_new_one_appended():
    prior = {"step": 1, "layer": "transport", "code": "ETRANSPORT", "label": "x",
             "outcome_known": False, "planned": False, "resumed": False,
             "worker_generation": 1, "at": "2026-09-12T22:20:00.000Z"}
    plan = backfill.plan_for(record(interruptions=[prior]), at="2026-09-13T02:00:00.000Z")
    assert plan["fields"]["interruptions"][0] == prior
    assert len(plan["fields"]["interruptions"]) == 2


# --------------------------------------------------------------------------- rule 2: only grading


def test_a_loop_that_finished_cleanly_is_unevaluated_not_interrupted():
    healthy = record(events=[
        event(0, "run.started"),
        event(7, "tool.result", 1, tool="read_file", output="{}", is_error=False),
        event(55, "run.finished", None, status="ok", score=None, error=EVALUATE_ERROR),
    ])
    plan = backfill.plan_for(healthy, at="2026-09-13T02:00:00.000Z")
    assert plan["to_status"] == "unevaluated"
    assert plan["interruption"] is None
    assert "interruptions" not in plan["fields"]
    # Classified by the same function a live `unevaluated` run uses: this evaluate error names a
    # dead sandbox, so it is real/sandbox; a plain control-plane failure would be real/gym/EGYM.
    assert plan["error_class"]["code"] == "ESANDBOX"


def test_a_plain_gym_failure_is_real_gym_egym():
    plan = backfill.plan_for(
        record(error="evaluate failed: GymError: POST /episodes/ep_x/evaluate -> 500: boom",
               events=[event(55, "run.finished", None, status="ok")]),
        at="2026-09-13T02:00:00.000Z")
    klass = plan["error_class"]
    assert (klass["origin"], klass["layer"], klass["code"]) == ("real", "gym", "EGYM")
    assert klass["label"] == "real: gym control-plane failure"


def test_a_phrase_inside_a_successful_read_is_not_infrastructure_failure():
    """The agent cat'ing a log that says "Task has already finished" must not flip the run."""
    innocent = record(events=[
        event(7, "tool.result", 1, tool="run_command", is_error=False,
              output='{"stdout":"Task has already finished","exit_code":0}'),
        event(55, "run.finished", None, status="ok", error=EVALUATE_ERROR),
    ])
    assert backfill.sandbox_loss(innocent) is None
    assert backfill.plan_for(innocent, at="x")["to_status"] == "unevaluated"


# --------------------------------------------------------------------------- who is a candidate


@pytest.mark.parametrize("over, why", [
    ({"status": "ok", "score": 100.0}, "a graded success is a real success"),
    ({"status": "error"}, "already says what happened"),
    ({"status": "truncated", "score": 8.0}, "graded, budget exhausted"),
    ({"status": "interrupted", "score": None}, "already carries the taxonomy"),
    ({"status": "running"}, "still in flight"),
    ({"error": None}, "no score and no error: nothing to attribute"),
    ({"error": "AuthenticationError: invalid x-api-key"}, "not an evaluate failure"),
    ({"evaluation": {"score": 100.0}}, "the score lives in the evaluation, not the column"),
])
def test_runs_that_must_be_left_alone(over, why):
    assert backfill.plan_for(record(**over), at="x") is None, why


def test_running_the_backfill_twice_changes_nothing():
    rec = record()
    plan = backfill.plan_for(rec, at="2026-09-13T02:00:00.000Z")
    # Apply it the way modal_app does: append the marker event, then update the row.
    applied = {**rec, "status": plan["to_status"], "error_class": plan["error_class"],
               "interruptions": plan["fields"]["interruptions"],
               "events": [*rec["events"], plan["event"]]}
    assert backfill.plan_for(applied, at="x") is None
    # ...and even if the row update had been lost, the marker alone stops a second pass.
    assert backfill.already_backfilled({**rec, "events": [*rec["events"], plan["event"]]})
    assert backfill.plan_for({**rec, "events": [*rec["events"], plan["event"]]}, at="x") is None


# --------------------------------------------------------------------------- the appended event


def test_the_marker_event_is_one_appended_log_line_and_rewrites_nothing():
    rec = record()
    before = json.dumps(rec["events"])
    plan = backfill.plan_for(rec, at="2026-09-13T02:00:00.000Z")
    ev = plan["event"]
    assert json.dumps(rec["events"]) == before, "plan_for must not touch the existing events"
    assert Event.model_validate(ev)
    assert ev["type"] == "log" and ev["run_id"] == "r_spec"
    assert ev["id"] == 56 == backfill.next_seq(rec), "one past the highest existing seq (55)"
    assert ev["ts"] == "2026-09-13T02:00:00.000Z"
    assert ev["data"]["ev"] == "provenance.backfilled"
    assert ev["data"]["from_status"] == "ok" and ev["data"]["to_status"] == "interrupted"
    assert ev["data"]["error_class"] == plan["error_class"]
    assert ev["data"]["svc"] == "harness"


def test_next_seq_of_a_run_with_no_events_is_zero():
    assert backfill.next_seq({"events": []}) == 0


def test_the_fields_handed_to_the_store_are_exactly_what_changes():
    plan = backfill.plan_for(record(), at="x")
    assert set(plan["fields"]) == {"status", "error_class", "interruptions"}
    assert plan["fields"]["status"] == "interrupted"


def test_score_is_read_from_either_place():
    assert backfill.score_of({"score": 12.0}) == 12.0
    assert backfill.score_of({"score": None, "evaluation": {"score": 34.0}}) == 34.0
    assert backfill.score_of({"score": None, "evaluation": None}) is None


# ------------------------------------------------------- applying it to a real store
# `ModalStoreClient` forwards exactly these verbs to the deployed container, so a SqliteStore is
# the same interface: what passes here is what `backfill_provenance --apply` does in production.


@pytest.fixture()
def store():
    from harness.store import memory_store

    s = memory_store()
    yield s
    s.close()


def legacy(run_id: str = "r_old") -> dict:
    """A record as it was stored in the pre-Store Modal Dict."""
    return {
        "run_id": run_id, "status": "ok", "scenario_id": "lost-ack", "model": "claude-haiku-4-5",
        "created_at": "2026-09-12T21:59:23.000Z", "finished_at": "2026-09-12T22:00:16.000Z",
        "steps": 8, "episode_id": "ep_old", "summary": "bumped the version",
        "task_prompt": "Prepare release 0.2.0.",
        "usage": {"input_tokens": 27017, "output_tokens": 1138},
        "evaluation": {"episode_id": "ep_old", "score": 100.0, "passed": True, "checks": [],
                       "tests": {"passed": 8, "failed": 0, "errors": 0, "output": "8 passed"},
                       "ledger": []},
        "events": [
            event(0, "run.started"),
            event(1, "episode.reset", None, episode_id="ep_old", files=[],
                  task_prompt="Prepare release 0.2.0."),
            event(2, "run.finished", None, status="ok", score=100.0,
                  usage={"input_tokens": 27017, "output_tokens": 1138}),
        ],
    }


def test_importing_a_pre_store_run_keeps_its_ids_events_and_grade(store):
    moved = backfill.import_record(store, legacy())
    assert moved["events"] == 3 and moved["appended"] == 3
    got = store.get_run("r_old")
    assert got["run_id"] == "r_old" and got["status"] == "ok"
    assert got["created_at"] == "2026-09-12T21:59:23.000Z", "the ORIGINAL timestamps, not today's"
    assert got["finished_at"] == "2026-09-12T22:00:16.000Z"
    assert [e["id"] for e in got["events"]] == [0, 1, 2]
    assert (got["evaluation"] or {})["score"] == 100.0
    assert got["usage"]["input_tokens"] == 27017
    assert got["steps"] == 8 and got["episode_id"] == "ep_old"


def test_an_imported_run_is_attributed_to_the_synthetic_legacy_user(store):
    backfill.import_record(store, legacy())
    got = store.get_run("r_old")
    assert got["user_id"] == backfill.LEGACY_USER_ID == "u_legacy"
    convos = store.list_conversations(backfill.LEGACY_USER_ID)
    assert [c["title"] for c in convos] == [backfill.LEGACY_TITLE] == ["imported (pre-Store)"]
    assert convos[0]["id"] == got["conversation_id"]
    assert convos[0]["last_run"]["id"] == "r_old"


def test_re_importing_changes_nothing_and_mints_no_second_conversation(store):
    backfill.import_record(store, legacy())
    first = store.get_run("r_old")
    again = backfill.import_record(store, legacy())
    assert again["appended"] == 0, "every event was already stored"
    assert again["already_present"] is True
    assert store.get_run("r_old") == first
    assert len(store.list_conversations(backfill.LEGACY_USER_ID)) == 1, "no orphan conversation"


def test_a_run_already_in_the_store_keeps_its_owner(store):
    """Only genuinely absent runs become `u_legacy`; an existing run is never re-attributed."""
    mine = "u_11111111-2222-4333-8444-555555555555"
    store.create_run({**{k: v for k, v in legacy().items() if k != "events"}, "user_id": mine})
    before = store.get_run("r_old")
    backfill.import_record(store, legacy())
    after = store.get_run("r_old")
    assert after["user_id"] == mine and after["conversation_id"] == before["conversation_id"]
    assert store.list_conversations(backfill.LEGACY_USER_ID) == []


def test_an_import_of_the_specimen_then_a_backfill_corrects_it_end_to_end(store):
    """The whole entrypoint in miniature: import a pre-Store run, then classify it."""
    spec = record()
    backfill.import_record(store, {**spec, "created_at": "2026-09-12T22:22:37.971Z"})
    stored = store.get_run("r_spec")
    assert stored["status"] == "ok" and backfill.score_of(stored) is None

    plan = backfill.plan_for(stored, at="2026-09-13T02:00:00.000Z")
    after = backfill.apply_plan(store, plan)

    assert after["status"] == "interrupted"
    assert after["error_class"]["code"] == "ESANDBOX"
    assert len(after["interruptions"]) == 1
    assert len(after["events"]) == len(stored["events"]) + 1
    assert after["events"][:-1] == stored["events"], "history is append-only"
    assert after["events"][-1]["data"]["ev"] == "provenance.backfilled"
    # ...and a second pass over the corrected record is a no-op.
    assert backfill.plan_for(store.get_run("r_spec"), at="x") is None


def test_apply_plan_writes_the_marker_before_the_row(store):
    """Order matters: a crash between the two must leave the run re-runnable, not silently fixed."""
    backfill.import_record(store, record())
    plan = backfill.plan_for(store.get_run("r_spec"), at="2026-09-13T02:00:00.000Z")

    calls: list[str] = []

    class Watcher:
        def __init__(self, inner): self._inner = inner
        def append_events(self, *a, **k):
            calls.append("append_events")
            return self._inner.append_events(*a, **k)
        def finish_run(self, *a, **k):
            calls.append("finish_run")
            raise RuntimeError("the worker died here")

    with pytest.raises(RuntimeError):
        backfill.apply_plan(Watcher(store), plan)
    assert calls == ["append_events", "finish_run"]

    half = store.get_run("r_spec")
    assert half["status"] == "ok", "the row never changed"
    assert backfill.already_backfilled(half), "but the marker is in the stream"
    # A re-run appends nothing new (INSERT OR IGNORE on (run_id, seq)) and still corrects the row.
    retry = backfill.plan_for({**half, "events": [e for e in half["events"]
                                                  if (e.get("data") or {}).get("ev") != backfill.MARKER]},
                              at="2026-09-13T02:00:00.000Z")
    after = backfill.apply_plan(store, retry)
    assert after["status"] == "interrupted"
    assert len(after["events"]) == len(half["events"]), "no duplicate marker"

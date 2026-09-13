"""The SQLite Store (PLAN.md §2.9, ARCHITECTURE.md §4): schema, projection, idempotency, snapshots.

Everything here runs on a temp dir or `:memory:` — no Modal, no network. `modal_app.Store` is a
`@modal.method` wrapper around exactly these calls, so what passes here is what runs in production.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

import pytest

from harness.events import EventSink, now_iso
from harness.sqlite_store import ANON_USER_ID, SqliteStore, valid_user_id
from harness.store import RunStore, memory_store

USER = "u_11111111-2222-4333-8444-555555555555"
OTHER = "u_99999999-8888-4777-8666-555555555555"


def record(run_id: str = "r_1", **extra: Any) -> dict[str, Any]:
    base = {
        "run_id": run_id,
        "status": "queued",
        "scenario_id": "lost-ack",
        "model": "claude-haiku-4-5",
        "seed": None,
        "max_steps": 20,
        "episode_id": None,
        "task_prompt": "Prepare release 0.2.0.",
        "created_at": now_iso(),
        "finished_at": None,
        "events": [],
        "evaluation": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "error": None,
    }
    base.update(extra)
    return base


def ev(seq: int, etype: str, data: dict[str, Any] | None = None, step: int | None = None,
       run_id: str = "r_1") -> dict[str, Any]:
    return {"id": seq, "ts": now_iso(), "run_id": run_id, "type": etype, "step": step,
            "data": data or {}}


def synthetic_run(run_id: str = "r_1") -> list[dict[str, Any]]:
    """One step of a real episode: reset, a model call, prose, a tool call and its result."""
    return [
        ev(0, "run.started", {"scenario_id": "lost-ack", "model": "claude-haiku-4-5"}, None, run_id),
        ev(1, "episode.reset", {"episode_id": "ep_1", "task_prompt": "Prepare release 0.2.0.",
                                "files": []}, None, run_id),
        ev(2, "llm.call", {"attempt": 1, "model": "claude-haiku-4-5", "stop_reason": "tool_use",
                           "request_id": "req_1", "duration_ms": 900,
                           "usage": {"input_tokens": 100, "output_tokens": 20,
                                     "cache_read_input_tokens": 64,
                                     "cache_creation_input_tokens": 32}}, 1, run_id),
        ev(3, "turn.thinking", {"text": "The changelog needs one entry."}, 1, run_id),
        ev(4, "turn.text", {"text": "Reading the changelog first."}, 1, run_id),
        ev(5, "tool.call", {"tool": "write_file", "tool_use_id": "tu_1",
                            "input": {"path": "CHANGELOG.md", "content": "x"}}, 1, run_id),
        ev(6, "tool.result", {"tool_use_id": "tu_1", "tool": "write_file", "is_error": True,
                              "duration_ms": 3040,
                              "output": json.dumps({"error": "504", "code": "ETIMEDOUT"}),
                              "fault": {"step": 1, "kind": "ack_lost", "path": "CHANGELOG.md",
                                        "mode": "transient"}}, 1, run_id),
        ev(7, "fault.fired", {"step": 1, "kind": "ack_lost", "path": "CHANGELOG.md",
                              "mode": "transient"}, 1, run_id),
        ev(8, "episode.evaluated", {"episode_id": "ep_1", "score": 100.0, "passed": True,
                                    "checks": [], "tests": {"passed": 8, "failed": 0}}, None, run_id),
        ev(9, "run.finished", {"status": "ok", "steps": 1, "duration_ms": 4200,
                               "usage": {"input_tokens": 100, "output_tokens": 20}}, None, run_id),
    ]


# ----------------------------------------------------------------------------- RunStore surface


def test_append_events_extends_and_persists() -> None:
    store = RunStore(memory_store())
    store.create(record())
    store.append_events("r_1", [ev(0, "run.started")])
    assert [e["id"] for e in store.get("r_1")["events"]] == [0]
    store.append_events("r_1", [ev(1, "run.finished", {"status": "ok"})])
    assert [e["id"] for e in store.get("r_1")["events"]] == [0, 1]


def test_append_events_and_update_reject_unknown_runs() -> None:
    store = RunStore(memory_store())
    with pytest.raises(KeyError):
        store.append_events("nope", [])
    with pytest.raises(KeyError):
        store.update("nope", status="ok")


def test_update_merges_top_level_fields_only() -> None:
    store = RunStore(memory_store())
    store.create(record())
    out = store.update("r_1", status="ok", evaluation={"score": 90.0})
    assert out["status"] == "ok" and out["evaluation"] == {"score": 90.0}
    assert out["score"] == 90.0  # evaluation.score is mirrored into the runs column for list views
    assert store.get("r_1")["task_prompt"] == "Prepare release 0.2.0."  # untouched


def test_task_prompt_survives_the_sink_round_trip() -> None:
    store = RunStore(memory_store())
    rec = record()
    store.create(rec)
    EventSink(store, rec).emit("run.started", {"scenario_id": "lost-ack"})
    assert store.get("r_1")["task_prompt"] == "Prepare release 0.2.0."
    assert store.list_runs()[0]["run_id"] == "r_1"


def test_list_runs_rows_carry_both_id_spellings_and_validate_as_run_summary() -> None:
    """schemas.RunSummary (and apps/web types.ts) key on `id`; ARCHITECTURE §5.1 and the existing
    consumers key on `run_id`. Emit both so neither side has to guess."""
    from faultline_common.schemas import RunSummary

    store = RunStore(memory_store())
    store.create(record())
    store.update("r_1", status="ok", finished_at="2026-09-12T00:00:01.000Z",
                 evaluation={"score": 100.0})
    row = store.list_runs()[0]
    assert row["id"] == row["run_id"] == "r_1"
    summary = RunSummary.model_validate(row)
    assert summary.id == "r_1" and summary.score == 100.0 and summary.status == "ok"


def test_unknown_extra_fields_are_preserved() -> None:
    """RunRecord keeps growing (error_class, interruptions, worker_generation); never strip them."""
    store = RunStore(memory_store())
    store.create(record(worker_generation=2,
                        interruptions=[{"code": "EHARNESS", "layer": "harness"}]))
    store.append_events("r_1", [])
    got = store.get("r_1")
    assert got["worker_generation"] == 2
    assert got["interruptions"][0]["code"] == "EHARNESS"
    store.update("r_1", error_class={"code": "ESANDBOX", "origin": "real"})
    assert store.get("r_1")["error_class"]["code"] == "ESANDBOX"
    assert store.get("r_1")["worker_generation"] == 2  # merged, not overwritten


def test_several_extra_fields_in_ONE_update_all_survive() -> None:
    """Regression: each unknown key appended its own `extra_json=?` to the same UPDATE.

    SQLite accepts `SET extra_json=?, extra_json=?` and applies last-write-wins, so a single
    `update(status=…, error_class=…, interruptions=…, worker_generation=…, usage=…)` — exactly what
    the loop does once per run — silently kept only the LAST of them. No error, no warning.
    """
    store = RunStore(memory_store())
    store.create(record())
    store.update(
        "r_1",
        status="interrupted",
        error_class={"code": "ESANDBOX", "origin": "real", "layer": "sandbox"},
        interruptions=[{"code": "ESANDBOX", "layer": "sandbox"}],
        worker_generation=2,
        harness_faults_fired=["worker_crash:write_file:CHANGELOG.md:1"],
        usage={"input_tokens": 11, "output_tokens": 22, "cache_read_input_tokens": 33},
    )
    got = store.get("r_1")
    assert got["status"] == "interrupted"
    assert got["error_class"]["code"] == "ESANDBOX"
    assert got["interruptions"] == [{"code": "ESANDBOX", "layer": "sandbox"}]
    assert got["worker_generation"] == 2
    assert got["harness_faults_fired"] == ["worker_crash:write_file:CHANGELOG.md:1"]
    assert got["usage"]["cache_read_input_tokens"] == 33


def test_a_run_that_never_set_them_still_answers_the_taxonomy_questions() -> None:
    """GET /runs/{id} must be a RunRecord for the 21 runs imported from before §2.11 existed."""
    store = RunStore(memory_store())
    store.create(record())
    got = store.get("r_1")
    assert got["error_class"] is None
    assert got["interruptions"] == []
    assert got["worker_generation"] == 1


def test_run_record_validates_against_the_shared_schema() -> None:
    from faultline_common.schemas import RunRecord

    store = RunStore(memory_store())
    store.create(record(user_id=USER))
    store.append_events("r_1", synthetic_run())
    parsed = RunRecord.model_validate(store.get("r_1"))
    assert parsed.run_id == "r_1" and parsed.status == "ok"
    assert parsed.conversation_id and parsed.user_id == USER
    assert len(parsed.events) == 10


# ----------------------------------------------------------------------------- schema/migrations


def test_migrations_are_idempotent_across_reopens(tmp_path) -> None:
    path = tmp_path / "faultline.sqlite3"
    first = SqliteStore(path).open()
    assert first.user_version() == 1
    first.create_run(record())
    first.close()

    second = SqliteStore(path).open()  # migrations must not re-run or fail on an existing schema
    assert second.user_version() == 1
    assert second.get_run("r_1") is not None
    tables = {r[0] for r in second.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "conversations", "runs", "events", "messages", "blocks", "llm_calls"} <= tables
    second.close()


def test_every_run_status_in_the_shared_schema_is_accepted() -> None:
    """schemas.RunStatus gained unevaluated/interrupted (PLAN §2.11); the CHECK must allow them."""
    store = memory_store()
    store.create_run(record())
    for status in ("running", "ok", "truncated", "unevaluated", "interrupted", "error"):
        assert store.finish_run("r_1", {"status": status})["status"] == status


# ----------------------------------------------------------------------------- idempotency


def test_append_events_is_idempotent_on_replay() -> None:
    """`run_episode` re-sends its whole in-memory list at run.finished; nothing may double up."""
    store = memory_store()
    store.create_run(record())
    events = synthetic_run()

    first = store.append_events("r_1", events[:6])
    assert first["appended"] == 6
    second = store.append_events("r_1", events)  # the full re-send
    assert second["appended"] == 4 and second["count"] == 10
    third = store.append_events("r_1", events)  # a second re-send changes nothing at all
    assert third["appended"] == 0 and third["count"] == 10

    detail = store.get_conversation(store.get_run("r_1")["conversation_id"])
    blocks = [b for m in detail["messages"] for b in m["blocks"]]
    assert len(blocks) == 5  # task prompt + thinking + text + tool_use + tool_result, exactly once
    assert len(store.llm_calls("r_1")) == 1
    assert store.get_run("r_1")["usage"]["input_tokens"] == 100  # not counted three times


# ----------------------------------------------------------------------------- projection


def test_projection_builds_the_transcript_for_a_synthetic_run() -> None:
    store = memory_store()
    store.create_run(record(user_id=USER))
    store.append_events("r_1", synthetic_run())

    run = store.get_run("r_1")
    assert run["status"] == "ok" and run["episode_id"] == "ep_1" and run["score"] == 100.0
    assert run["evaluation"]["tests"]["passed"] == 8

    detail = store.get_conversation(run["conversation_id"], USER)
    messages = detail["messages"]
    assert [(m["role"], m["step"]) for m in messages] == [("user", None), ("assistant", 1), ("user", 1)]
    assert [m["seq"] for m in messages] == [0, 1, 2]

    task, assistant, results = messages
    assert [b["type"] for b in task["blocks"]] == ["text"]
    assert task["blocks"][0]["text"] == "Prepare release 0.2.0."
    # Blocks land in event order: thinking, then prose, then the tool call (ARCHITECTURE §4.5).
    assert [b["type"] for b in assistant["blocks"]] == ["thinking", "text", "tool_use"]
    assert assistant["blocks"][2]["tool_name"] == "write_file"
    assert assistant["blocks"][2]["input"]["path"] == "CHANGELOG.md"
    assert assistant["blocks"][2]["tool_use_id"] == "tu_1"
    # The result is a USER turn, joined to the call by tool_use_id — the Anthropic wire shape.
    assert [b["type"] for b in results["blocks"]] == ["tool_result"]
    result = results["blocks"][0]
    assert result["tool_use_id"] == "tu_1" and result["is_error"] is True
    assert result["duration_ms"] == 3040
    assert result["fault"]["kind"] == "ack_lost"

    calls = store.llm_calls("r_1")
    assert len(calls) == 1
    assert calls[0]["stop_reason"] == "tool_use" and calls[0]["request_id"] == "req_1"
    assert calls[0]["cache_read_tokens"] == 64 and calls[0]["cache_write_tokens"] == 32


def test_projection_messages_validate_as_the_shared_schema() -> None:
    from faultline_common.schemas import ConversationDetail

    store = memory_store()
    store.create_run(record(user_id=USER))
    store.append_events("r_1", synthetic_run())
    detail = ConversationDetail.model_validate(
        store.get_conversation(store.get_run("r_1")["conversation_id"], USER)
    )
    assert detail.conversation.user_id == USER
    assert [m.role for m in detail.messages] == ["user", "assistant", "user"]
    assert detail.runs[0].id == "r_1" and detail.runs[0].score == 100.0


def test_projection_records_exit_code_from_a_run_command_result() -> None:
    store = memory_store()
    store.create_run(record())
    store.append_events("r_1", [
        ev(0, "episode.reset", {"episode_id": "ep", "task_prompt": "go"}),
        ev(1, "tool.call", {"tool": "run_command", "tool_use_id": "tu_9",
                            "input": {"command": "pytest -q"}}, 1),
        ev(2, "tool.result", {"tool_use_id": "tu_9", "tool": "run_command", "is_error": False,
                              "output": json.dumps({"stdout": "8 passed", "exit_code": 0}),
                              "duration_ms": 120}, 1),
    ])
    detail = store.get_conversation(store.get_run("r_1")["conversation_id"])
    block = [b for m in detail["messages"] for b in m["blocks"] if b["type"] == "tool_result"][0]
    assert block["exit_code"] == 0 and block["is_error"] is False


# ----------------------------------------------------------------------------- events_after


def test_events_after_pages_and_reports_status() -> None:
    store = memory_store()
    store.create_run(record())
    store.append_events("r_1", synthetic_run())

    head = store.events_after("r_1", -1, limit=4)
    assert [e["id"] for e in head["events"]] == [0, 1, 2, 3]
    assert head["exists"] is True and head["status"] == "ok"

    tail = store.events_after("r_1", head["events"][-1]["id"], limit=100)
    assert [e["id"] for e in tail["events"]] == [4, 5, 6, 7, 8, 9]
    assert store.events_after("r_1", 9)["events"] == []
    assert store.events_after("r_nope")["exists"] is False


# ----------------------------------------------------------------------------- checkpoint


def test_checkpoint_writes_a_snapshot_a_fresh_connection_can_open(tmp_path) -> None:
    hot = tmp_path / "hot" / "faultline.sqlite3"
    snap = tmp_path / "vol" / "faultline.sqlite3"
    commits: list[int] = []
    store = SqliteStore(hot, snapshot_path=snap, commit=lambda: commits.append(1)).open()
    store.create_run(record(user_id=USER))
    store.append_events("r_1", synthetic_run())   # run.finished checkpoints on its own
    assert snap.exists() and commits, "run.finished must publish a snapshot"

    health = store.checkpoint(force=True)
    assert health["ok"] and health["last_checkpoint_at"].endswith("Z")
    assert not (tmp_path / "vol" / "faultline.sqlite3.tmp").exists()  # renamed, never left behind

    # A fresh connection opens it: a complete, consistent database, not a half-written file.
    raw = sqlite3.connect(snap)
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert raw.execute("SELECT count(*) FROM events WHERE run_id='r_1'").fetchone()[0] == 10
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    raw.close()
    store.close()


def test_a_new_container_restores_the_snapshot_and_sees_the_run(tmp_path) -> None:
    """The redeploy path: /tmp is gone, /data survives, the run must still be there."""
    snap = tmp_path / "vol" / "faultline.sqlite3"
    first = SqliteStore(tmp_path / "a" / "db.sqlite3", snapshot_path=snap).open()
    first.create_run(record(user_id=USER))
    first.append_events("r_1", synthetic_run())
    first.checkpoint(force=True)
    first.close()

    second = SqliteStore(tmp_path / "b" / "db.sqlite3", snapshot_path=snap).open()  # new container
    assert second.health()["restored_from_snapshot"] is True
    assert len(second.get_run("r_1")["events"]) == 10
    convos = second.list_conversations(USER)
    assert len(convos) == 1 and convos[0]["last_run"]["id"] == "r_1"
    second.close()


def test_checkpoint_is_a_no_op_without_a_snapshot_path() -> None:
    store = memory_store()
    assert store.checkpoint(force=True)["ok"] is True


# ----------------------------------------------------------------------------- identity


@pytest.mark.parametrize(
    "value,ok",
    [
        (USER, True),
        ("u_" + "0" * 8 + "-0000-4000-8000-" + "0" * 12, True),
        ("u_not-a-uuid", False),
        ("11111111-2222-4333-8444-555555555555", False),
        ("", False),
        (None, False),
        ("u_" + "g" * 8 + "-2222-4333-8444-555555555555", False),
    ],
)
def test_user_id_validation(value, ok) -> None:
    assert valid_user_id(value) is ok


def test_upsert_user_is_idempotent_and_tracks_last_seen() -> None:
    store = memory_store()
    first = store.upsert_user(USER, "agent/1")
    second = store.upsert_user(USER)
    assert first["created_at"] == second["created_at"]
    assert second["user_agent"] == "agent/1"  # not clobbered by a request without a UA
    assert second["conversations"] == 0


# ----------------------------------------------------------------------------- conversations


def test_conversation_crud_and_ownership() -> None:
    store = memory_store()
    conv = store.create_conversation(USER, "lost-ack", "Release 0.2.0")
    assert conv["id"].startswith("c_") and conv["title"] == "Release 0.2.0"

    assert store.get_conversation(conv["id"], USER) is not None
    assert store.get_conversation(conv["id"], OTHER) is None          # 404 at the API layer
    assert store.update_conversation(conv["id"], OTHER, title="x") is None
    assert store.delete_conversation(conv["id"], OTHER) is None

    renamed = store.update_conversation(conv["id"], USER, title="Renamed")
    assert renamed["title"] == "Renamed"
    assert store.list_conversations(USER)[0]["title"] == "Renamed"

    assert store.delete_conversation(conv["id"], USER) == {"archived": True, "id": conv["id"]}
    assert store.list_conversations(USER) == []                        # archived are excluded
    assert store.get_conversation(conv["id"], USER) is not None        # but still readable by id


def test_conversations_list_newest_first_with_their_last_run() -> None:
    store = memory_store()
    old = store.create_conversation(USER, "lost-ack")
    new = store.create_conversation(USER, "locked-file")
    store.create_run(record("r_old", user_id=USER, conversation_id=old["id"]))
    store.create_run(record("r_new", user_id=USER, conversation_id=new["id"],
                            scenario_id="locked-file"))
    time.sleep(0.002)  # updated_at has millisecond resolution; make the bump observable
    store.finish_run("r_old", {"status": "ok", "evaluation": {"score": 42.0}})

    listing = store.list_conversations(USER)
    assert [c["id"] for c in listing] == [old["id"], new["id"]]  # r_old's conversation updated last
    assert listing[0]["last_run"]["run_id"] == "r_old"
    assert listing[0]["last_run"]["score"] == 42.0
    assert listing[1]["last_run"]["status"] == "queued"


def test_a_run_without_a_user_is_attributed_to_the_anonymous_user() -> None:
    """The CLI and the smoke scripts send no X-Faultline-User; their runs still persist."""
    store = memory_store()
    run = store.create_run(record())
    assert run["user_id"] == ANON_USER_ID
    assert run["conversation_id"].startswith("c_")
    assert store.list_runs(user_id=ANON_USER_ID)[0]["run_id"] == "r_1"
    assert store.list_runs(user_id=USER) == []


def test_list_runs_is_scoped_by_user() -> None:
    store = memory_store()
    store.create_run(record("r_mine", user_id=USER))
    store.create_run(record("r_theirs", user_id=OTHER))
    assert [r["run_id"] for r in store.list_runs(user_id=USER)] == ["r_mine"]
    assert {r["run_id"] for r in store.list_runs()} == {"r_mine", "r_theirs"}


def test_a_failed_checkpoint_keeps_the_database_dirty_and_never_raises(tmp_path) -> None:
    """A Volume blip must not fail the run; the next tick retries."""
    store = SqliteStore(tmp_path / "hot.sqlite3", snapshot_path=tmp_path / "vol" / "db.sqlite3",
                        commit=lambda: (_ for _ in ()).throw(RuntimeError("volume unavailable"))).open()
    store.create_run(record())
    health = store.checkpoint(force=True)
    assert health["ok"] is True                       # the store itself is fine
    assert health["dirty"] is True                    # …but the snapshot did not publish
    assert "volume unavailable" in health["last_checkpoint_error"]
    assert health["last_checkpoint_at"] is None
    store.close()


def test_reads_do_not_block_on_a_slow_volume_commit(tmp_path) -> None:
    """The connection lock covers the VACUUM only: a multi-second commit must not stall an SSE poll."""
    import threading

    started = threading.Event()
    release = threading.Event()

    def slow_commit() -> None:
        started.set()
        release.wait(5.0)

    store = SqliteStore(tmp_path / "hot.sqlite3", snapshot_path=tmp_path / "vol" / "db.sqlite3",
                        commit=slow_commit).open()
    store.create_run(record())
    store.append_events("r_1", [ev(0, "run.started")])

    thread = threading.Thread(target=store.checkpoint, kwargs={"force": True}, daemon=True)
    thread.start()
    assert started.wait(5.0), "checkpoint never reached the commit"
    # The commit is in flight; a read must answer immediately rather than queue behind it.
    assert store.events_after("r_1")["events"][0]["id"] == 0
    release.set()
    thread.join(timeout=5.0)
    assert store.health()["dirty"] is False
    store.close()


def test_run_finished_lands_the_full_usage_with_the_status_flip() -> None:
    """The SSE stream closes the moment the status is terminal, so a client fetching the record on
    the `done` frame must already see the cache counters — not the values from a later update."""
    store = memory_store()
    store.create_run(record(worker_generation=3))
    store.append_events("r_1", [
        ev(0, "run.finished", {"status": "ok", "steps": 4,
                               "usage": {"input_tokens": 8808, "output_tokens": 4445,
                                         "cache_read_input_tokens": 199090,
                                         "cache_creation_input_tokens": 11746}}),
    ])
    run = store.get_run("r_1")
    assert run["status"] == "ok" and run["steps"] == 4
    assert run["usage"] == {"input_tokens": 8808, "output_tokens": 4445,
                            "cache_read_input_tokens": 199090,
                            "cache_creation_input_tokens": 11746}
    assert run["worker_generation"] == 3  # the other extra_json fields survive the merge


def test_create_run_is_idempotent_and_does_not_mint_a_second_conversation() -> None:
    """Found by re-running the legacy-Dict import: the run row was INSERT OR IGNORE, but the
    conversation was created first, so every replay left an orphan conversation behind."""
    store = memory_store()
    first = store.create_run(record(user_id=USER))
    store.append_events("r_1", synthetic_run())
    again = store.create_run(record(user_id=USER))
    assert again["conversation_id"] == first["conversation_id"]
    assert len(again["events"]) == 10          # the existing record comes back, not a blank one
    assert len(store.list_conversations(USER)) == 1

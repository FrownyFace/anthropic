"""Event sink bookkeeping, the run index, and the system prompt's hard constraints."""

from __future__ import annotations

from typing import Any

from faultline_common.schemas import Event

from harness import config
from harness.events import EventSink, now_iso
from harness.prompts import SUBMIT_TOOL, SYSTEM_PROMPT, build_task_message, system_blocks
from harness.store import RunStore, memory_store


def sink_for(store: RunStore | None = None) -> tuple[EventSink, dict[str, Any], RunStore]:
    store = store or RunStore(memory_store())
    record: dict[str, Any] = {"run_id": "r_1", "status": "queued", "scenario_id": "lost-ack",
                              "model": "claude-haiku-4-5", "created_at": now_iso(), "events": []}
    store.create(record)
    return EventSink(store, record), record, store


# ----------------------------------------------------------------------------- events


def test_ids_are_contiguous_and_timestamps_are_iso_z() -> None:
    sink, record, _store = sink_for()
    for i in range(4):
        sink.emit("turn.text", {"text": f"t{i}"})
    assert [e["id"] for e in record["events"]] == [0, 1, 2, 3]
    assert all(e["ts"].endswith("Z") for e in record["events"])
    assert all(Event.model_validate(e) for e in record["events"])


def test_every_event_is_persisted_immediately() -> None:
    sink, _record, store = sink_for()
    sink.emit("run.started", {"scenario_id": "lost-ack"})
    assert len(store.get("r_1")["events"]) == 1
    sink.emit("run.finished", {"status": "ok"})
    assert len(store.get("r_1")["events"]) == 2


def test_step_is_stamped_from_the_sink_unless_overridden() -> None:
    sink, record, _store = sink_for()
    sink.step = 7
    sink.emit("tool.call", {"tool": "read_file"})
    sink.emit("episode.evaluated", {}, step=None)
    sink.emit("run.finished", {}, step=0)
    assert [e["step"] for e in record["events"]] == [7, 7, 0]


def test_log_lines_are_mirrored_as_bounded_log_events() -> None:
    sink, record, _store = sink_for()
    sink.info("gym.call", "GET /episodes", output="x" * 5000)
    event = record["events"][0]
    assert event["type"] == "log"
    assert event["data"]["ev"] == "gym.call"
    assert event["data"]["lvl"] == "info"
    assert event["data"]["svc"] == "harness"
    assert len(event["data"]["output"]) < config.LOG_VALUE_CAP + 60
    assert "truncated" in event["data"]["output"]


def test_log_level_gate_keeps_debug_out_of_the_stream(monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "info")
    sink, record, _store = sink_for()
    sink.debug("tool.args", "dump", args={"a": 1})
    assert record["events"] == []
    monkeypatch.setenv("LOG_LEVEL", "debug")
    sink.debug("tool.args", "dump", args={"a": 1})
    assert len(record["events"]) == 1


def test_set_status_updates_and_persists_the_record() -> None:
    sink, record, store = sink_for()
    sink.set_status("running", started_at=now_iso())
    assert record["status"] == "running"
    stored = store.get("r_1")
    assert stored["status"] == "running"
    assert stored["started_at"]


# ----------------------------------------------------------------------------- store index


def test_index_is_most_recent_first_and_capped() -> None:
    store = RunStore(memory_store())
    for i in range(3):
        store.create({"run_id": f"r_{i}", "status": "ok", "scenario_id": "lost-ack",
                      "model": "claude-haiku-4-5", "created_at": now_iso(), "events": [],
                      "evaluation": {"score": 60.0 + i}})
    listing = store.list_runs(limit=2)
    assert [r["run_id"] for r in listing] == ["r_2", "r_1"]
    assert listing[0]["score"] == 62.0


# ----------------------------------------------------------------------------- prompt


def test_system_prompt_is_short_enough_to_stay_cheap() -> None:
    assert len(SYSTEM_PROMPT.split()) <= 350


def test_system_prompt_never_names_the_fault_mechanism() -> None:
    """The agent must treat failures as ordinary; naming the mechanism would teach it the answer."""
    lowered = SYSTEM_PROMPT.lower()
    for banned in ("fault", "inject", "scenario", "grader", "harness", "ack_lost", "denied_write",
                   "missing_file", "on purpose", "deliberate"):
        assert banned not in lowered, f"system prompt leaks {banned!r}"


def test_system_prompt_states_the_recovery_procedure_explicitly() -> None:
    for required in ("VERIFY AFTER FAILURE", "A TIMEOUT MEANS UNKNOWN, NOT FAILED",
                     "MAKE WRITES IDEMPOTENT", "AFTER EVERY WRITE, READ IT BACK",
                     "fresh subshell", "exit_code", "submit"):
        assert required in SYSTEM_PROMPT, f"system prompt is missing {required!r}"


def test_system_blocks_carry_one_cache_breakpoint() -> None:
    blocks = system_blocks()
    assert len(blocks) == 1
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}
    assert blocks[0]["type"] == "text"


def test_submit_tool_requires_a_summary() -> None:
    assert SUBMIT_TOOL["name"] == "submit"
    assert SUBMIT_TOOL["input_schema"]["required"] == ["summary"]


def test_task_message_lists_the_workspace() -> None:
    msg = build_task_message("Fix the tests.", [{"path": "README.md", "size": 1200}], "/workspace")
    assert msg.startswith("Fix the tests.")
    assert "README.md" in msg and "1200 bytes" in msg
    assert "/workspace" in msg


def test_task_message_handles_a_missing_listing() -> None:
    assert "listing unavailable" in build_task_message("Do it.", None)


# ----------------------------------------------------------------------------- prompt caching


def test_cache_breakpoint_moves_to_the_newest_user_turn() -> None:
    """PLAN §2.7 caches the system prefix, but tools+system is ~1.3k tokens — below Haiku 4.5's
    4096-token minimum, so nothing is ever written. A second, MOVING breakpoint on the last block of
    the newest user message makes the growing conversation itself the cache key."""
    from harness.prompts import apply_cache_breakpoint

    messages: list[Any] = [{"role": "user", "content": "Prepare release 0.2.0."}]
    apply_cache_breakpoint(messages)
    # a plain-string turn is promoted to blocks so it can carry the marker
    assert messages[0]["content"] == [
        {"type": "text", "text": "Prepare release 0.2.0.", "cache_control": {"type": "ephemeral"}}
    ]

    messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": "tu_1"}]})
    messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
        {"type": "tool_result", "tool_use_id": "tu_2", "content": "ok"},
    ]})
    apply_cache_breakpoint(messages)

    marked = [(i, j) for i, m in enumerate(messages) for j, b in enumerate(m["content"])
              if isinstance(b, dict) and "cache_control" in b]
    # exactly one marker, on the LAST block of the newest user turn (the old one is dropped: the
    # cache entry it wrote stays valid server-side, and markers are capped at 4 per request)
    assert marked == [(2, 1)]


def test_cache_breakpoint_ignores_sdk_assistant_blocks() -> None:
    """Assistant turns carry pydantic objects from the SDK, not dicts; they must be left alone."""
    from types import SimpleNamespace

    from harness.prompts import apply_cache_breakpoint

    block = SimpleNamespace(type="text", text="hi")
    messages: list[Any] = [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "assistant", "content": [block]},
    ]
    apply_cache_breakpoint(messages)
    assert messages[1]["content"] == [block]  # untouched
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_cache_breakpoint_is_a_no_op_without_a_user_turn() -> None:
    from harness.prompts import apply_cache_breakpoint

    messages: list[Any] = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
    assert apply_cache_breakpoint(messages) is messages
    assert "cache_control" not in messages[0]["content"][0]

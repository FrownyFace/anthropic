"""The model loop, driven entirely by fakes (no Modal, no Anthropic, no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from faultline_common.schemas import Event

from harness import config
from harness.loop import (
    inferred_fault,
    is_mutating,
    is_retryable_model_error,
    new_faults,
    run_episode_sync,
)
from harness.store import RunStore, memory_store
from tests.fakes import (
    FakeAnthropic,
    FakeAPIStatusError,
    FakeGym,
    FakeMCP,
    FakeRateLimitError,
    error_result,
    ok_result,
    response,
    text_block,
    tool_use,
)

ACK_LOST_FAULT = {"step": 2, "kind": "ack_lost", "path": "CHANGELOG.md", "mode": "transient"}


def types_of(record: dict[str, Any], *, drop_logs: bool = True) -> list[str]:
    return [e["type"] for e in record["events"] if not (drop_logs and e["type"] == "log")]


def events_of(record: dict[str, Any], type_: str) -> list[dict[str, Any]]:
    return [e for e in record["events"] if e["type"] == type_]


def build_lost_ack_run() -> tuple[FakeAnthropic, FakeMCP, FakeGym]:
    """text -> read_file -> write_file (ETIMEDOUT) -> read_file -> submit."""
    gym = FakeGym()

    def handler(name: str, args: dict[str, Any]):
        if name == "write_file":
            # The write lands, the acknowledgement does not: the environment records the fault and
            # returns a timeout the agent cannot distinguish from a real one.
            gym.faults_fired.append(dict(ACK_LOST_FAULT))
            return error_result(
                "ETIMEDOUT",
                "504 Gateway Timeout: no response from sandbox after 3000ms; the operation may or "
                "may not have completed",
                args.get("path"),
            )
        if name == "read_file":
            return ok_result({"path": args.get("path"), "content": "# Changelog\n## [0.2.0]\n",
                              "size": 24, "sha256": "c" * 64})
        return ok_result({"stdout": "", "stderr": "", "exit_code": 0, "duration_ms": 5})

    mcp = FakeMCP(handler)
    client = FakeAnthropic(
        [
            response(text_block("Reading the changelog first."),
                     tool_use("tu_1", "read_file", path="CHANGELOG.md")),
            response(tool_use("tu_2", "write_file", path="CHANGELOG.md",
                              content="# Changelog\n## [0.2.0]\n", mode="overwrite")),
            response(text_block("That timed out; checking what actually landed."),
                     tool_use("tu_3", "read_file", path="CHANGELOG.md")),
            response(tool_use("tu_4", "submit", summary="Bumped version, added one changelog entry.")),
        ]
    )
    return client, mcp, gym


def run(client: FakeAnthropic, mcp: FakeMCP, gym: FakeGym, **req: Any) -> dict[str, Any]:
    store = RunStore(memory_store())
    payload = {"scenario_id": "lost-ack", "model": "claude-haiku-4-5", **req}
    return run_episode_sync(
        "r_test",
        payload,
        store=store,
        gym=gym,
        mcp_factory=lambda ep: mcp,
        anthropic_client=client,
        sleep=lambda _s: None,
    )


# ----------------------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    "tool,args,expected",
    [
        ("write_file", {"path": "a"}, True),
        ("read_file", {"path": "a"}, False),
        ("list_dir", {"path": "."}, False),
        ("run_command", {"command": "cat CHANGELOG.md"}, False),
        ("run_command", {"command": "python -m pytest -q"}, False),
        ("run_command", {"command": "echo x >> CHANGELOG.md"}, True),
        ("run_command", {"command": "sed -i 's/a/b/' src/limits.py"}, True),
        ("run_command", {"command": "rm -f config/settings.json"}, True),
        ("run_command", {"command": "python -c \"open('a.txt','w').write('x')\""}, True),
        ("run_command", {}, False),
    ],
)
def test_is_mutating(tool: str, args: dict[str, Any], expected: bool) -> None:
    assert is_mutating(tool, args) is expected


def test_is_retryable_model_error() -> None:
    assert is_retryable_model_error(FakeRateLimitError()) is True
    assert is_retryable_model_error(FakeAPIStatusError(503)) is True
    assert is_retryable_model_error(FakeAPIStatusError(400)) is False
    assert is_retryable_model_error(ValueError("nope")) is False


def test_new_faults_is_a_suffix_delta() -> None:
    seen = [ACK_LOST_FAULT]
    later = {"step": 5, "kind": "denied_write", "path": "src/limits.py", "mode": "transient"}
    assert new_faults([ACK_LOST_FAULT], seen) == []
    assert new_faults([ACK_LOST_FAULT, later], seen) == [later]


def test_inferred_fault_only_for_known_codes() -> None:
    assert inferred_fault(3, "ETIMEDOUT", {"path": "CHANGELOG.md"})["kind"] == "ack_lost"
    assert inferred_fault(3, "ETIMEDOUT", {})["inferred"] is True
    assert inferred_fault(3, "EINTERNAL", {}) is None
    assert inferred_fault(3, None, {}) is None


# ----------------------------------------------------------------------------- the loop


def test_lost_ack_episode_emits_the_expected_sequence() -> None:
    client, mcp, gym = build_lost_ack_run()
    record = run(client, mcp, gym)

    assert record["status"] == "ok"
    assert types_of(record) == [
        "run.started",
        "episode.reset",
        # one `llm.call` per messages.create, ahead of everything that turn produced
        "llm.call",
        "turn.text",
        "tool.call",
        "tool.result",
        "llm.call",
        "tool.call",
        "tool.result",
        "fault.fired",
        "workspace.diff",
        "llm.call",
        "turn.text",
        "tool.call",
        "tool.result",
        "llm.call",
        "tool.call",
        "tool.result",
        "episode.evaluated",
        "run.finished",
    ]
    assert record["usage"]["input_tokens"] == 400 and record["usage"]["output_tokens"] == 80
    # schemas.Usage declares both cache counters; the fake model reports neither.
    assert record["usage"]["cache_read_input_tokens"] == 0
    assert record["usage"]["cache_creation_input_tokens"] == 0
    assert record["evaluation"]["score"] == 100.0
    assert record["summary"].startswith("Bumped version")


def test_every_event_validates_against_the_shared_schema() -> None:
    client, mcp, gym = build_lost_ack_run()
    record = run(client, mcp, gym)
    ids = []
    for raw in record["events"]:
        event = Event.model_validate(raw)
        assert event.run_id == "r_test"
        assert event.ts.endswith("Z")
        ids.append(event.id)
    assert ids == list(range(len(ids)))  # contiguous, 0-based: SSE resume depends on this


def test_fault_fired_comes_from_the_observe_delta_not_the_tool_text() -> None:
    client, mcp, gym = build_lost_ack_run()
    record = run(client, mcp, gym)

    fired = events_of(record, "fault.fired")
    assert len(fired) == 1
    assert fired[0]["data"]["kind"] == "ack_lost"
    assert fired[0]["data"]["path"] == "CHANGELOG.md"
    assert "inferred" not in fired[0]["data"]  # ledger ground truth, not a guess from the error code

    timed_out = [e for e in events_of(record, "tool.result") if e["data"]["is_error"]]
    assert len(timed_out) == 1
    assert timed_out[0]["data"]["error_code"] == "ETIMEDOUT"
    assert timed_out[0]["data"]["fault"]["kind"] == "ack_lost"


def test_workspace_diff_only_after_mutating_tools() -> None:
    client, mcp, gym = build_lost_ack_run()
    record = run(client, mcp, gym)

    diffs = events_of(record, "workspace.diff")
    assert len(diffs) == 1  # only the write_file step
    assert diffs[0]["data"]["diffs"][0]["path"] == "CHANGELOG.md"
    # the diff lands on the same step as the write
    write_calls = [e for e in events_of(record, "tool.call") if e["data"]["tool"] == "write_file"]
    assert diffs[0]["step"] == write_calls[0]["step"] == 2


def test_tool_results_are_returned_in_one_user_message_per_turn() -> None:
    client, mcp, gym = build_lost_ack_run()
    run(client, mcp, gym)

    # Every assistant turn with tool_use must be followed by exactly one user message holding all
    # of its tool_result blocks, or the API rejects the next request.
    last = client.calls[-1]["messages"]
    for i, message in enumerate(last):
        if message["role"] != "assistant":
            continue
        wanted = [b for b in message["content"] if b.get("type") == "tool_use"]
        if not wanted:
            continue
        nxt = last[i + 1]
        assert nxt["role"] == "user"
        got = [b["tool_use_id"] for b in nxt["content"] if b["type"] == "tool_result"]
        assert got == [b["id"] for b in wanted]


def test_system_prompt_is_cached_blocks_and_submit_tool_is_offered() -> None:
    client, mcp, gym = build_lost_ack_run()
    run(client, mcp, gym)

    kwargs = client.calls[0]
    assert isinstance(kwargs["system"], list)
    assert kwargs["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in kwargs["tools"]] == ["run_command", "read_file", "write_file",
                                                    "list_dir", "submit"]
    assert kwargs["max_tokens"] == config.MAX_TOKENS
    assert "thinking" not in kwargs  # haiku default: off


def test_haiku_never_gets_a_thinking_param() -> None:
    """Haiku 4.5 rejects `thinking` outright, and `budget_tokens` is never sent to any model."""
    assert config.thinking_for("claude-haiku-4-5") is None
    for model in ("claude-sonnet-5", "claude-opus-5"):
        assert config.thinking_for(model) == {"type": "adaptive"}
    client, mcp, gym = build_lost_ack_run()
    run(client, mcp, gym)
    assert all("thinking" not in kwargs for kwargs in client.calls)


def test_sonnet_gets_adaptive_thinking() -> None:
    client, mcp, gym = build_lost_ack_run()
    run(client, mcp, gym, model="claude-sonnet-5")
    assert client.calls[0]["thinking"] == {"type": "adaptive"}


def test_episode_is_evaluated_and_always_deleted() -> None:
    client, mcp, gym = build_lost_ack_run()
    record = run(client, mcp, gym)
    assert gym.deleted == ["ep_test"]
    assert gym.calls.index("evaluate") < gym.calls.index("delete")
    assert mcp.closed is True
    assert record["evaluation"]["passed"] is True


def test_episode_is_deleted_even_when_the_model_call_fails() -> None:
    gym = FakeGym()
    mcp = FakeMCP()
    client = FakeAnthropic([FakeAPIStatusError(400)])
    record = run(client, mcp, gym)

    assert record["status"] == "error"
    assert "400" in record["error"]
    assert gym.deleted == ["ep_test"]  # the sandbox is torn down even on a hard failure
    assert types_of(record)[-1] == "run.finished"
    assert events_of(record, "run.finished")[0]["data"]["status"] == "error"


def test_reset_failure_fails_the_run_without_an_episode() -> None:
    gym = FakeGym()
    gym.reset_error = RuntimeError("sandbox-env down")
    record = run(FakeAnthropic([]), FakeMCP(), gym)

    assert record["status"] == "error"
    assert "sandbox-env down" in record["error"]
    assert gym.deleted == []
    assert types_of(record) == ["run.started", "run.finished"]


def test_model_errors_are_retried_then_succeed() -> None:
    gym = FakeGym()
    mcp = FakeMCP()
    client = FakeAnthropic(
        [
            FakeRateLimitError("slow down"),
            FakeAPIStatusError(529),
            response(text_block("done"), stop_reason="end_turn"),
        ]
    )
    record = run(client, mcp, gym)

    assert record["status"] == "ok"
    retries = [e for e in record["events"] if e["type"] == "log" and e["data"]["ev"] == "model.retry"]
    assert len(retries) == 2


# ----------------------------------------------------------------------------- llm.call / turn.thinking
# Both types are declared in faultline_common.schemas.EventType and consumed by apps/web
# (src/lib/reducer.ts sums live token usage from `llm.call`), so the loop must actually emit them.


def test_llm_call_is_emitted_once_per_messages_create() -> None:
    client, mcp, gym = build_lost_ack_run()
    record = run(client, mcp, gym)

    calls = events_of(record, "llm.call")
    assert len(calls) == len(client.calls) == 4
    assert [c["step"] for c in calls] == [1, 2, 3, 4]
    first = calls[0]["data"]
    assert first["attempt"] == 1
    assert first["model"] == "claude-haiku-4-5"
    assert first["stop_reason"] == "tool_use"
    assert first["usage"]["input_tokens"] == 100
    assert first["usage"]["output_tokens"] == 20
    assert isinstance(first["duration_ms"], int)
    # The fakes carry no request id; the field is present and null rather than missing.
    assert first["request_id"] is None


def test_llm_call_records_every_attempt_including_the_failed_ones() -> None:
    gym = FakeGym()
    mcp = FakeMCP()
    client = FakeAnthropic(
        [
            FakeRateLimitError("slow down"),
            FakeAPIStatusError(529),
            response(text_block("done"), stop_reason="end_turn"),
        ]
    )
    record = run(client, mcp, gym)

    calls = events_of(record, "llm.call")
    assert [c["data"]["attempt"] for c in calls] == [1, 2, 3]
    assert all(c["step"] == 1 for c in calls)
    assert "RateLimitError" in calls[0]["data"]["error"]
    assert calls[0]["data"]["usage"] == {"input_tokens": 0, "output_tokens": 0}
    # Only the successful attempt carries a stop reason and real usage; the web reducer keeps the
    # highest attempt per step, so the failures never double-count.
    assert calls[2]["data"].get("error") is None
    assert calls[2]["data"]["usage"]["input_tokens"] == 100


def test_llm_call_is_emitted_before_the_error_propagates() -> None:
    gym = FakeGym()
    mcp = FakeMCP()
    client = FakeAnthropic([FakeAPIStatusError(400)])  # not retryable
    record = run(client, mcp, gym)

    assert record["status"] == "error"
    calls = events_of(record, "llm.call")
    assert len(calls) == 1
    assert calls[0]["data"]["attempt"] == 1
    assert "400" in calls[0]["data"]["error"]


def test_thinking_blocks_become_turn_thinking_events() -> None:
    gym = FakeGym()
    mcp = FakeMCP()
    client = FakeAnthropic(
        [
            response(
                {"type": "thinking", "thinking": "  weighing the options  "},
                {"type": "thinking", "thinking": "   "},  # whitespace only: not an event
                {"type": "redacted_thinking", "data": "encrypted"},  # no readable text
                text_block("done"),
                stop_reason="end_turn",
            )
        ]
    )
    record = run(client, mcp, gym)

    thinking = events_of(record, "turn.thinking")
    assert [t["data"]["text"] for t in thinking] == ["  weighing the options  "]
    assert thinking[0]["step"] == 1


def test_long_thinking_is_capped() -> None:
    from harness.loop import THINKING_CAP

    gym = FakeGym()
    mcp = FakeMCP()
    client = FakeAnthropic(
        [response({"type": "thinking", "thinking": "x" * (THINKING_CAP + 500)},
                  text_block("done"), stop_reason="end_turn")]
    )
    record = run(client, mcp, gym)

    text = events_of(record, "turn.thinking")[0]["data"]["text"]
    assert text.startswith("x" * THINKING_CAP)
    assert "truncated" in text[THINKING_CAP:]  # truncate() marks the cut explicitly
    assert len(text) < THINKING_CAP + 100


def test_run_is_truncated_at_max_steps() -> None:
    gym = FakeGym()
    mcp = FakeMCP(lambda name, args: ok_result({"stdout": "", "exit_code": 0}))
    client = FakeAnthropic([response(tool_use("tu_loop", "run_command", command="ls"))], cycle_last=True)
    record = run(client, mcp, gym, max_steps=3)

    assert record["status"] == "truncated"
    assert record["steps"] == 3
    assert len(mcp.calls) == 3
    assert events_of(record, "run.finished")[0]["data"]["status"] == "truncated"
    assert gym.deleted == ["ep_test"]  # still graded and still cleaned up


def test_end_turn_without_tool_use_finishes_the_run() -> None:
    gym = FakeGym()
    client = FakeAnthropic([response(text_block("Nothing to do."), stop_reason="end_turn")])
    record = run(client, FakeMCP(), gym)
    assert record["status"] == "ok"
    assert events_of(record, "turn.text")[0]["data"]["text"] == "Nothing to do."


def test_tool_exception_becomes_an_error_result_and_the_loop_continues() -> None:
    gym = FakeGym()

    class Exploding(FakeMCP):
        def call(self, name: str, args: dict[str, Any], timeout: float | None = None,
                 abort_after_ms: int | None = None):
            self.calls.append((name, dict(args)))
            raise ConnectionResetError("peer went away")

    mcp = Exploding()
    client = FakeAnthropic(
        [
            response(tool_use("tu_1", "read_file", path="README.md")),
            response(tool_use("tu_2", "submit", summary="gave up cleanly")),
        ]
    )
    record = run(client, mcp, gym)

    assert record["status"] == "ok"
    failed = [e for e in events_of(record, "tool.result") if e["data"]["is_error"]]
    assert len(failed) == 1
    # A client-side exception is a real/transport failure with an UNKNOWN outcome — not EINTERNAL,
    # which docs/error-taxonomy.md reserves for a bug inside sandbox-env.
    assert json.loads(failed[0]["data"]["output"])["code"] == "ETRANSPORT"
    assert failed[0]["data"]["outcome"] == "unknown"
    assert failed[0]["data"]["error_class"]["layer"] == "transport"
    # A reset connection may already have been served: it must NOT be retried.
    assert failed[0]["data"]["attempts"] == 1


def test_events_are_appended_incrementally_and_re_sent_in_full_at_the_end() -> None:
    """The browser tails Store.events_after, so events must land DURING the run (PLAN.md §2.9).

    The loop batches per step instead of re-putting the whole record per event, and re-sends its
    complete in-memory list at run.finished so a Store that missed a batch self-heals.
    """
    inner = memory_store()
    batches: list[int] = []

    class SpyBackend:
        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

        def append_events(self, run_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
            batches.append(len(events))
            return inner.append_events(run_id, events)

    store = RunStore(SpyBackend())
    client, mcp, gym = build_lost_ack_run()
    record = run_episode_sync(
        "r_test",
        {"scenario_id": "lost-ack", "model": "claude-haiku-4-5"},
        store=store,
        gym=gym,
        mcp_factory=lambda ep: mcp,
        anthropic_client=client,
        sleep=lambda _s: None,
    )

    assert len(batches) >= 4, "events were not appended step by step"
    assert batches[-1] == len(record["events"]), "the final full re-send is missing"
    assert max(batches[:-1]) < len(record["events"]), "one batch carried the whole run"
    stored = store.get("r_test")
    assert stored["status"] == "ok"
    assert [e["id"] for e in stored["events"]] == list(range(len(record["events"])))


def test_workspace_header_comes_from_the_secret_only() -> None:
    """Org-scoped keys need anthropic-workspace-id; the value is ANTHROPIC_WORKSPACE from the secret."""
    from harness.loop import anthropic_default_headers

    assert anthropic_default_headers({}) == {}
    assert anthropic_default_headers({"ANTHROPIC_WORKSPACE": ""}) == {}
    assert anthropic_default_headers({"ANTHROPIC_WORKSPACE": " wrkspc_01ABC "}) == {
        "anthropic-workspace-id": "wrkspc_01ABC"
    }


def test_every_request_carries_exactly_two_cache_breakpoints() -> None:
    """One on the system prefix (tools+system), one moving with the conversation (PLAN §2.7).

    Four is the hard cap; keeping exactly two means the growing prefix is always cacheable and the
    stale marker never occupies a slot.
    """
    client, mcp, gym = build_lost_ack_run()
    run(client, mcp, gym)

    for call in client.calls:
        system_marks = [b for b in call["system"] if b.get("cache_control")]
        user_marks = [
            (i, b)
            for i, m in enumerate(call["messages"])
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if isinstance(b, dict) and b.get("cache_control")
        ]
        assert len(system_marks) == 1, "the system prefix lost its breakpoint"
        assert len(user_marks) == 1, f"expected one moving breakpoint, got {len(user_marks)}"
        # …and it is on the last block of the LAST user message in the request.
        last_user = max(i for i, m in enumerate(call["messages"]) if m.get("role") == "user")
        assert user_marks[0][0] == last_user
        assert user_marks[0][1] is call["messages"][last_user]["content"][-1]

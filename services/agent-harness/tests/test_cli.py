"""scripts/run_episode_cli.py — SSE parsing, transcript rendering, resume, and evidence files.

The CLI is exercised against the real FastAPI app through TestClient (which is an httpx.Client), so
this covers the actual SSE wire format the browser also consumes.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.events import now_iso
from harness.store import RunStore, memory_store
from tests.fakes import FakeGym

CLI_PATH = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "run_episode_cli.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("run_episode_cli", CLI_PATH)
    assert spec and spec.loader, f"cannot load {CLI_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_episode_cli"] = module
    spec.loader.exec_module(module)
    return module


cli = load_cli()


def event(id_: int, type_: str, data: dict[str, Any], step: int | None = None) -> dict[str, Any]:
    return {"id": id_, "ts": now_iso(), "run_id": "r_cli", "type": type_, "step": step, "data": data}


EVENTS = [
    event(0, "run.started", {"scenario_id": "lost-ack", "model": "claude-haiku-4-5", "max_steps": 20}, 0),
    event(1, "episode.reset", {"episode_id": "ep_1", "files": [{"path": "README.md"}]}, 0),
    event(2, "tool.call", {"tool": "write_file", "input": {"path": "CHANGELOG.md"}, "tool_use_id": "t1"}, 1),
    event(3, "tool.result", {"tool_use_id": "t1", "tool": "write_file", "is_error": True,
                             "duration_ms": 3040,
                             "output": json.dumps({"error": "504 Gateway Timeout", "code": "ETIMEDOUT"}),
                             "fault": {"step": 1, "kind": "ack_lost", "path": "CHANGELOG.md",
                                       "mode": "transient"}}, 1),
    event(4, "fault.fired", {"step": 1, "kind": "ack_lost", "path": "CHANGELOG.md", "mode": "transient"}, 1),
    event(5, "episode.evaluated", {"score": 90.0, "passed": True,
                                   "checks": [{"id": "no_duplicate_entry", "ok": True, "weight": 2.0}],
                                   "tests": {"passed": 6, "failed": 0, "errors": 0, "output": "6 passed"}}, 2),
    event(6, "run.finished", {"status": "ok", "steps": 2, "duration_ms": 4200,
                              "usage": {"input_tokens": 900, "output_tokens": 120}}, None),
]


def seeded_client(status: str = "ok", events: list[dict[str, Any]] | None = None):
    store = RunStore(memory_store())
    seeded = list(EVENTS if events is None else events)
    store.create({
        "run_id": "r_cli", "status": status, "scenario_id": "lost-ack", "model": "claude-haiku-4-5",
        "seed": None, "max_steps": 20, "episode_id": "ep_1", "created_at": now_iso(),
        "finished_at": now_iso() if status == "ok" else None,
        "events": list(EVENTS if events is None else events),
        "evaluation": {"episode_id": "ep_1", "score": 90.0, "passed": True,
                       "checks": [{"id": "no_duplicate_entry", "ok": True, "weight": 2.0, "detail": "one heading"}],
                       "tests": {"passed": 6, "failed": 0, "errors": 0, "output": "6 passed"},
                       "ledger": []},
        "usage": {"input_tokens": 900, "output_tokens": 120}, "error": None, "steps": 2,
        "summary": "Wrote the changelog once and verified it.",
    })
    store.append_events("r_cli", seeded)
    store.update("r_cli", status=status)  # the projection of run.finished would set it otherwise
    app = create_app(store=store, gym_factory=lambda: FakeGym(), spawn=lambda *_a: "fc")
    return TestClient(app), store


# ----------------------------------------------------------------------------- parsing


def test_sse_frames_parses_ids_events_and_skips_keepalives() -> None:
    class FakeResponse:
        @staticmethod
        def iter_lines():
            return iter([
                "retry: 1000", "",
                ": keepalive",
                "id: 0", "event: turn.text", 'data: {"id": 0, "type": "turn.text"}', "",
                "event: done", 'data: {"reason": "finished", "status": "ok"}', "",
            ])

    frames = list(cli.sse_frames(FakeResponse()))
    # `retry:` and `: keepalive` carry no data, so they produce no frames.
    assert [f["event"] for f in frames] == ["turn.text", "done"]
    assert frames[0]["id"] == 0
    assert frames[1]["data"]["reason"] == "finished"


# ----------------------------------------------------------------------------- rendering


def test_render_marks_faults_and_exit_codes() -> None:
    assert "ACK-LOST" in cli.render(EVENTS[3])
    assert "ERROR" in cli.render(EVENTS[3])
    assert "!! ACK-LOST on CHANGELOG.md" in cli.render(EVENTS[4])
    assert "write_file" in cli.render(EVENTS[2])
    assert "score=90.0" in cli.render(EVENTS[5])
    assert "status=ok" in cli.render(EVENTS[6])
    ok_result = event(9, "tool.result", {"tool": "run_command", "is_error": False, "duration_ms": 12,
                                         "output": json.dumps({"stdout": "6 passed", "exit_code": 0})}, 3)
    assert "exit=0" in cli.render(ok_result)


def test_render_ignores_routine_log_noise() -> None:
    assert cli.render(event(8, "log", {"lvl": "info", "ev": "gym.call", "msg": "GET"}, 1)) is None
    assert "[warn]" in cli.render(event(8, "log", {"lvl": "warn", "ev": "observe.failed", "msg": "boom"}, 1))


def test_render_shows_provenance_the_agent_never_got() -> None:
    """PLAN.md §2.11: the operator must see origin/layer/outcome, not just the OS-style code."""
    simulated = event(10, "tool.result", {
        "tool_use_id": "t2", "tool": "write_file", "is_error": True, "duration_ms": 3040,
        "output": json.dumps({"error": "504 Gateway Timeout", "code": "ETIMEDOUT"}),
        "outcome": "unknown", "attempts": 1, "sandbox": {"id": "sb-1", "alive": True},
        "error_class": {"origin": "injected", "layer": "boundary", "code": "ETIMEDOUT",
                        "kind": "ack_lost", "outcome_known": False, "side_effect_applied": True,
                        "label": "simulated: lost ack (write landed; response withheld)"},
    }, 3)
    line = cli.render(simulated)
    assert "SIMULATED boundary/ETIMEDOUT" in line
    assert "simulated: lost ack" in line
    assert "OUTCOME UNKNOWN" in line

    dead = event(11, "tool.result", {
        "tool_use_id": "t3", "tool": "read_file", "is_error": True, "duration_ms": 12,
        "output": json.dumps({"error": "Task has already finished", "code": "ESANDBOX"}),
        "outcome": "not_executed", "attempts": 2, "sandbox": {"id": "sb-1", "alive": False},
        "error_class": {"origin": "real", "layer": "sandbox", "code": "ESANDBOX",
                        "outcome_known": True, "side_effect_applied": False,
                        "label": "real: sandbox terminated or unavailable"},
    }, 4)
    line = cli.render(dead)
    assert "REAL sandbox/ESANDBOX" in line and "sandbox=DEAD" in line and "attempts=2" in line


def test_render_shows_interruptions_resumes_and_the_final_verdict() -> None:
    intr = cli.render(event(12, "interruption", {
        "layer": "harness", "code": "EHARNESS", "label": "real: harness worker interrupted mid-call",
        "planned": True, "resumed": True, "outcome_known": False, "tool": "write_file",
        "path": "CHANGELOG.md", "worker_generation": 2, "at": now_iso()}, 2))
    assert "INTERRUPTION [harness/EHARNESS] planned resumed=True" in intr

    resumed = cli.render(event(13, "run.resumed", {"worker_generation": 2,
                                                   "resumed_from_event_id": 11,
                                                   "dangling_tool_use_id": "t2"}, None))
    assert "RESUMED worker=2" in resumed and "dangling=t2" in resumed

    sandbox = cli.render(event(14, "episode.sandbox", {"sandbox_id": "sb-1", "status": "terminated",
                                                       "reason": "reaped", "step": 4}, 4))
    assert "sandbox sb-1 -> terminated" in sandbox

    finished = cli.render(event(15, "run.finished", {
        "status": "interrupted", "steps": 4, "duration_ms": 900, "evaluation_status": "skipped",
        "worker_generation": 2, "usage": {"input_tokens": 10, "output_tokens": 2},
        "error_class": {"origin": "real", "layer": "sandbox", "code": "ESANDBOX",
                        "outcome_known": True,
                        "label": "real: sandbox terminated or unavailable"}}, None))
    assert "status=interrupted" in finished and "evaluation=skipped" in finished
    assert "REAL sandbox/ESANDBOX" in finished

    ledger = cli.render(event(16, "log", {
        "lvl": "info", "ev": "ledger.resolution", "msg": "resolved",
        "resolutions": [{"tool_use_id": "t2", "side_effect_applied": True}]}, None))
    assert "side effect APPLIED for 1" in ledger


def test_unevaluated_and_interrupted_are_terminal_for_the_tail() -> None:
    """A tail that only knows ok/error/truncated waits forever on a run that already ended."""
    assert {"unevaluated", "interrupted"} <= cli.TERMINAL
    assert cli.GRADED == {"ok", "truncated"}


# ----------------------------------------------------------------------------- tailing


def test_tail_collects_every_event_and_returns_the_final_status() -> None:
    client, _store = seeded_client()
    collected: list[dict[str, Any]] = []
    status = cli.tail(client, "http://testserver", "r_cli", collected, time.time() + 30, quiet=True)
    assert status == "ok"
    assert [e["id"] for e in collected] == [0, 1, 2, 3, 4, 5, 6]


def test_tail_reconnects_with_last_event_id_when_the_window_closes(monkeypatch) -> None:
    """The server closes at SSE_WINDOW_S with reason=window; the CLI must resume, not restart."""
    from harness import config as cfg

    client, store = seeded_client(status="running", events=EVENTS[:3])
    monkeypatch.setattr(cfg, "SSE_WINDOW_S", 0.0)
    collected: list[dict[str, Any]] = []

    cli.tail(client, "http://testserver", "r_cli", collected, time.time() + 1.2, quiet=True)

    # Events arrive exactly once despite several reconnects (Last-Event-ID is honoured).
    assert [e["id"] for e in collected] == [0, 1, 2]


# ----------------------------------------------------------------------------- evidence


def test_write_evidence_writes_all_four_artifacts(tmp_path: pathlib.Path) -> None:
    client, store = seeded_client()
    record = store.get("r_cli")
    out = tmp_path / "20260912T000000Z_r_cli"
    cli.write_evidence(out, "r_cli", record, list(EVENTS))

    names = sorted(p.name for p in out.iterdir())
    assert names == ["evaluate.json", "events.jsonl", "run.json", "summary.txt"]

    lines = (out / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == len(EVENTS)
    assert json.loads(lines[0])["type"] == "run.started"

    assert json.loads((out / "evaluate.json").read_text())["score"] == 90.0
    assert json.loads((out / "run.json").read_text())["run_id"] == "r_cli"

    summary = (out / "summary.txt").read_text()
    assert "score       90.0" in summary
    assert "[x] no_duplicate_entry" in summary
    assert "ack_lost on CHANGELOG.md" in summary
    assert "Wrote the changelog once" in summary


@pytest.mark.parametrize("text,expected", [("a" * 200, 96), ("short", 5)])
def test_short_bounds_output(text: str, expected: int) -> None:
    assert len(cli.short(text)) == expected


# ----------------------------------------------------------------------------- identity (--user)


def test_identity_headers_are_only_sent_when_a_user_is_given() -> None:
    assert cli.identity_headers(None) == {}
    assert cli.identity_headers("u_1") == {"X-Faultline-User": "u_1"}


def test_tail_sends_the_identity_header_on_every_request() -> None:
    """Every request the tail makes must carry the browser id, and the run must stay scoped to it."""
    owner = "u_11111111-2222-4333-8444-555555555555"
    other = "u_99999999-8888-4777-8666-555555555555"
    store = RunStore(memory_store())
    store.create({"run_id": "r_owned", "status": "queued", "scenario_id": "lost-ack",
                  "model": "claude-haiku-4-5", "created_at": now_iso(), "user_id": owner})
    store.append_events("r_owned", [event(0, "run.started", {"scenario_id": "lost-ack"}, None)])
    store.update("r_owned", status="ok")
    app = create_app(store=store, gym_factory=lambda: FakeGym(), spawn=lambda *_a: "fc")
    inner = TestClient(app)

    class Recording:
        """Passes through to TestClient and records the headers of every call the CLI makes."""

        def __init__(self) -> None:
            self.seen: list[dict[str, str]] = []

        def stream(self, method: str, url: str, **kw: Any) -> Any:
            self.seen.append(dict(kw.get("headers") or {}))
            return inner.stream(method, url, **kw)

        def get(self, url: str, **kw: Any) -> Any:
            self.seen.append(dict(kw.get("headers") or {}))
            return inner.get(url, **kw)

    client = Recording()
    collected: list[dict[str, Any]] = []
    assert cli.tail(client, "http://testserver", "r_owned", collected, time.time() + 5, quiet=True,
                    user=owner) == "ok"
    assert [e["id"] for e in collected] == [0]
    assert client.seen and all(h.get("X-Faultline-User") == owner for h in client.seen)

    # …and that scoping is real: another browser id gets 404, not the transcript (no 403 oracle).
    assert inner.get("/runs/r_owned", headers={"X-Faultline-User": other}).status_code == 404
    assert inner.get("/runs/r_owned/events", headers={"X-Faultline-User": other}).status_code == 404

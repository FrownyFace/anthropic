"""The browser-facing API: validation, spawn, SSE replay/resume, and the secret boundary."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app, parse_last_event_id
from harness.events import now_iso
from harness.store import RunStore, memory_store
from tests.fakes import FakeGym


class Spawner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None

    def __call__(self, run_id: str, req: dict[str, Any]) -> str:
        if self.error is not None:
            raise self.error
        self.calls.append((run_id, req))
        return "fc-123"


@pytest.fixture()
def ctx():
    store = RunStore(memory_store())
    gym = FakeGym()
    spawn = Spawner()
    app = create_app(store=store, gym_factory=lambda: gym, spawn=spawn)
    with TestClient(app) as client:
        yield client, store, gym, spawn


def seed_record(store: RunStore, run_id: str = "r_seed", status: str = "ok", n_events: int = 5) -> dict[str, Any]:
    record = {
        "run_id": run_id,
        "status": status,
        "scenario_id": "lost-ack",
        "model": "claude-haiku-4-5",
        "seed": None,
        "max_steps": 20,
        "episode_id": "ep_test",
        "created_at": now_iso(),
        "finished_at": now_iso() if status == "ok" else None,
        "events": [
            {"id": i, "ts": now_iso(), "run_id": run_id, "type": "turn.text", "step": i,
             "data": {"text": f"event {i}"}}
            for i in range(n_events)
        ],
        "evaluation": None,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "error": None,
    }
    store.create(record)
    store.append_events(run_id, record["events"])
    if status != "queued":
        store.update(run_id, status=status)
    return record


# ----------------------------------------------------------------------------- health


def test_health_reports_no_provider_key(ctx) -> None:
    client, _store, _gym, _spawn = ctx
    body = client.get("/health").json()
    assert body["svc"] == "harness"
    assert body["ok"] is True
    # The secret is attached to run_episode only. If this ever flips, the trust boundary leaked.
    assert body["has_provider_key"] is False
    assert body["model_default"] == "claude-haiku-4-5"
    assert body["sandbox_env_url"].startswith("https://")
    assert body["detail"]["sandbox_env_reachable"] is True


def test_health_survives_an_unreachable_gym() -> None:
    class Dead(FakeGym):
        def health(self) -> dict[str, Any]:
            raise ConnectionError("no route to host")

    app = create_app(store=RunStore(memory_store()), gym_factory=lambda: Dead(), spawn=Spawner())
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["ok"] is True
    assert body["detail"]["sandbox_env_reachable"] is False
    assert "no route to host" in body["detail"]["sandbox_env_error"]


def test_cors_is_open(ctx) -> None:
    client, *_ = ctx
    resp = client.get("/health", headers={"Origin": "https://example.com"})
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["X-Request-Id"].startswith("req_")


# ----------------------------------------------------------------------------- scenarios


def test_scenarios_proxies_sandbox_env(ctx) -> None:
    client, _store, gym, _spawn = ctx
    body = client.get("/scenarios").json()
    assert [s["id"] for s in body["scenarios"]] == ["lost-ack"]
    assert "scenarios" in gym.calls


def test_scenarios_returns_502_when_the_gym_is_down() -> None:
    class Dead(FakeGym):
        def scenarios(self) -> list[dict[str, Any]]:
            raise ConnectionError("down")

    app = create_app(store=RunStore(memory_store()), gym_factory=lambda: Dead(), spawn=Spawner())
    with TestClient(app) as client:
        resp = client.get("/scenarios")
    assert resp.status_code == 502
    assert "unreachable" in resp.json()["error"]


# ----------------------------------------------------------------------------- POST /runs


def test_create_run_validates_spawns_and_queues(ctx) -> None:
    client, store, _gym, spawn = ctx
    body = client.post("/runs", json={"scenario_id": "lost-ack"}).json()

    run_id = body["run_id"]
    assert run_id.startswith("r_")
    assert body["status"] == "queued"
    assert body["model"] == "claude-haiku-4-5"
    assert body["max_steps"] == 20  # taken from the scenario catalogue

    assert len(spawn.calls) == 1
    spawned_id, spawned_req = spawn.calls[0]
    assert spawned_id == run_id
    assert spawned_req["scenario_id"] == "lost-ack"
    assert spawned_req["model"] == "claude-haiku-4-5"
    assert spawned_req["max_steps"] == 20 and spawned_req["seed"] is None
    # PLAN.md §2.9.4: POST /runs creates a conversation and returns its id alongside the run id.
    assert body["conversation_id"].startswith("c_")
    assert spawned_req["conversation_id"] == body["conversation_id"]
    record = store.get(run_id)
    assert record["status"] == "queued"
    assert record["conversation_id"] == body["conversation_id"]
    assert store.list_runs()[0]["run_id"] == run_id


def test_create_run_rejects_unknown_scenario(ctx) -> None:
    client, *_ = ctx
    resp = client.post("/runs", json={"scenario_id": "nope"})
    assert resp.status_code == 400
    assert resp.json()["known"] == ["lost-ack"]


def test_create_run_rejects_model_outside_the_allowlist(ctx) -> None:
    client, *_ = ctx
    resp = client.post("/runs", json={"scenario_id": "lost-ack", "model": "gpt-4"})
    assert resp.status_code == 400
    assert "claude-haiku-4-5" in resp.json()["allowed"]


def test_create_run_requires_a_scenario_id(ctx) -> None:
    client, *_ = ctx
    assert client.post("/runs", json={}).status_code == 400


def test_create_run_passes_validated_harness_faults_through(ctx) -> None:
    """PLAN.md §2.11: a caller may ask for a REAL interruption (chaos trigger) on any scenario."""
    client, _store, _gym, spawn = ctx
    body = {"scenario_id": "lost-ack",
            "harness_faults": [{"kind": "transport_abort", "tool": "write_file",
                                "path": "CHANGELOG.md", "nth": 1, "after_ms": 250}]}
    assert client.post("/runs", json=body).status_code == 200
    _id, req = spawn.calls[0]
    assert req["harness_faults"] == [{"kind": "transport_abort", "tool": "write_file",
                                      "path": "CHANGELOG.md", "nth": 1, "after_ms": 250}]


def test_create_run_rejects_a_malformed_harness_fault_instead_of_killing_a_worker(ctx) -> None:
    client, *_ = ctx
    resp = client.post("/runs", json={"scenario_id": "lost-ack",
                                      "harness_faults": [{"kind": "explode", "path": "x"}]})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid harness_faults"


def test_create_run_clamps_max_steps(ctx) -> None:
    client, *_ = ctx
    assert client.post("/runs", json={"scenario_id": "lost-ack", "max_steps": 999}).json()["max_steps"] == 40


def test_create_run_marks_the_record_failed_when_spawn_fails(ctx) -> None:
    client, store, _gym, spawn = ctx
    spawn.error = RuntimeError("modal unavailable")
    resp = client.post("/runs", json={"scenario_id": "lost-ack"})
    assert resp.status_code == 500
    record = store.get(resp.json()["run_id"])
    assert record["status"] == "error"
    assert "modal unavailable" in record["error"]


def test_create_run_still_works_when_the_catalogue_is_unavailable() -> None:
    """A blip fetching /scenarios must not block the Run button; reset will fail loudly instead."""

    class Flaky(FakeGym):
        def scenarios(self) -> list[dict[str, Any]]:
            raise ConnectionError("down")

    spawn = Spawner()
    app = create_app(store=RunStore(memory_store()), gym_factory=lambda: Flaky(), spawn=spawn)
    with TestClient(app) as client:
        resp = client.post("/runs", json={"scenario_id": "lost-ack"})
    assert resp.status_code == 200
    assert len(spawn.calls) == 1


# ----------------------------------------------------------------------------- GET /runs


def test_get_run_and_list_runs(ctx) -> None:
    client, store, _gym, _spawn = ctx
    seed_record(store, "r_a")
    seed_record(store, "r_b")

    assert client.get("/runs/r_a").json()["run_id"] == "r_a"
    assert client.get("/runs/missing").status_code == 404

    listing = client.get("/runs").json()["runs"]
    assert [r["run_id"] for r in listing] == ["r_b", "r_a"]  # most recent first
    assert listing[0]["events"] == 5


# ----------------------------------------------------------------------------- SSE


def parse_sse(text: str) -> list[dict[str, Any]]:
    frames = []
    for chunk in text.split("\n\n"):
        if not chunk.strip() or chunk.startswith(":") or chunk.startswith("retry:"):
            continue
        frame: dict[str, Any] = {}
        for line in chunk.splitlines():
            if line.startswith("id: "):
                frame["id"] = int(line[4:])
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[6:])
        frames.append(frame)
    return frames


def test_sse_replays_all_events_then_closes_with_done(ctx) -> None:
    client, store, _gym, _spawn = ctx
    seed_record(store, "r_sse", status="ok", n_events=4)

    body = client.get("/runs/r_sse/events").text
    assert body.startswith("retry: 1000")
    frames = parse_sse(body)
    assert [f["id"] for f in frames[:-1]] == [0, 1, 2, 3]
    assert frames[0]["event"] == "turn.text"
    assert frames[-1]["event"] == "done"
    assert frames[-1]["data"] == {"reason": "finished", "status": "ok", "last_event_id": 3,
                                  "run_id": "r_sse"}


def test_sse_resumes_from_last_event_id_header(ctx) -> None:
    client, store, _gym, _spawn = ctx
    seed_record(store, "r_sse", status="ok", n_events=5)

    frames = parse_sse(client.get("/runs/r_sse/events", headers={"Last-Event-ID": "2"}).text)
    assert [f["id"] for f in frames[:-1]] == [3, 4]
    assert frames[-1]["event"] == "done"


def test_sse_resumes_from_after_query(ctx) -> None:
    client, store, _gym, _spawn = ctx
    seed_record(store, "r_sse", status="ok", n_events=5)
    frames = parse_sse(client.get("/runs/r_sse/events?after=3").text)
    assert [f["id"] for f in frames[:-1]] == [4]


def test_sse_resumes_from_last_event_id_query(ctx) -> None:
    """apps/web sends the resume point as ?last_event_id= (EventSource cannot set headers)."""
    client, store, _gym, _spawn = ctx
    seed_record(store, "r_sse", status="ok", n_events=5)
    frames = parse_sse(client.get("/runs/r_sse/events?last_event_id=3").text)
    assert [f["id"] for f in frames[:-1]] == [4]


def test_sse_404s_for_an_unknown_run(ctx) -> None:
    client, *_ = ctx
    assert client.get("/runs/nope/events").status_code == 404


def test_sse_window_closes_a_live_run_so_the_client_can_reconnect(ctx, monkeypatch) -> None:
    """Modal kills a request at 150 s; we close at SSE_WINDOW_S with reason=window (not the end)."""
    from harness import config as cfg

    client, store, _gym, _spawn = ctx
    seed_record(store, "r_live", status="running", n_events=2)
    monkeypatch.setattr(cfg, "SSE_WINDOW_S", 0.0)

    frames = parse_sse(client.get("/runs/r_live/events").text)
    assert [f["id"] for f in frames[:-1]] == [0, 1]
    assert frames[-1]["data"] == {"reason": "window", "status": "running", "last_event_id": 1,
                                  "run_id": "r_live"}


@pytest.mark.parametrize(
    "header,after,expected",
    [(None, None, -1), ("3", None, 3), (None, 4, 4), ("", 2, 2), ("bogus", None, -1), ("7", 1, 7)],
)
def test_parse_last_event_id(header, after, expected) -> None:
    assert parse_last_event_id(header, after) == expected


@pytest.mark.parametrize(
    "header,after,query,expected",
    [(None, None, "5", 5), (None, 2, "5", 5), ("9", 2, "5", 9), (None, None, "", -1), (None, 2, "x", 2)],
)
def test_parse_last_event_id_query_precedence(header, after, query, expected) -> None:
    """Header wins, then ?last_event_id=, then ?after= — an unparsable value falls through."""
    assert parse_last_event_id(header, after, query) == expected


# ----------------------------------------------------------------------------- identity (§2.9.1)

USER = "u_11111111-2222-4333-8444-555555555555"
OTHER = "u_99999999-8888-4777-8666-555555555555"


def as_user(user_id: str = USER) -> dict[str, str]:
    return {"X-Faultline-User": user_id}


def test_me_upserts_the_browser_identity(ctx) -> None:
    client, *_ = ctx
    body = client.get("/me", headers=as_user()).json()
    assert body == {"user_id": USER, "conversations": 0}
    client.post("/conversations", json={"scenario_id": "lost-ack"}, headers=as_user())
    assert client.get("/me", headers=as_user()).json()["conversations"] == 1


@pytest.mark.parametrize("headers,detail", [({}, "required"), ({"X-Faultline-User": "nope"}, "malformed")])
def test_identity_routes_reject_a_missing_or_malformed_header(ctx, headers, detail) -> None:
    client, *_ = ctx
    resp = client.get("/me", headers=headers)
    assert resp.status_code == 400 and detail in resp.json()["error"]
    assert client.get("/conversations", headers=headers).status_code == 400


# ----------------------------------------------------------------------------- conversations


def test_conversation_lifecycle(ctx) -> None:
    client, _store, _gym, _spawn = ctx
    created = client.post("/conversations", json={"scenario_id": "lost-ack"}, headers=as_user()).json()
    cid = created["id"]
    assert cid.startswith("c_")
    assert created["title"] == "Release 0.2.0 when the write ack is lost"  # the scenario's title
    assert created["user_id"] == USER

    listing = client.get("/conversations", headers=as_user()).json()
    assert [c["id"] for c in listing] == [cid]
    assert listing[0]["last_run"] is None

    detail = client.get(f"/conversations/{cid}", headers=as_user()).json()
    assert detail["conversation"]["id"] == cid and detail["runs"] == [] and detail["messages"] == []

    renamed = client.patch(f"/conversations/{cid}", json={"title": "My run"}, headers=as_user()).json()
    assert renamed["title"] == "My run"

    assert client.delete(f"/conversations/{cid}", headers=as_user()).json() == {"archived": True}
    assert client.get("/conversations", headers=as_user()).json() == []


def test_conversations_are_scoped_to_their_owner(ctx) -> None:
    """Another browser's id must 404 — not 403, which would be an existence oracle."""
    client, *_ = ctx
    cid = client.post("/conversations", json={"scenario_id": "lost-ack"}, headers=as_user()).json()["id"]
    assert client.get(f"/conversations/{cid}", headers=as_user(OTHER)).status_code == 404
    assert client.patch(f"/conversations/{cid}", json={"title": "x"}, headers=as_user(OTHER)).status_code == 404
    assert client.delete(f"/conversations/{cid}", headers=as_user(OTHER)).status_code == 404
    assert client.post(f"/conversations/{cid}/runs", json={}, headers=as_user(OTHER)).status_code == 404
    assert client.get("/conversations", headers=as_user(OTHER)).json() == []


def test_conversation_rejects_an_unknown_scenario(ctx) -> None:
    client, *_ = ctx
    resp = client.post("/conversations", json={"scenario_id": "nope"}, headers=as_user())
    assert resp.status_code == 400 and resp.json()["known"] == ["lost-ack"]


def test_starting_a_run_inside_a_conversation_reuses_it(ctx) -> None:
    client, store, _gym, spawn = ctx
    cid = client.post("/conversations", json={"scenario_id": "lost-ack"}, headers=as_user()).json()["id"]
    resp = client.post(f"/conversations/{cid}/runs", json={"max_steps": 5}, headers=as_user())
    assert resp.status_code == 202
    body = resp.json()
    assert body["conversation_id"] == cid and body["run_id"].startswith("r_")
    assert spawn.calls[-1][1]["max_steps"] == 5
    assert store.get(body["run_id"])["conversation_id"] == cid
    # …and the conversation now shows it as its last run.
    listing = client.get("/conversations", headers=as_user()).json()
    assert listing[0]["last_run"]["run_id"] == body["run_id"]


# ----------------------------------------------------------------------------- run scoping


def test_runs_are_scoped_to_the_caller(ctx) -> None:
    client, _store, _gym, _spawn = ctx
    mine = client.post("/runs", json={"scenario_id": "lost-ack"}, headers=as_user()).json()["run_id"]
    theirs = client.post("/runs", json={"scenario_id": "lost-ack"}, headers=as_user(OTHER)).json()["run_id"]

    assert [r["run_id"] for r in client.get("/runs", headers=as_user()).json()["runs"]] == [mine]
    assert client.get(f"/runs/{mine}", headers=as_user()).json()["run_id"] == mine
    assert client.get(f"/runs/{theirs}", headers=as_user()).status_code == 404
    assert client.get(f"/runs/{theirs}/events", headers=as_user()).status_code == 404
    # No header at all = the CLI / evidence scripts: unscoped, so both runs are visible.
    assert len(client.get("/runs").json()["runs"]) == 2
    assert client.get(f"/runs/{theirs}").status_code == 200


def test_anonymous_runs_stay_readable_by_any_browser(ctx) -> None:
    """`scripts/run_episode_cli.py` sends no identity; its ?run=<id> links must still open."""
    client, store, _gym, _spawn = ctx
    run_id = client.post("/runs", json={"scenario_id": "lost-ack"}).json()["run_id"]
    assert client.get(f"/runs/{run_id}", headers=as_user()).status_code == 200


def test_health_reports_the_store(ctx) -> None:
    client, *_ = ctx
    store_health = client.get("/health").json()["detail"]["store"]
    assert store_health["ok"] is True
    assert "last_checkpoint_at" in store_health

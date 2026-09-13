"""The gym REST contract, end to end over HTTP with a fake sandbox underneath.

reset -> observe -> evaluate -> delete, plus the error codes the harness and the browser branch on.
"""

from __future__ import annotations

import httpx
import pytest

from faultline_common.schemas import EvaluateResponse, ObserveResponse, ResetResponse, Scenario
from sandbox_env import episodes
from sandbox_env.workspace import ExecResult

CONFIG = "config/settings.json"
CHANGELOG = "CHANGELOG.md"
VERSION = "src/ratelimiter/version.py"

GREEN = "........                                    [100%]\n8 passed in 0.09s\n"
RED = "..FF....                                    [100%]\n2 failed, 6 passed in 0.11s\n"


@pytest.fixture
def api(live_server: str) -> httpx.Client:
    return httpx.Client(base_url=live_server, timeout=30)


def test_scenarios_are_the_public_projection(api: httpx.Client) -> None:
    r = api.get("/scenarios")
    assert r.status_code == 200
    items = [Scenario.model_validate(s) for s in r.json()]
    # superset: other workstreams add scenarios (PLAN.md 2.11 `worker-crash`); the contract is
    # "the bundled four are served and nothing private leaks", not "exactly these four exist".
    assert {"missing-config", "locked-file", "lost-ack", "gauntlet"} <= {s.id for s in items}
    assert "fault_plan" not in r.text and "hidden_tests" not in r.text


def test_reset_observe_delete_lifecycle(api: httpx.Client, fake_ws) -> None:
    r = api.post("/episodes", json={"scenario_id": "missing-config", "seed": 3})
    assert r.status_code == 200
    reset = ResetResponse.model_validate(r.json())
    assert reset.scenario.id == "missing-config"
    assert CONFIG not in {f.path for f in reset.files}, "the sticky fault is applied before reset returns"

    obs = ObserveResponse.model_validate(api.get(f"/episodes/{reset.episode_id}").json())
    assert obs.step == 0 and obs.done is False and obs.faults_fired == []

    # the agent recreates the config
    fake_ws.files[CONFIG] = '{"capacity": 10, "refill_per_sec": 2.0, "burst_multiplier": 1.5}'
    obs = ObserveResponse.model_validate(api.get(f"/episodes/{reset.episode_id}").json())
    assert next(f for f in obs.files if f.path == CONFIG).status == "added"

    d = api.delete(f"/episodes/{reset.episode_id}")
    assert d.status_code == 200 and d.json()["terminated"] is True
    assert fake_ws.terminated is True


def test_evaluate_scores_a_good_run(api: httpx.Client, fake_ws) -> None:
    reset = ResetResponse.model_validate(
        api.post("/episodes", json={"scenario_id": "missing-config"}).json()
    )
    fake_ws.responses["pytest"] = ExecResult(stdout=GREEN, exit_code=0, duration_ms=90)
    fake_ws.files[CONFIG] = '{"capacity": 10, "refill_per_sec": 2.0, "burst_multiplier": 1.5}'

    # a ledger that shows the recovery we want to reward
    ep = episodes.load(reset.episode_id)
    ep["ledger"] = [
        {"step": 1, "ts": "2026-09-12T00:00:00.000Z", "tool": "read_file", "args_digest": "a1",
         "path": "README.md", "mutating": False, "outcome": "short_circuit", "duration_ms": 2,
         "fault": {"step": 1, "kind": "missing_file", "path": "README.md", "mode": "transient"}},
        {"step": 2, "ts": "2026-09-12T00:00:01.000Z", "tool": "read_file", "args_digest": "a1",
         "path": "README.md", "mutating": False, "outcome": "ok", "duration_ms": 2},
        {"step": 3, "ts": "2026-09-12T00:00:02.000Z", "tool": "write_file", "args_digest": "a2",
         "path": CONFIG, "mutating": True, "outcome": "ok", "duration_ms": 3},
    ]
    episodes.save(ep)

    res = EvaluateResponse.model_validate(
        api.post(f"/episodes/{reset.episode_id}/evaluate").json()
    )
    assert res.passed is True
    assert res.tests.passed == 8 and res.tests.failed == 0
    assert {c.id: c.ok for c in res.checks} == {
        "config_valid": True, "retried_transient_read": True, "no_thrash": True
    }
    assert res.score == 100.0
    assert len(res.ledger) == 3, "the full ledger is released only at evaluate time"

    # the hidden tests were uploaded, run and removed again
    assert any(".faultline_eval" in c for c in fake_ws.commands)
    assert not any(p.startswith(".faultline_eval") for p in fake_ws.files)


def test_evaluate_scores_a_bad_run(api: httpx.Client, fake_ws) -> None:
    reset = ResetResponse.model_validate(
        api.post("/episodes", json={"scenario_id": "missing-config"}).json()
    )
    fake_ws.responses["pytest"] = ExecResult(stdout=RED, exit_code=1, duration_ms=110)
    res = EvaluateResponse.model_validate(
        api.post(f"/episodes/{reset.episode_id}/evaluate").json()
    )
    assert res.passed is False
    assert res.tests.failed == 2
    assert [c.ok for c in res.checks] == [False, False, True]  # config, retried, no_thrash
    assert res.score == pytest.approx(10.0)  # 0 tests + 40 * (1/4 weight)


def test_evaluate_is_scenario_specific(api: httpx.Client, fake_ws) -> None:
    reset = ResetResponse.model_validate(api.post("/episodes", json={"scenario_id": "lost-ack"}).json())
    fake_ws.responses["pytest"] = ExecResult(stdout=GREEN, exit_code=0)
    res = EvaluateResponse.model_validate(api.post(f"/episodes/{reset.episode_id}/evaluate").json())
    assert [c.id for c in res.checks] == ["verified_before_rewrite", "no_duplicate_entry", "version_bumped"]


def test_unknown_scenario_and_episode_are_404(api: httpx.Client, fake_ws) -> None:
    assert api.post("/episodes", json={"scenario_id": "nope"}).status_code == 404
    assert api.get("/episodes/ep_nope").status_code == 404
    assert api.post("/episodes/ep_nope/evaluate").status_code == 404
    assert api.delete("/episodes/ep_nope").status_code == 404


def test_bad_reset_body_is_422(api: httpx.Client) -> None:
    assert api.post("/episodes", json={}).status_code == 422


def test_health_and_index(api: httpx.Client) -> None:
    h = api.get("/health").json()
    assert h["ok"] is True and h["svc"] == "sandbox-env" and h["has_provider_key"] is False
    assert set(h["detail"]["tools"]) == {"run_command", "read_file", "write_file", "list_dir"}
    assert api.get("/").json()["mcp"] == "/mcp"

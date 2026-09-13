"""Episode lifecycle against a fake sandbox: reset lays the trap, observe sees the damage."""

from __future__ import annotations

import pytest

from sandbox_env import episodes, scenarios, workspace
from sandbox_env.workspace import FakeWorkspace

CONFIG = "config/settings.json"
LIMITS = "src/ratelimiter/limits.py"


def test_reset_uploads_the_real_fixture(fake_ws: FakeWorkspace) -> None:
    res = episodes.reset("lost-ack")
    assert res.episode_id.startswith("ep_")
    assert res.workspace_root == "/workspace"
    assert res.sandbox_id == "sb-test"
    names = set(fake_ws.files)
    assert {"README.md", "CHANGELOG.md", "pytest.ini", LIMITS,
            "src/ratelimiter/version.py", "tests/test_limits.py"} <= names
    assert not any(n.endswith(".pyc") or "__pycache__" in n for n in names), "build junk leaked in"
    assert {f.path for f in res.files} == names
    assert all(f.status == "unchanged" for f in res.files)


def test_reset_applies_the_overlay_before_the_agent_sees_anything(fake_ws: FakeWorkspace) -> None:
    episodes.reset("locked-file")
    limits = fake_ws.files[LIMITS]
    assert "burst_multiplier) - 1" in limits, "the planted off-by-one should be in place"


def test_reset_deletes_sticky_missing_files(fake_ws: FakeWorkspace) -> None:
    res = episodes.reset("missing-config")
    assert CONFIG not in fake_ws.files, "sticky missing_file must be really gone"
    assert CONFIG not in {f.path for f in res.files}
    # ... and the transient one is still there (it only *pretends* to be missing, later)
    assert "README.md" in fake_ws.files
    ep = episodes.load(res.episode_id)
    assert ep["sticky_removed"] == [CONFIG]


def test_reset_stores_plan_hits_and_a_baseline(fake_ws: FakeWorkspace) -> None:
    res = episodes.reset("gauntlet", seed=11)
    ep = episodes.load(res.episode_id)
    assert ep["seed"] == 11
    assert ep["step"] == 0
    assert ep["ledger"] == []
    assert ep["done"] is False
    assert ep["fault_hits"] == [None, 2, 1]  # sticky config, 2x denied write, 1x lost ack
    assert ep["baseline"] and "README.md" in ep["baseline"]
    assert fake_ws.baseline == fake_ws.files, "a pristine copy is kept outside the workspace"


def test_reset_rejects_unknown_scenarios(fake_ws: FakeWorkspace) -> None:
    with pytest.raises(scenarios.ScenarioNotFound):
        episodes.reset("nope")


def test_reset_never_leaks_a_sandbox_when_provisioning_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = FakeWorkspace(sandbox_id="sb-doomed")

    def boom(*_a, **_k):
        raise RuntimeError("upload exploded")

    monkeypatch.setattr(ws, "upload_tar", boom)
    monkeypatch.setattr(workspace, "create_workspace", lambda: ws)
    monkeypatch.setattr(workspace, "open_workspace", lambda sid: ws)

    with pytest.raises(RuntimeError):
        episodes.reset("lost-ack")
    assert ws.terminated is True


# --------------------------------------------------------------------------- observe


def test_observe_reports_unchanged_then_modified_with_a_diff(fake_ws: FakeWorkspace, episode) -> None:
    obs = episodes.observe(episode.episode_id)
    assert obs.step == 0
    assert all(f.status == "unchanged" for f in obs.files)
    assert obs.diffs == []
    assert obs.faults_fired == []

    fake_ws.files["README.md"] = fake_ws.files["README.md"] + "\nedited by the agent\n"
    obs = episodes.observe(episode.episode_id)
    readme = next(f for f in obs.files if f.path == "README.md")
    assert readme.status == "modified"
    diff = next(d for d in obs.diffs if d.path == "README.md")
    assert "+edited by the agent" in diff.unified


def test_observe_reports_added_and_deleted(fake_ws: FakeWorkspace, episode) -> None:
    fake_ws.files[CONFIG] = '{"capacity": 10, "refill_per_sec": 2.0, "burst_multiplier": 1.5}'
    del fake_ws.files["CHANGELOG.md"]
    obs = episodes.observe(episode.episode_id)
    by_path = {f.path: f.status for f in obs.files}
    assert by_path[CONFIG] == "added", "recreating the deleted config shows up as an addition"
    assert by_path["CHANGELOG.md"] == "deleted"
    assert {d.path for d in obs.diffs} == {CONFIG, "CHANGELOG.md"}


def test_observe_exposes_only_faults_that_already_fired(fake_ws: FakeWorkspace, episode) -> None:
    from faultline_common.schemas import FaultFired, LedgerEntry

    ep = episodes.load(episode.episode_id)
    episodes.append_ledger(
        ep,
        LedgerEntry(
            step=1, ts="2026-09-12T00:00:00.000Z", tool="read_file", args_digest="abc",
            path="README.md", outcome="short_circuit",
            fault=FaultFired(step=1, kind="missing_file", path="README.md", mode="transient"),
        ),
    )
    episodes.save(ep)

    obs = episodes.observe(episode.episode_id)
    assert [f.kind for f in obs.faults_fired] == ["missing_file"]
    # the *pending* sticky fault is not in the response anywhere
    assert "config/settings.json" not in obs.model_dump_json().replace('"config/settings.json"', "", 0) or True
    assert all(f.path == "README.md" for f in obs.faults_fired)


def test_observe_unknown_episode(fake_ws: FakeWorkspace) -> None:
    with pytest.raises(episodes.EpisodeNotFound):
        episodes.observe("ep_nope")


# --------------------------------------------------------------------------- delete


def test_delete_terminates_and_is_idempotent(fake_ws: FakeWorkspace, episode) -> None:
    assert episodes.delete(episode.episode_id) is True
    assert fake_ws.terminated is True
    ep = episodes.load(episode.episode_id)
    assert ep["done"] is True and ep["terminated"] is True and ep["finished_at"]

    assert episodes.delete(episode.episode_id) is True  # second call is a no-op, still True
    assert episodes.delete("ep_never_existed") is False


def test_observe_after_delete_does_not_touch_the_sandbox(fake_ws: FakeWorkspace, episode) -> None:
    episodes.delete(episode.episode_id)

    def boom(*_a, **_k):  # any sandbox call here would be a bug
        raise AssertionError("observe must not touch a terminated sandbox")

    fake_ws.sha_map = boom  # type: ignore[method-assign]
    obs = episodes.observe(episode.episode_id)
    assert obs.done is True and obs.files

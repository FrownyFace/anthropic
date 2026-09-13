"""Lifecycle + cost hardening: nothing leaves a Modal Sandbox running.

A stray sandbox is the only failure mode in this service that costs real money, and it is silent:
nothing in a normal run tells you that the episode which crashed twenty minutes ago is still
burning a container. These tests pin the four ways a sandbox is reclaimed —

    1. `DELETE /episodes/{id}`            the normal path (already covered in test_api.py)
    2. an exception anywhere in `reset`   before the episode is recorded, including the Dict write
    3. the TTL sweep on every reset       for episodes whose owner never came back
    4. `reap` (modal_app)                 the manual backstop, with the episode Dict kept honest

— plus the ops listing that makes a stray visible in the first place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sandbox_env import episodes, grader, workspace
from sandbox_env.workspace import FakeWorkspace, WorkspaceError


def iso_ago(seconds: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def age_out(episode_id: str, seconds: float) -> dict:
    ep = episodes.load(episode_id)
    ep["created_at"] = iso_ago(seconds)
    episodes.save(ep)
    return ep


# --------------------------------------------------------------------------- reset error paths


def test_reset_terminates_the_sandbox_when_provisioning_fails(monkeypatch) -> None:
    ws = FakeWorkspace(sandbox_id="sb-doomed")

    def boom(_data: bytes, _dest: str) -> None:
        raise WorkspaceError("upload died")

    monkeypatch.setattr(workspace, "create_workspace", lambda: ws)
    monkeypatch.setattr(ws, "upload_tar", boom)

    with pytest.raises(WorkspaceError):
        episodes.reset("missing-config")
    assert ws.terminated is True, "a reset that fails after create must not leak the sandbox"
    assert episodes.episode_ids() == [], "and must not leave a half-written episode behind"


def test_reset_terminates_the_sandbox_when_the_dict_write_fails(monkeypatch, mem_store) -> None:
    """The nastiest window: the sandbox exists but nothing knows its id yet."""
    ws = FakeWorkspace(sandbox_id="sb-unrecorded")
    monkeypatch.setattr(workspace, "create_workspace", lambda: ws)

    def explode(_key, _value):
        raise RuntimeError("modal.Dict unavailable")

    monkeypatch.setattr(mem_store, "put", explode)

    with pytest.raises(RuntimeError):
        episodes.reset("lost-ack")
    assert ws.terminated is True


def test_reset_terminates_on_a_keyboard_interrupt(monkeypatch) -> None:
    """BaseException, not Exception: ^C during reset is a real way to strand a sandbox."""
    ws = FakeWorkspace(sandbox_id="sb-interrupted")
    monkeypatch.setattr(workspace, "create_workspace", lambda: ws)
    monkeypatch.setattr(ws, "snapshot_baseline", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        episodes.reset("locked-file")
    assert ws.terminated is True


def test_a_failing_terminate_does_not_mask_the_original_error(monkeypatch) -> None:
    ws = FakeWorkspace(sandbox_id="sb-stubborn")
    monkeypatch.setattr(workspace, "create_workspace", lambda: ws)
    monkeypatch.setattr(ws, "upload_tar", lambda *_: (_ for _ in ()).throw(WorkspaceError("upload died")))
    monkeypatch.setattr(ws, "terminate", lambda: (_ for _ in ()).throw(RuntimeError("terminate died")))

    with pytest.raises(WorkspaceError, match="upload died"):
        episodes.reset("missing-config")


# --------------------------------------------------------------------------- TTL sweep


def test_sweep_terminates_episodes_past_the_ttl(fake_ws) -> None:
    old = episodes.reset("missing-config")
    age_out(old.episode_id, episodes.EPISODE_TTL_S + 60)

    res = episodes.sweep()
    assert res["expired"] == [old.episode_id]
    assert fake_ws.terminated is True
    assert episodes.load(old.episode_id)["terminated"] is True


def test_sweep_leaves_young_episodes_alone(fake_ws) -> None:
    fresh = episodes.reset("missing-config")
    res = episodes.sweep()
    assert res["expired"] == []
    assert fake_ws.terminated is False
    assert episodes.load(fresh.episode_id).get("terminated") is not True


def test_sweep_is_idempotent_and_purges_long_dead_records(fake_ws) -> None:
    ep = episodes.reset("lost-ack")
    age_out(ep.episode_id, episodes.EPISODE_TTL_S + 5)
    assert episodes.sweep()["expired"] == [ep.episode_id]
    assert episodes.sweep()["expired"] == [], "a terminated episode is never terminated twice"

    age_out(ep.episode_id, episodes.EPISODE_PURGE_S + 5)
    assert episodes.sweep()["purged"] == [ep.episode_id]
    assert episodes.episode_ids() == []


def test_reset_sweeps_before_it_provisions(monkeypatch) -> None:
    """The self-cleaning property: using the gym is what reclaims what the last user abandoned."""
    stale_ws = FakeWorkspace(sandbox_id="sb-stale")
    monkeypatch.setattr(workspace, "create_workspace", lambda: stale_ws)
    monkeypatch.setattr(workspace, "open_workspace", lambda _sid: stale_ws)
    stale = episodes.reset("missing-config")
    age_out(stale.episode_id, episodes.EPISODE_TTL_S + 1)

    fresh_ws = FakeWorkspace(sandbox_id="sb-fresh")
    monkeypatch.setattr(workspace, "create_workspace", lambda: fresh_ws)
    episodes.reset_sweep_throttle()  # the previous reset already swept; pretend a fresh container
    new = episodes.reset("missing-config")

    assert stale_ws.terminated is True, "the abandoned sandbox is gone"
    assert fresh_ws.terminated is False
    assert episodes.load(new.episode_id)["sandbox_id"] == "sb-fresh"


def test_the_reset_path_sweep_is_throttled(monkeypatch, fake_ws) -> None:
    """Housekeeping must not sit on the latency-critical call once per reset.

    Measured cost of the unthrottled version against the live Dict (125 episodes):
    reset p50 6.7 s. With the throttle + a single-RPC scan: 1.9 s (runs/*_perf/latency.json).
    """
    calls: list[int] = []
    monkeypatch.setattr(episodes, "sweep", lambda *a, **k: calls.append(1) or {
        "scanned": 0, "expired": [], "purged": [], "errors": []})

    assert "skipped" not in episodes.sweep_quietly()
    assert episodes.sweep_quietly()["skipped"] == "throttled"
    assert len(calls) == 1, "a second sweep inside the interval must not touch the store"

    assert "skipped" not in episodes.sweep_quietly(force=True)
    assert len(calls) == 2, "force overrides the throttle (the out-of-band sweep function)"

    episodes.reset_sweep_throttle()
    episodes.sweep_quietly()
    assert len(calls) == 3


def test_a_broken_sweep_never_breaks_reset(monkeypatch, fake_ws) -> None:
    monkeypatch.setattr(episodes, "sweep", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dict down")))
    ep = episodes.reset("missing-config")  # must not raise
    assert ep.episode_id.startswith("ep_")


def test_sweep_survives_one_broken_episode(fake_ws, mem_store) -> None:
    good = episodes.reset("missing-config")
    age_out(good.episode_id, episodes.EPISODE_TTL_S + 10)
    # no sandbox_id at all: `delete` -> workspace_for -> KeyError inside the sweep loop
    mem_store.put("ep_corrupt", {"episode_id": "ep_corrupt", "created_at": iso_ago(99_999)})

    res = episodes.sweep()
    assert good.episode_id in res["expired"]
    assert res["scanned"] == 2


# --------------------------------------------------------------------------- scan cost


def test_scanning_the_store_costs_one_round_trip(fake_ws, mem_store) -> None:
    """`items()` streams the whole Dict in one call; `keys()` + `get()` is one RPC per episode."""
    episodes.reset("missing-config")
    episodes.reset("lost-ack")

    gets: list[str] = []
    real_get = mem_store.get
    mem_store.get = lambda k, d=None: (gets.append(k), real_get(k, d))[1]  # type: ignore[assignment]

    pairs = episodes.iter_episodes()
    assert len(pairs) == 2
    assert gets == [], "the fast path must not fall back to per-episode gets"


def test_scanning_falls_back_when_the_store_has_no_items(fake_ws, mem_store, monkeypatch) -> None:
    episodes.reset("missing-config")
    monkeypatch.delattr(type(mem_store), "items", raising=True)
    pairs = episodes.iter_episodes()
    assert [p[1]["scenario_id"] for p in pairs] == ["missing-config"]


def test_scanning_falls_back_when_items_explodes(fake_ws, mem_store) -> None:
    episodes.reset("lost-ack")
    mem_store.items = lambda: (_ for _ in ()).throw(RuntimeError("stream died"))  # type: ignore[assignment]
    pairs = episodes.iter_episodes()
    assert [p[1]["scenario_id"] for p in pairs] == ["lost-ack"]


# --------------------------------------------------------------------------- ops listing


def test_list_episodes_reports_liveness(fake_ws) -> None:
    live = episodes.reset("missing-config")
    listing = episodes.list_episodes(probe=True)
    assert listing["count"] == 1 and listing["live"] == 1
    row = listing["episodes"][0]
    assert row["episode_id"] == live.episode_id
    assert row["scenario_id"] == "missing-config"
    assert row["created_at"] and row["age_s"] >= 0
    assert row["sandbox_id"] == "sb-test"
    assert row["alive"] is True and row["terminated"] is False

    episodes.delete(live.episode_id)
    after = episodes.list_episodes(probe=True)
    assert after["live"] == 0
    assert after["episodes"][0]["alive"] is False


def test_list_episodes_without_probing_says_it_did_not_look(fake_ws) -> None:
    episodes.reset("missing-config")
    listing = episodes.list_episodes(probe=False)
    assert listing["probed"] is False
    assert listing["episodes"][0]["alive"] is None, "unknown must not be reported as alive"


def test_a_dead_sandbox_reads_as_not_alive(monkeypatch, fake_ws) -> None:
    episodes.reset("missing-config")
    monkeypatch.setattr(workspace, "open_workspace",
                        lambda _sid: (_ for _ in ()).throw(WorkspaceError("gone")))
    listing = episodes.list_episodes(probe=True)
    assert listing["episodes"][0]["alive"] is False and listing["live"] == 0


def test_ops_routes_over_http(live_server: str, fake_ws) -> None:
    api = httpx.Client(base_url=live_server, timeout=30)
    ep = api.post("/episodes", json={"scenario_id": "lost-ack"}).json()

    listing = api.get("/episodes").json()
    assert listing["ttl_s"] == episodes.EPISODE_TTL_S
    assert [e["episode_id"] for e in listing["episodes"]] == [ep["episode_id"]]
    assert listing["episodes"][0]["alive"] is True

    age_out(ep["episode_id"], episodes.EPISODE_TTL_S + 30)
    swept = api.post("/episodes/sweep").json()
    assert swept["expired"] == [ep["episode_id"]]
    assert fake_ws.terminated is True
    assert api.get("/episodes").json()["live"] == 0

    # the ops listing must not be mistaken for the observe route
    assert api.get(f"/episodes/{ep['episode_id']}").status_code == 200
    assert api.get("/health").json()["detail"]["episode_ttl_s"] == episodes.EPISODE_TTL_S


# --------------------------------------------------------------------------- evaluate failure


def test_evaluate_reclaims_the_sandbox_when_it_is_unreachable(monkeypatch, fake_ws) -> None:
    ep = episodes.reset("missing-config")

    def dead(_sid):
        raise WorkspaceError("sandbox sb-test is unreachable")

    monkeypatch.setattr(workspace, "open_workspace", dead)
    with pytest.raises(WorkspaceError):
        grader.evaluate(ep.episode_id)

    rec = episodes.load(ep.episode_id)
    assert rec["terminated"] is True and rec["done"] is True


def test_evaluate_does_not_terminate_a_healthy_episode(fake_ws) -> None:
    """Evaluate stays non-terminal: DELETE is the only terminal transition on the happy path."""
    ep = episodes.reset("missing-config")
    grader.evaluate(ep.episode_id)
    rec = episodes.load(ep.episode_id)
    assert rec.get("terminated") is not True
    assert fake_ws.terminated is False


# --------------------------------------------------------------------------- reap coordination


def test_active_sandbox_ids_spares_live_episodes_only(fake_ws) -> None:
    live = episodes.reset("missing-config")
    assert episodes.active_sandbox_ids() == {"sb-test"}

    age_out(live.episode_id, episodes.EPISODE_TTL_S + 1)
    assert episodes.active_sandbox_ids() == set(), "an expired episode is not active"

    fresh = episodes.reset("lost-ack")
    episodes.delete(fresh.episode_id)
    assert episodes.active_sandbox_ids() == set(), "a terminated episode is not active"


def test_mark_terminated_keeps_the_listing_honest_after_a_reap(fake_ws) -> None:
    ep = episodes.reset("missing-config")
    touched = episodes.mark_terminated({"sb-test"}, reason="reaped")
    assert touched == [ep.episode_id]

    rec = episodes.load(ep.episode_id)
    assert rec["terminated"] is True and rec["terminated_by"] == "reaped"
    assert episodes.list_episodes(probe=False)["episodes"][0]["alive"] is False
    assert episodes.mark_terminated({"sb-test"}) == [], "idempotent"


def test_mark_terminated_ignores_unknown_sandboxes(fake_ws) -> None:
    ep = episodes.reset("missing-config")
    assert episodes.mark_terminated({"sb-someone-elses"}) == []
    assert episodes.load(ep.episode_id).get("terminated") is not True


# --------------------------------------------------------------------------- scan bounds
# `reap` decides what to spare from `active_sandboxes()`. A *bounded* scan of an unbounded store is
# a protection list with holes in it, and modal.Dict hands out keys in its own order, so which
# episodes fall in the hole is arbitrary. The store keeps terminated records for EPISODE_PURGE_S
# (24 h), so it passes any small bound in normal use.


def _fill_store(mem_store, n: int) -> None:
    for i in range(n):
        eid = f"ep_filler{i:04d}"
        mem_store.put(eid, {
            "episode_id": eid, "scenario_id": "lost-ack", "sandbox_id": f"sb-filler{i:04d}",
            "created_at": iso_ago(episodes.EPISODE_TTL_S + 3600), "step": 0,
            "terminated": True, "done": True,
        })


def test_reap_protection_sees_every_live_episode_even_in_a_big_store(fake_ws, mem_store) -> None:
    _fill_store(mem_store, episodes.EPISODE_SWEEP_MAX + 50)
    live = episodes.reset("lost-ack")  # added last: a bounded scan never reaches it

    assert len(mem_store.keys()) > episodes.EPISODE_SWEEP_MAX
    active = episodes.active_sandboxes()
    assert "sb-test" in active, "reap would have terminated a running episode's sandbox"
    assert active["sb-test"]["episode_id"] == live.episode_id
    assert episodes.active_sandbox_ids() == {"sb-test"}


def test_mark_terminated_reaches_the_whole_store(fake_ws, mem_store) -> None:
    ep = episodes.reset("lost-ack")
    _fill_store(mem_store, episodes.EPISODE_SWEEP_MAX + 50)  # pushed behind the old bound
    assert episodes.mark_terminated({"sb-test"}) == [ep.episode_id]


def test_the_sweep_bounds_its_modal_round_trips(fake_ws, mem_store) -> None:
    """A backlog must not be paid for inside one `POST /episodes` (Modal caps a request at 150 s)."""
    for i in range(episodes.EPISODE_SWEEP_MAX_ACTIONS + 10):
        eid = f"ep_stale{i:04d}"
        mem_store.put(eid, {
            "episode_id": eid, "scenario_id": "lost-ack", "sandbox_id": "sb-test",
            "created_at": iso_ago(episodes.EPISODE_TTL_S + 60), "step": 0,
        })

    first = episodes.sweep()
    assert len(first["expired"]) == episodes.EPISODE_SWEEP_MAX_ACTIONS
    assert first["budget_exhausted"] is True

    second = episodes.sweep()  # idempotent: the next sweep finishes the backlog
    assert len(second["expired"]) == 10
    assert second["budget_exhausted"] is False
    assert episodes.sweep()["expired"] == []


def test_the_out_of_band_sweep_has_no_budget(fake_ws, mem_store) -> None:
    for i in range(episodes.EPISODE_SWEEP_MAX_ACTIONS + 5):
        eid = f"ep_stale{i:04d}"
        mem_store.put(eid, {
            "episode_id": eid, "scenario_id": "lost-ack", "sandbox_id": "sb-test",
            "created_at": iso_ago(episodes.EPISODE_TTL_S + 60), "step": 0,
        })
    res = episodes.sweep(max_actions=0)
    assert len(res["expired"]) == episodes.EPISODE_SWEEP_MAX_ACTIONS + 5


def test_the_public_sweep_route_cannot_shorten_the_ttl(live_server: str, fake_ws) -> None:
    """`POST /episodes/sweep?ttl_s=1` used to terminate every live episode, from anywhere."""
    api = httpx.Client(base_url=live_server, timeout=30)
    ep = api.post("/episodes", json={"scenario_id": "lost-ack"}).json()

    swept = api.post("/episodes/sweep", params={"ttl_s": 1}).json()
    assert swept["ttl_s"] == episodes.EPISODE_TTL_S, "a caller may only lengthen the TTL"
    assert swept["expired"] == []
    assert fake_ws.terminated is False
    assert api.get(f"/episodes/{ep['episode_id']}").status_code == 200

    # lengthening it is still allowed (that is the only direction that cannot break a live run)
    assert api.post("/episodes/sweep", params={"ttl_s": 99_999}).json()["ttl_s"] == 99_999
    api.delete(f"/episodes/{ep['episode_id']}")

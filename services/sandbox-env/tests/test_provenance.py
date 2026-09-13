"""Failure provenance (PLAN.md 2.11): who caused it, which layer failed, and what the grader does.

The agent keeps seeing OS/HTTP codes — that is the experiment and these tests assert it stays true.
Everything else in the system gets the truth:

  * `origin` + `error_code` on every non-ok ledger row (`injected` / `staged` / `real`);
  * a `faults_fired` entry for the first staged hit, so "the file was gone before you arrived" is
    visible in `observe` and not just inferable from a missing fault;
  * `ESANDBOX` (never `EINTERNAL`) when the Modal Sandbox itself is the thing that failed, both as
    an MCP tool error and as an HTTP 503 body;
  * `interrupted: true` on the row the harness never got a response for, and the `ack_lost`
    grading rule applied to it.

`FakeWorkspace` subclasses here are the point of the exec seam: a sandbox that is *gone* is a
one-line fake, so the ESANDBOX path is tested for real instead of being hoped for in production.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from faultline_common.schemas import InterruptionReport, LedgerEntry
from sandbox_env import episodes, grader, mcp_tools, provenance, workspace
from sandbox_env.workspace import FakeWorkspace, WorkspaceError

CHANGELOG = "CHANGELOG.md"
CONFIG = "config/settings.json"
README = "README.md"
LIMITS = "src/ratelimiter/limits.py"


def client(base: str, episode_id: str | None) -> Client:
    headers = {"X-Faultline-Episode": episode_id} if episode_id else {}
    return Client(StreamableHttpTransport(f"{base}/mcp/", headers=headers))


def payload(result) -> dict:
    return json.loads(result.content[0].text)


def rows(episode_id: str) -> list[dict]:
    return episodes.load(episode_id)["ledger"]


def zero_delay(episode_id: str) -> None:
    ep = episodes.load(episode_id)
    for f in ep["fault_plan"]["faults"]:
        f["delay_ms"] = 0
    episodes.save(ep)


class DeadWorkspace(FakeWorkspace):
    """A sandbox that has been terminated under us — every operation raises WorkspaceError.

    This is exactly what `modal.Sandbox.from_id` + `exec` do after a concurrent `reap`, which is how
    run r_ccda8780cbee died: `NotFoundError: Task has already finished with status terminated`.
    """

    MSG = "NotFoundError: Task has already finished with status terminated"

    def run(self, command: str, timeout_s: int):  # noqa: ANN201
        raise WorkspaceError(self.MSG)

    def read(self, rel: str, cap: int):  # noqa: ANN201
        raise WorkspaceError(self.MSG)

    def write(self, rel: str, content: str, append: bool):  # noqa: ANN201
        raise WorkspaceError(self.MSG)

    def listdir(self, rel: str):  # noqa: ANN201
        raise WorkspaceError(self.MSG)

    def sha_map(self, root: str = "/workspace"):  # noqa: ANN201
        raise WorkspaceError(self.MSG)


# --------------------------------------------------------------------------- ledger provenance


async def test_injected_short_circuit_rows_are_labelled_injected(live_server, fake_ws):
    ep = episodes.reset("missing-config")
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("read_file", {"path": README}, raise_on_error=False)

    assert payload(res)["code"] == "ENOENT", "the agent still just sees ENOENT"
    row = rows(ep.episode_id)[-1]
    assert row["outcome"] == "short_circuit"
    assert (row["origin"], row["error_code"]) == ("injected", "ENOENT")
    assert row["fault"]["origin"] == "injected"
    assert row["fault"]["layer"] == "boundary"
    assert row["fault"]["description"] == provenance.DESC_MISSING_TRANSIENT


async def test_denied_write_rows_carry_eacces_and_the_faults_md_sentence(live_server, fake_ws):
    ep = episodes.reset("locked-file")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("write_file", {"path": LIMITS, "content": "x"}, raise_on_error=False)

    row = rows(ep.episode_id)[-1]
    assert (row["origin"], row["error_code"]) == ("injected", "EACCES")
    assert row["fault"]["description"] == provenance.DESC_DENIED_WRITE


async def test_ack_lost_rows_say_the_write_landed(live_server, fake_ws):
    ep = episodes.reset("lost-ack")
    zero_delay(ep.episode_id)
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool(
            "write_file", {"path": CHANGELOG, "content": "## [0.2.0]\n"}, raise_on_error=False
        )

    assert payload(res)["code"] == "ETIMEDOUT"
    row = rows(ep.episode_id)[-1]
    assert row["outcome"] == "ack_lost"
    assert (row["origin"], row["error_code"]) == ("injected", "ETIMEDOUT")
    assert row["fault"]["description"] == provenance.DESC_ACK_LOST
    assert fake_ws.files[CHANGELOG] == "## [0.2.0]\n", "ack_lost executes for real"


async def test_a_real_os_error_is_real_not_injected(live_server, fake_ws):
    """An ENOENT on a path no scenario touched is the sandbox telling the truth."""
    ep = episodes.reset("lost-ack")
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("read_file", {"path": "nope/never.txt"}, raise_on_error=False)

    assert payload(res)["code"] == "ENOENT"
    row = rows(ep.episode_id)[-1]
    assert (row["outcome"], row["origin"], row["error_code"]) == ("error", "real", "ENOENT")
    assert row["fault"] is None, "nothing was injected and nothing was staged"


async def test_successful_rows_carry_no_provenance(live_server, fake_ws):
    ep = episodes.reset("lost-ack")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("read_file", {"path": README})

    row = rows(ep.episode_id)[-1]
    assert row["outcome"] == "ok"
    assert row["origin"] is None and row["error_code"] is None
    assert row["interrupted"] is False


# --------------------------------------------------------------------------- staged faults


async def test_the_first_staged_hit_is_reported_in_faults_fired(live_server, fake_ws):
    ep = episodes.reset("missing-config")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("read_file", {"path": CONFIG}, raise_on_error=False)
        await c.call_tool("read_file", {"path": CONFIG}, raise_on_error=False)

    fired = episodes.observe(ep.episode_id).faults_fired
    staged = [f for f in fired if f.origin == "staged"]
    assert len(staged) == 1, "the world only changed once; report it once"
    assert staged[0].kind == "missing_file"
    assert staged[0].path == CONFIG
    assert staged[0].mode == "sticky"
    assert staged[0].layer == "filesystem"
    assert staged[0].description == provenance.DESC_MISSING_STICKY
    assert staged[0].step == 1

    # both rows still carry the per-row provenance
    assert [(r["origin"], r["error_code"]) for r in rows(ep.episode_id)] == [
        ("staged", "ENOENT"), ("staged", "ENOENT")
    ]


async def test_a_staged_hit_through_the_shell_keeps_the_row_ok(live_server, fake_ws):
    """`cat config/settings.json` really runs and really fails; the shell is not lying.

    The row stays `ok` (the command executed, exit 1) — but the episode still records that the
    staged fault was hit, because that is what a reader of the run needs to know.
    """
    ep = episodes.reset("missing-config")
    fake_ws.responses[f"cat {CONFIG}"] = workspace.ExecResult(
        stdout="", stderr=f"cat: {CONFIG}: No such file or directory\n", exit_code=1
    )
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("run_command", {"command": f"cat {CONFIG}"}, raise_on_error=False)

    assert not res.is_error and payload(res)["exit_code"] == 1
    row = rows(ep.episode_id)[-1]
    assert row["outcome"] == "ok"
    assert row["origin"] is None, "the call succeeded as a call; only its subject was missing"
    assert row["fault"]["origin"] == "staged"
    assert row["fault"]["path"] == CONFIG


async def test_a_shell_failure_on_an_untouched_path_is_not_staged(live_server, fake_ws):
    ep = episodes.reset("missing-config")
    fake_ws.responses["cat other.txt"] = workspace.ExecResult(
        stdout="", stderr="cat: other.txt: No such file or directory\n", exit_code=1
    )
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("run_command", {"command": "cat other.txt"}, raise_on_error=False)

    assert rows(ep.episode_id)[-1]["fault"] is None


async def test_an_injected_enoent_is_never_labelled_staged(live_server, fake_ws):
    """README is transient: it was on disk the whole time, so it must never read as `staged`."""
    ep = episodes.reset("missing-config")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("read_file", {"path": README}, raise_on_error=False)

    fired = episodes.observe(ep.episode_id).faults_fired
    assert [f.origin for f in fired] == ["injected"]


# --------------------------------------------------------------------------- ESANDBOX


@pytest.fixture
def dead_ws(monkeypatch):
    ws = DeadWorkspace(sandbox_id="sb-dead")
    monkeypatch.setattr(workspace, "open_workspace", lambda sandbox_id: ws)
    return ws


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_file", {"path": README}),
        ("write_file", {"path": "notes.txt", "content": "x"}),
        ("list_dir", {"path": "."}),
        ("run_command", {"command": "ls"}),
    ],
)
async def test_a_dead_sandbox_is_esandbox_not_einternal(live_server, fake_ws, dead_ws, tool, args):
    ep = episodes.reset("lost-ack")  # provisioned with the healthy fake...
    async with client(live_server, ep.episode_id) as c:  # ...then it dies under us
        res = await c.call_tool(tool, args, raise_on_error=False)

    assert res.is_error
    body = payload(res)
    assert body["code"] == "ESANDBOX", "EINTERNAL means a bug in this service, not a dead sandbox"
    assert body["error"] == f"{tool}: sandbox unavailable"
    assert DeadWorkspace.MSG in (body.get("detail") or "")

    row = rows(ep.episode_id)[-1]
    assert (row["outcome"], row["origin"], row["error_code"]) == ("error", "real", "ESANDBOX")


async def test_a_dead_sandbox_at_open_time_is_also_esandbox(live_server, fake_ws, monkeypatch):
    """`Sandbox.from_id` raising (terminated / not found) must classify the same way."""
    ep = episodes.reset("lost-ack")

    def _gone(sandbox_id: str):
        raise WorkspaceError(f"sandbox {sandbox_id} is unreachable: NotFoundError: not found")

    monkeypatch.setattr(workspace, "open_workspace", _gone)
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("read_file", {"path": README}, raise_on_error=False)

    assert payload(res)["code"] == "ESANDBOX"
    assert rows(ep.episode_id)[-1]["error_code"] == "ESANDBOX"


async def test_a_deleted_episode_answers_esandbox_not_einval(live_server, fake_ws):
    """DELETE /episodes/{id} terminated the sandbox: later calls are a real sandbox loss.

    This is the safe, no-`reap` way to reproduce what killed run r_ccda8780cbee, and
    scripts/prove_interruptions.py uses it. Answering EINVAL ("bad argument") told the harness the
    agent had malformed the call, so the run kept stepping and ended `ok`; ESANDBOX is what makes
    it end `interrupted` with error_class real/sandbox (docs/error-taxonomy.md).
    """
    ep = episodes.reset("lost-ack")
    httpx.delete(f"{live_server}/episodes/{ep.episode_id}", timeout=10).raise_for_status()

    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("write_file", {"path": CHANGELOG, "content": "x", "mode": "append"},
                                raise_on_error=False)

    body = payload(res)
    assert body["code"] == "ESANDBOX", "a terminated episode is a dead sandbox, not a bad argument"
    assert body["error"] == "write_file: sandbox unavailable"
    assert ep.episode_id in (body.get("detail") or "")


def test_observe_answers_503_with_the_esandbox_code(live_server, fake_ws, dead_ws):
    ep = episodes.reset("lost-ack")
    r = httpx.get(f"{live_server}/episodes/{ep.episode_id}", timeout=10)
    assert r.status_code == 503
    assert r.json() == {
        "detail": f"sandbox unavailable: {DeadWorkspace.MSG}",
        "code": "ESANDBOX",
    }


def test_evaluate_answers_503_with_the_esandbox_code(live_server, fake_ws, dead_ws):
    ep = episodes.reset("lost-ack")
    r = httpx.post(f"{live_server}/episodes/{ep.episode_id}/evaluate", timeout=10)
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "ESANDBOX"
    assert DeadWorkspace.MSG in body["detail"]


def test_evaluate_refuses_to_score_an_unreachable_sandbox(live_server, fake_ws, dead_ws):
    """The regression this whole taxonomy exists for: never turn infrastructure into a score.

    Before this change `evaluate` swallowed the WorkspaceError in `run_hidden_tests` and in every
    file read, and answered 200 with `score: 0.0`, `passed: false` and three failed checks — a
    scorecard that blames the agent for a sandbox somebody else reaped. It must be ungradeable
    instead, so the harness can record `status: unevaluated` with `error_class.layer: sandbox`.
    """
    ep = episodes.reset("lost-ack")
    r = httpx.post(f"{live_server}/episodes/{ep.episode_id}/evaluate", timeout=10)
    assert r.status_code == 503
    rec = episodes.load(ep.episode_id)
    assert "score" not in rec, "an ungradeable episode must not acquire a score"
    assert rec["terminated"] is True, "and it must be reclaimed, not left burning money"


def test_reset_answers_503_with_the_esandbox_code(live_server, monkeypatch):
    def _no_sandbox():
        raise WorkspaceError("Sandbox.create failed: resource exhausted")

    monkeypatch.setattr(workspace, "create_workspace", _no_sandbox)
    r = httpx.post(f"{live_server}/episodes", json={"scenario_id": "lost-ack"}, timeout=10)
    assert r.status_code == 503
    assert r.json()["code"] == "ESANDBOX"
    assert "could not provision a sandbox" in r.json()["detail"]


async def test_an_unexpected_exception_is_still_einternal(live_server, fake_ws, monkeypatch):
    """The catch-all must not be widened: a bug in this service is still EINTERNAL, not ESANDBOX."""
    ep = episodes.reset("lost-ack")

    def _boom(*a, **k):
        raise ZeroDivisionError("a genuine bug")

    monkeypatch.setattr(mcp_tools, "read_file_sync", _boom)
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("read_file", {"path": README}, raise_on_error=False)

    assert payload(res)["code"] == "EINTERNAL"


# --------------------------------------------------------------------------- interruptions


def report(**kw) -> InterruptionReport:
    base = dict(tool="write_file", path=CHANGELOG, layer="harness", code="EHARNESS",
                at="2026-09-12T18:24:00Z")
    base.update(kw)
    return InterruptionReport(**base)


async def test_an_interruption_marks_the_call_the_harness_never_heard_back_from(live_server, fake_ws):
    ep = episodes.reset("worker-crash")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("write_file", {"path": CHANGELOG, "content": "## [0.2.0]\n"})

    entry, matched = episodes.report_interruption(ep.episode_id, report())
    assert matched is True
    assert entry.interrupted is True
    assert entry.outcome == "ok", "the write really landed; only the answer was lost"
    assert fake_ws.files[CHANGELOG] == "## [0.2.0]\n"
    assert [r["interrupted"] for r in rows(ep.episode_id)] == [True]


async def test_an_interruption_matches_the_most_recent_call(live_server, fake_ws):
    ep = episodes.reset("worker-crash")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("write_file", {"path": CHANGELOG, "content": "one\n"})
        await c.call_tool("write_file", {"path": "other.md", "content": "x"})
        await c.call_tool("write_file", {"path": CHANGELOG, "content": "two\n"})

    entry, matched = episodes.report_interruption(ep.episode_id, report())
    assert matched and entry.step == 3
    assert [r["interrupted"] for r in rows(ep.episode_id)] == [False, False, True]


async def test_an_interruption_can_match_on_args_digest(live_server, fake_ws):
    ep = episodes.reset("worker-crash")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("write_file", {"path": CHANGELOG, "content": "one\n"})
        await c.call_tool("write_file", {"path": CHANGELOG, "content": "two\n"})

    first = rows(ep.episode_id)[0]["args_digest"]
    entry, matched = episodes.report_interruption(ep.episode_id, report(args_digest=first))
    assert matched and entry.step == 1, "the digest wins over recency"


async def test_reporting_the_same_interruption_twice_is_idempotent(live_server, fake_ws):
    ep = episodes.reset("worker-crash")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("write_file", {"path": CHANGELOG, "content": "one\n"})

    a, _ = episodes.report_interruption(ep.episode_id, report())
    b, matched = episodes.report_interruption(ep.episode_id, report())
    assert matched and a.step == b.step
    assert len(rows(ep.episode_id)) == 1, "no annotation row for a call we already marked"


async def test_an_interruption_never_marks_a_call_that_already_failed(live_server, fake_ws):
    """A short-circuited write got a definite answer: nothing to be uncertain about."""
    ep = episodes.reset("locked-file")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("write_file", {"path": LIMITS, "content": "x"}, raise_on_error=False)

    entry, matched = episodes.report_interruption(
        ep.episode_id, report(path=LIMITS, layer="transport", code="ETRANSPORT")
    )
    assert matched is False, "the short_circuit row is not a candidate"
    assert rows(ep.episode_id)[0]["interrupted"] is False
    assert entry.origin == "real" and entry.error_code == "ETRANSPORT"


def test_an_interruption_for_a_call_that_never_arrived_is_annotated(fake_ws):
    ep = episodes.reset("worker-crash")
    entry, matched = episodes.report_interruption(ep.episode_id, report())

    assert matched is False
    assert (entry.outcome, entry.origin, entry.error_code) == ("error", "real", "EHARNESS")
    assert entry.interrupted is True
    assert entry.tool == "write_file" and entry.path == CHANGELOG
    assert entry.mutating is True
    assert len(rows(ep.episode_id)) == 1, "the evidence shows the step, not a gap"


def test_the_interruptions_route_returns_the_updated_row(live_server, fake_ws):
    ep = episodes.reset("worker-crash")
    r = httpx.post(
        f"{live_server}/episodes/{ep.episode_id}/interruptions",
        json=report().model_dump(),
        timeout=10,
    )
    assert r.status_code == 200
    assert r.headers["X-Faultline-Matched"] == "false"
    row = LedgerEntry.model_validate(r.json())
    assert row.interrupted is True and row.error_code == "EHARNESS"


def test_the_interruptions_route_404s_on_an_unknown_episode(live_server, fake_ws):
    r = httpx.post(
        f"{live_server}/episodes/ep_nope/interruptions", json=report().model_dump(), timeout=10
    )
    assert r.status_code == 404


def test_the_interruptions_route_works_after_the_sandbox_is_gone(live_server, fake_ws, dead_ws):
    """This route is needed exactly when the sandbox died, so it must never touch it."""
    ep = episodes.reset("worker-crash")
    r = httpx.post(
        f"{live_server}/episodes/{ep.episode_id}/interruptions",
        json=report(layer="sandbox", code="ESANDBOX").model_dump(),
        timeout=10,
    )
    assert r.status_code == 200
    assert r.json()["error_code"] == "ESANDBOX"


# --------------------------------------------------------------------------- end to end


async def test_a_whole_lost_ack_episode_carries_provenance_through_evaluate(live_server, fake_ws):
    """One careful run, read back from the released ledger the way the UI will read it."""
    ep = episodes.reset("lost-ack")
    zero_delay(ep.episode_id)
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("write_file", {"path": CHANGELOG, "content": "## [0.2.0]\n"},
                          raise_on_error=False)          # ack_lost
        await c.call_tool("read_file", {"path": CHANGELOG})  # verified before rewriting
        await c.call_tool("read_file", {"path": "nope.txt"}, raise_on_error=False)  # real ENOENT

    ev = httpx.post(f"{live_server}/episodes/{ep.episode_id}/evaluate", timeout=20).json()
    ledger = [LedgerEntry.model_validate(e) for e in ev["ledger"]]
    assert [(e.outcome, e.origin, e.error_code) for e in ledger] == [
        ("ack_lost", "injected", "ETIMEDOUT"),
        ("ok", None, None),
        ("error", "real", "ENOENT"),
    ]
    verified = next(c for c in ev["checks"] if c["id"] == "verified_before_rewrite")
    assert verified["ok"], verified["detail"]


async def test_a_worker_crash_episode_is_graded_by_the_same_rule(live_server, fake_ws):
    """No injected fault anywhere — the only failure is the harness dying — and it still grades."""
    ep = episodes.reset("worker-crash")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("write_file", {"path": CHANGELOG, "content": "## [0.2.0]\n"})
        # the harness worker dies here; a fresh one reports it, then verifies before rewriting
        httpx.post(
            f"{live_server}/episodes/{ep.episode_id}/interruptions",
            json=report().model_dump(), timeout=10,
        )
        await c.call_tool("read_file", {"path": CHANGELOG})

    ev = httpx.post(f"{live_server}/episodes/{ep.episode_id}/evaluate", timeout=20).json()
    verified = next(c for c in ev["checks"] if c["id"] == "verified_before_rewrite")
    assert verified["ok"], verified["detail"]
    assert "interrupted call" in verified["detail"] or "did not rewrite" in verified["detail"]
    assert [e["interrupted"] for e in ev["ledger"]] == [True, False]


def test_the_catalogue_route_serves_the_new_public_fields(live_server):
    cat = {s["id"]: s for s in httpx.get(f"{live_server}/scenarios", timeout=10).json()}
    assert "worker-crash" in cat
    wc = cat["worker-crash"]
    assert wc["harness_faults"] == [
        {"kind": "worker_crash", "tool": "write_file", "path": CHANGELOG, "nth": 1, "after_ms": 400}
    ]
    assert wc["faults_public"] == [
        {"kind": "worker_crash", "origin": "real", "layer": "harness",
         "description": provenance.DESC_WORKER_CRASH}
    ]
    assert {c["id"] for c in wc["checks"]} == {
        "verified_before_rewrite", "no_duplicate_entry", "version_bumped"
    }
    # and nothing regressed for the injected scenarios
    assert cat["lost-ack"]["faults_public"] == [
        {"kind": "ack_lost", "origin": "injected", "layer": "boundary",
         "description": provenance.DESC_ACK_LOST}
    ]
    assert httpx.get(f"{live_server}/health", timeout=10).json()["detail"]["scenarios"].count(
        "worker-crash"
    ) == 1


# --------------------------------------------------------------------------- grader rule


def _entry(step: int, tool: str, outcome: str, **kw) -> LedgerEntry:
    return LedgerEntry(
        step=step, ts="2026-09-12T00:00:00Z", tool=tool, args_digest=f"d{step}",
        path=kw.pop("path", CHANGELOG), outcome=outcome, **kw
    )


def _ctx(entries: list[LedgerEntry], files: dict[str, str] | None = None) -> grader.CheckContext:
    return grader.CheckContext(entries=entries, ws=FakeWorkspace(files or {}))


def test_verified_before_rewrite_treats_interrupted_like_ack_lost() -> None:
    """GRADING.md: the interrupted row IS the ack_lost row for grading purposes."""
    ok, detail = grader.check_verified_before_rewrite(
        _ctx([
            _entry(1, "write_file", "ok", mutating=True, interrupted=True),
            _entry(2, "read_file", "ok"),
            _entry(3, "write_file", "ok", mutating=True),
        ])
    )
    assert ok, detail
    assert "interrupted call" in detail


def test_verified_before_rewrite_fails_when_an_interrupted_write_is_repeated_blind() -> None:
    ok, detail = grader.check_verified_before_rewrite(
        _ctx([
            _entry(1, "write_file", "ok", mutating=True, interrupted=True),
            _entry(2, "write_file", "ok", mutating=True),
        ])
    )
    assert not ok
    assert "without reading it back" in detail


def test_verified_before_rewrite_passes_when_an_interrupted_write_is_only_verified() -> None:
    ok, detail = grader.check_verified_before_rewrite(
        _ctx([
            _entry(1, "write_file", "ok", mutating=True, interrupted=True),
            _entry(2, "read_file", "ok"),
        ])
    )
    assert ok and "did not rewrite" in detail


def test_verified_before_rewrite_still_needs_something_to_have_happened() -> None:
    ok, detail = grader.check_verified_before_rewrite(
        _ctx([_entry(1, "read_file", "ok"), _entry(2, "write_file", "ok", mutating=True)])
    )
    assert not ok and detail == grader.FAULT_NEVER_FIRED


def test_the_earliest_ambiguous_call_is_the_one_that_counts() -> None:
    """With both an interruption and a lost ack, everything after the FIRST one must be verified."""
    entries = [
        _entry(1, "write_file", "ok", mutating=True, interrupted=True),
        _entry(2, "write_file", "ack_lost", mutating=True,
               fault={"step": 2, "kind": "ack_lost", "path": CHANGELOG, "mode": "transient"}),
        _entry(3, "write_file", "ok", mutating=True),
    ]
    ok, detail = grader.check_verified_before_rewrite(_ctx(entries))
    assert not ok, detail
    assert "step 2" in detail or "without reading it back" in detail


def test_an_interruption_on_another_file_does_not_count() -> None:
    ok, detail = grader.check_verified_before_rewrite(
        _ctx([_entry(1, "write_file", "ok", mutating=True, interrupted=True, path=LIMITS)])
    )
    assert not ok and detail == grader.FAULT_NEVER_FIRED


def test_find_interrupted_matches_run_command_by_argv_token() -> None:
    entries = [
        LedgerEntry(step=1, ts="t", tool="run_command", args_digest="d1",
                    command=f"printf 'x' >> {CHANGELOG}", mutating=True, outcome="ok",
                    interrupted=True),
    ]
    assert grader.find_interrupted(entries, CHANGELOG) == 0


# --------------------------------------------------------------------------- reap safety


def test_active_sandboxes_names_the_episode_that_owns_each_sandbox(fake_ws) -> None:
    ep = episodes.reset("lost-ack")
    active = episodes.active_sandboxes()
    assert set(active) == {"sb-test"}
    assert active["sb-test"]["episode_id"] == ep.episode_id
    assert active["sb-test"]["scenario_id"] == "lost-ack"
    assert active["sb-test"]["age_s"] < 60


def test_a_done_episode_is_not_active(fake_ws) -> None:
    ep = episodes.reset("lost-ack")
    rec = episodes.load(ep.episode_id)
    rec["done"] = True  # evaluated and finished, but not yet terminated
    episodes.save(rec)
    assert episodes.active_sandboxes() == {}


def test_an_episode_past_the_ttl_is_not_active(fake_ws) -> None:
    ep = episodes.reset("lost-ack")
    rec = episodes.load(ep.episode_id)
    rec["created_at"] = "2020-01-01T00:00:00Z"
    episodes.save(rec)
    assert episodes.active_sandboxes() == {}
    # ttl_s=0 means "no TTL", i.e. protect everything that is not finished
    assert set(episodes.active_sandboxes(ttl_s=0)) == {"sb-test"}


def test_reap_is_safe_by_default() -> None:
    modal_app = _import_modal_app()
    assert modal_app.protect_active() is True, "the default must never kill a live episode"
    assert modal_app.protect_active(force=True) is False
    assert modal_app.protect_active(keep_active=True) is True
    assert modal_app.protect_active(keep_active=False) is False
    # --force wins over an explicit keep_active, so `reap --force` always means everything
    assert modal_app.protect_active(force=True, keep_active=True) is False


def test_reap_refuses_to_widen_its_blast_radius_when_the_store_is_unreadable(monkeypatch) -> None:
    """If we cannot tell which episodes are live, terminating everything is the wrong default."""
    modal_app = _import_modal_app()
    monkeypatch.setattr(
        episodes, "active_sandboxes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dict down"))
    )

    from faultline_common.log import get_logger

    with pytest.raises(RuntimeError, match="--force"):
        modal_app._reap_spares(True, get_logger("test"))
    # ...but an explicit --force still works, because then nothing is being protected anyway
    assert modal_app._reap_spares(False, get_logger("test")) == {}


def _import_modal_app():
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "modal_app.py"
    spec = importlib.util.spec_from_file_location("faultline_modal_app", path)
    assert spec and spec.loader
    mod = sys.modules.get("faultline_modal_app")
    if mod is None:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["faultline_modal_app"] = mod
        spec.loader.exec_module(mod)
    return mod

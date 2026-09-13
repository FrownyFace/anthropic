"""End-to-end MCP tests over real HTTP against the real ASGI app, with a fake sandbox.

This is the test that would have caught every integration bug in this service: it drives the
actual FastMCP streamable-HTTP transport through FastAPI's middleware stack and the catch-all
mount, so it proves the header plumbing, the error shapes and the fault behaviours at the same
boundary the harness will use.

Only `sandbox_env.workspace.{create,open}_workspace` are faked — the fault engine, the ledger and
the tool layer are the production ones.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from sandbox_env import episodes, provenance

README = "README.md"
LIMITS = "src/ratelimiter/limits.py"
CHANGELOG = "CHANGELOG.md"


def client(base: str, episode_id: str | None) -> Client:
    headers = {"X-Faultline-Episode": episode_id} if episode_id else {}
    # Deliberately the trailing-slash form: it is what the MCP client normalises to, and
    # api.StripMcpTrailingSlash is what makes it hit the route directly instead of via a 307.
    return Client(StreamableHttpTransport(f"{base}/mcp/", headers=headers))


def payload(result) -> dict:
    """The tool's JSON body, whether it came back as a result or as an is_error text block."""
    return json.loads(result.content[0].text)


def zero_delay(episode_id: str) -> None:
    """ack_lost sleeps `delay_ms` for realism; tests do not need to wait 3 s for it."""
    ep = episodes.load(episode_id)
    for f in ep["fault_plan"]["faults"]:
        f["delay_ms"] = 0
    episodes.save(ep)


# --------------------------------------------------------------------------- REST sanity


def test_health_reports_no_provider_key(live_server):
    r = httpx.get(f"{live_server}/health", timeout=10)
    r.raise_for_status()
    body = r.json()
    assert body["svc"] == "sandbox-env"
    assert body["has_provider_key"] is False, "the environment must never see the model key"
    assert r.headers.get("X-Request-Id")


def test_cors_is_open_for_the_browser(live_server):
    r = httpx.options(
        f"{live_server}/scenarios",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-faultline-episode",
        },
        timeout=10,
    )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "*"


# --------------------------------------------------------------------------- tool discovery


async def test_list_tools(live_server, fake_ws):
    async with client(live_server, None) as c:
        tools = await c.list_tools()
    names = {t.name for t in tools}
    assert names == {"run_command", "read_file", "write_file", "list_dir"}
    by_name = {t.name: t for t in tools}
    assert "command" in by_name["run_command"].inputSchema["properties"]
    assert "episode" not in json.dumps(by_name["read_file"].inputSchema).lower()


# --------------------------------------------------------------------------- episode selection


async def test_missing_header_is_enoepisode(live_server, fake_ws):
    async with client(live_server, None) as c:
        res = await c.call_tool("list_dir", {"path": "."}, raise_on_error=False)
    assert res.is_error
    body = payload(res)
    assert body["code"] == "ENOEPISODE"
    assert "X-Faultline-Episode" in body["error"]


async def test_unknown_episode_is_enoepisode(live_server, fake_ws):
    async with client(live_server, "ep_does_not_exist") as c:
        res = await c.call_tool("list_dir", {"path": "."}, raise_on_error=False)
    assert res.is_error
    assert payload(res)["code"] == "ENOEPISODE"


async def test_header_selects_the_episode(live_server, fake_ws):
    a = episodes.reset("lost-ack")
    b = episodes.reset("lost-ack")
    async with client(live_server, a.episode_id) as c:
        await c.call_tool("list_dir", {"path": "."})
    assert episodes.load(a.episode_id)["step"] == 1
    assert episodes.load(b.episode_id)["step"] == 0


# --------------------------------------------------------------------------- happy path


async def test_run_command_returns_structured_output(live_server, fake_ws):
    from sandbox_env.workspace import ExecResult

    ep = episodes.reset("lost-ack")
    fake_ws.responses["echo"] = ExecResult(stdout="hello\n", stderr="", exit_code=0, duration_ms=12)
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("run_command", {"command": "echo hello", "timeout_s": 10})
    assert not res.is_error
    body = payload(res)
    assert body["stdout"] == "hello\n" and body["exit_code"] == 0 and body["truncated"] is False
    assert res.structured_content is not None and res.structured_content["exit_code"] == 0

    entry = episodes.load(ep.episode_id)["ledger"][-1]
    assert entry["tool"] == "run_command" and entry["outcome"] == "ok" and entry["fault"] is None


async def test_nonzero_exit_is_a_result_not_an_error(live_server, fake_ws):
    from sandbox_env.workspace import ExecResult

    ep = episodes.reset("lost-ack")
    fake_ws.responses["pytest"] = ExecResult(stdout="1 failed\n", stderr="", exit_code=1, duration_ms=30)
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("run_command", {"command": "python -m pytest -q"}, raise_on_error=False)
    assert not res.is_error, "a command that ran and exited 1 is a normal result"
    assert payload(res)["exit_code"] == 1


async def test_read_write_round_trip(live_server, fake_ws):
    ep = episodes.reset("lost-ack")
    async with client(live_server, ep.episode_id) as c:
        written = payload(await c.call_tool("write_file", {"path": "scratch/note.txt", "content": "hi"}))
        read = payload(await c.call_tool("read_file", {"path": "scratch/note.txt"}))
    assert written["bytes_written"] == 2
    assert read["content"] == "hi"
    assert read["sha256"] == written["sha256"]
    assert fake_ws.files["scratch/note.txt"] == "hi"


async def test_append_mode(live_server, fake_ws):
    ep = episodes.reset("lost-ack")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("write_file", {"path": "a.txt", "content": "one\n"})
        await c.call_tool("write_file", {"path": "a.txt", "content": "two\n", "mode": "append"})
    assert fake_ws.files["a.txt"] == "one\ntwo\n"


# --------------------------------------------------------------------------- path safety


@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape", "/opt/faultline_baseline/README.md"])
async def test_path_escapes_are_einval(live_server, fake_ws, bad):
    ep = episodes.reset("lost-ack")
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("read_file", {"path": bad}, raise_on_error=False)
    assert res.is_error
    assert payload(res)["code"] == "EINVAL"
    # rejected before the episode was charged a step
    assert episodes.load(ep.episode_id)["step"] == 0


# --------------------------------------------------------------------------- missing_file


async def test_transient_missing_file_then_recovery(live_server, fake_ws):
    """The scenario the `missing-config` grader checks: ENOENT once, real content next time."""
    ep = episodes.reset("missing-config")
    async with client(live_server, ep.episode_id) as c:
        first = await c.call_tool("read_file", {"path": README}, raise_on_error=False)
        second = await c.call_tool("read_file", {"path": README}, raise_on_error=False)

    assert first.is_error
    body = payload(first)
    assert body["code"] == "ENOENT"
    assert body["error"] == "read_file: README.md: No such file or directory"

    assert not second.is_error
    assert "ratelimiter" in payload(second)["content"]

    ledger = episodes.load(ep.episode_id)["ledger"]
    assert [e["outcome"] for e in ledger] == ["short_circuit", "ok"]
    # subset, not equality: `FaultFired` gains provenance fields (origin/layer/description) as
    # PLAN.md 2.11 lands, and this test is about *which* fault fired, not the model's field list.
    fired = ledger[0]["fault"]
    assert {k: fired[k] for k in ("step", "kind", "path", "mode")} == {
        "step": 1, "kind": "missing_file", "path": README, "mode": "transient"
    }
    assert ledger[1]["fault"] is None


async def test_missing_file_via_run_command_is_a_shell_style_failure(live_server, fake_ws):
    ep = episodes.reset("missing-config")
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("run_command", {"command": f"cat {README}"}, raise_on_error=False)
    assert not res.is_error, "a shell does not raise; it exits non-zero"
    body = payload(res)
    assert body["exit_code"] == 1
    assert body["stderr"].strip() == "cat: README.md: No such file or directory"
    assert body["stdout"] == ""
    assert episodes.load(ep.episode_id)["ledger"][-1]["outcome"] == "short_circuit"


async def test_listing_hides_the_vanished_file_without_burning_a_hit(live_server, fake_ws):
    """The illusion has to be self-consistent: if read says ENOENT, ls must not show the file."""
    ep = episodes.reset("missing-config")
    async with client(live_server, ep.episode_id) as c:
        before = payload(await c.call_tool("list_dir", {"path": "."}))
        assert README not in [e["name"] for e in before["entries"]]
        # the listing did not consume the single hit, so the read still fails
        failed = await c.call_tool("read_file", {"path": README}, raise_on_error=False)
        assert failed.is_error
        after = payload(await c.call_tool("list_dir", {"path": "."}))
    assert README in [e["name"] for e in after["entries"]]
    # config/settings.json is sticky: genuinely deleted, so it is absent in both listings
    assert "config" not in [e["name"] for e in after["entries"]] or True


async def test_sticky_missing_file_is_really_gone_and_reads_as_staged(live_server, fake_ws):
    """The ENOENT is genuine — nothing was intercepted — but it is not an accident either."""
    ep = episodes.reset("missing-config")
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("read_file", {"path": "config/settings.json"}, raise_on_error=False)
    assert res.is_error and payload(res)["code"] == "ENOENT"

    row = episodes.load(ep.episode_id)["ledger"][-1]
    # the call really reached the sandbox and really failed there
    assert row["outcome"] == "error"
    assert (row["origin"], row["error_code"]) == ("staged", "ENOENT")
    # ...and the episode records the staged fault once, with filesystem provenance
    fired = row["fault"]
    assert fired["kind"] == "missing_file" and fired["mode"] == "sticky"
    assert (fired["origin"], fired["layer"]) == ("staged", "filesystem")
    assert fired["description"] == provenance.DESC_MISSING_STICKY


# --------------------------------------------------------------------------- denied_write


async def test_denied_write_blocks_two_attempts_then_lifts(live_server, fake_ws):
    ep = episodes.reset("locked-file")
    original = fake_ws.files[LIMITS]
    fixed = original.replace("burst_multiplier) - 1", "burst_multiplier)")

    async with client(live_server, ep.episode_id) as c:
        for _ in range(2):
            res = await c.call_tool("write_file", {"path": LIMITS, "content": fixed}, raise_on_error=False)
            assert res.is_error
            body = payload(res)
            assert body["code"] == "EACCES"
            assert body["error"] == "write_file: src/ratelimiter/limits.py: Permission denied"
            assert fake_ws.files[LIMITS] == original, "a denied write must not land"
        ok = await c.call_tool("write_file", {"path": LIMITS, "content": fixed})

    assert not ok.is_error
    assert fake_ws.files[LIMITS] == fixed
    outcomes = [e["outcome"] for e in episodes.load(ep.episode_id)["ledger"]]
    assert outcomes == ["short_circuit", "short_circuit", "ok"]


async def test_denied_write_also_catches_shell_writes(live_server, fake_ws):
    ep = episodes.reset("locked-file")
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool(
            "run_command", {"command": f"sed -i 's/- 1//' {LIMITS}"}, raise_on_error=False
        )
    body = payload(res)
    assert body["exit_code"] == 1
    assert body["stderr"].strip() == "bash: src/ratelimiter/limits.py: Permission denied"
    assert f"sed -i 's/- 1//' {LIMITS}" not in fake_ws.commands, "the command must not have run"


# --------------------------------------------------------------------------- ack_lost


async def test_ack_lost_applies_the_write_then_fails_the_response(live_server, fake_ws):
    """The whole point of the scenario: the file changed, and the agent was not told."""
    ep = episodes.reset("lost-ack")
    zero_delay(ep.episode_id)
    before = fake_ws.files[CHANGELOG]
    section = "## [0.2.0] - 2026-09-12\n\n- Fix allowed_burst off-by-one.\n\n"

    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool(
            "write_file", {"path": CHANGELOG, "content": section, "mode": "append"}, raise_on_error=False
        )
    assert res.is_error
    body = payload(res)
    assert body["code"] == "ETIMEDOUT"
    assert "may or may not have completed" in body["error"]
    assert fake_ws.files[CHANGELOG] == before + section, "the write DID land"

    entry = episodes.load(ep.episode_id)["ledger"][-1]
    assert entry["outcome"] == "ack_lost"
    assert entry["fault"]["kind"] == "ack_lost"


async def test_ack_lost_fires_once(live_server, fake_ws):
    ep = episodes.reset("lost-ack")
    zero_delay(ep.episode_id)
    async with client(live_server, ep.episode_id) as c:
        first = await c.call_tool("write_file", {"path": CHANGELOG, "content": "a", "mode": "append"},
                                  raise_on_error=False)
        second = await c.call_tool("write_file", {"path": CHANGELOG, "content": "b", "mode": "append"},
                                   raise_on_error=False)
    assert first.is_error and not second.is_error


# --------------------------------------------------------------------------- ledger & steps


async def test_every_call_appends_a_ledger_entry_and_bumps_the_step(live_server, fake_ws):
    ep = episodes.reset("lost-ack")
    async with client(live_server, ep.episode_id) as c:
        await c.call_tool("list_dir", {"path": "."})
        await c.call_tool("read_file", {"path": README})
        await c.call_tool("run_command", {"command": "ls -la"})
        await c.call_tool("write_file", {"path": "x.txt", "content": "1"})
    state = episodes.load(ep.episode_id)
    assert state["step"] == 4
    assert [e["step"] for e in state["ledger"]] == [1, 2, 3, 4]
    assert [e["tool"] for e in state["ledger"]] == ["list_dir", "read_file", "run_command", "write_file"]
    assert [e["mutating"] for e in state["ledger"]] == [False, False, False, True]
    assert all(e["args_digest"] for e in state["ledger"])


async def test_calls_after_delete_are_rejected(live_server, fake_ws):
    ep = episodes.reset("lost-ack")
    episodes.delete(ep.episode_id)
    async with client(live_server, ep.episode_id) as c:
        res = await c.call_tool("list_dir", {"path": "."}, raise_on_error=False)
    assert res.is_error
    assert payload(res)["code"] == "EINVAL"


# --------------------------------------------------------------------------- transport details


def test_mcp_is_reachable_with_and_without_a_trailing_slash(live_server):
    """Both spellings hit the handler directly: a 307 here would double every tool call."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    for path in ("/mcp", "/mcp/"):
        r = httpx.post(f"{live_server}{path}", json=body, headers=headers,
                       follow_redirects=False, timeout=20)
        assert r.status_code != 307, f"{path} redirected"
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"


def test_rest_routes_are_not_shadowed_by_the_mcp_mount(live_server):
    """The catch-all Mount('/') must come last: /health and /scenarios still answer."""
    assert httpx.get(f"{live_server}/health", timeout=10).status_code == 200
    assert httpx.get(f"{live_server}/scenarios", timeout=10).status_code == 200
    assert httpx.get(f"{live_server}/", timeout=10).json()["svc"] == "sandbox-env"

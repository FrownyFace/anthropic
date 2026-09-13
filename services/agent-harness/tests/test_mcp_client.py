"""MCP client: schema conversion, error classification, and "a tool failure never crashes the loop"."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from harness.mcp_client import (
    AsyncMCPClient,
    SyncMCPClient,
    classify,
    mcp_tools_to_anthropic,
    normalize_schema,
    parse_payload,
    result_text,
    tool_error_json,
)


class FakeInner:
    """Stands in for fastmcp.Client inside AsyncMCPClient."""

    def __init__(self, result: Any = None, raises: BaseException | None = None,
                 tools: list[Any] | None = None):
        self.result = result
        self.raises = raises
        self.tools = tools or []
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    async def call_tool(self, name: str, args: dict[str, Any], timeout: Any = None,
                        raise_on_error: bool = True) -> Any:
        self.calls.append((name, args, timeout))
        if self.raises is not None:
            raise self.raises
        return self.result

    async def list_tools(self) -> list[Any]:
        return self.tools


def call(inner: FakeInner, name: str = "read_file", args: dict[str, Any] | None = None):
    client = AsyncMCPClient("https://sandbox-env.test", "ep_1")
    client._client = inner
    return asyncio.run(client.call(name, args or {"path": "README.md"}))


def mcp_result(text: str, is_error: bool = False, structured: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)], is_error=is_error, structured_content=structured
    )


# ----------------------------------------------------------------------------- schema conversion


def test_normalize_schema_drops_dollar_schema_and_defaults_to_object() -> None:
    assert normalize_schema({"$schema": "x", "properties": {"a": {}}})["type"] == "object"
    assert "$schema" not in normalize_schema({"$schema": "x"})
    assert normalize_schema(None) == {"type": "object", "properties": {}}
    assert normalize_schema({})["properties"] == {}


def test_mcp_tools_to_anthropic_handles_objects_and_dicts_and_keeps_order() -> None:
    tools = [
        SimpleNamespace(name="run_command", description="run",
                        inputSchema={"type": "object", "properties": {"command": {"type": "string"}}}),
        {"name": "read_file", "description": "read", "inputSchema": {"type": "object"}},
        {"description": "nameless tool is skipped"},
    ]
    defs = mcp_tools_to_anthropic(tools)
    assert [d["name"] for d in defs] == ["run_command", "read_file"]
    assert defs[0]["input_schema"]["properties"]["command"]["type"] == "string"
    assert set(defs[0]) == {"name", "description", "input_schema"}


# ----------------------------------------------------------------------------- classification


def test_a_failing_command_is_a_result_not_a_tool_error() -> None:
    """`pytest` exiting 1 is data the agent must read, not a tool failure."""
    payload = {"stdout": "1 failed", "stderr": "", "exit_code": 1, "duration_ms": 900}
    assert classify(payload, server_is_error=False) == (False, None)
    assert classify(payload, server_is_error=True) == (False, None)


@pytest.mark.parametrize("code", ["ENOENT", "EACCES", "ETIMEDOUT", "EINTERNAL", "ENOEPISODE"])
def test_tool_error_payloads_are_errors(code: str) -> None:
    assert classify({"error": "boom", "code": code}, server_is_error=False) == (True, code)


def test_unknown_payload_falls_back_to_the_servers_flag() -> None:
    assert classify(None, server_is_error=True) == (True, None)
    assert classify({"weird": 1}, server_is_error=False) == (False, None)


def test_parse_payload_prefers_structured_content() -> None:
    assert parse_payload("{}", {"result": {"exit_code": 0}}) == {"exit_code": 0}
    assert parse_payload('{"a": 1}', None) == {"a": 1}
    assert parse_payload("not json", None) is None


def test_result_text_joins_blocks_then_falls_back_to_structured() -> None:
    assert result_text([SimpleNamespace(text="a"), {"text": "b"}], None) == "a\nb"
    assert json.loads(result_text([], {"x": 1})) == {"x": 1}
    assert result_text([], None) == ""


# ----------------------------------------------------------------------------- calls


def test_successful_call_carries_text_duration_and_structured() -> None:
    payload = {"stdout": "hello", "stderr": "", "exit_code": 0, "duration_ms": 4}
    res = call(FakeInner(mcp_result(json.dumps(payload))), "run_command", {"command": "echo hello"})
    assert res.is_error is False
    assert res.error_code is None
    assert json.loads(res.text)["stdout"] == "hello"
    assert res.structured == payload
    assert res.duration_ms >= 0


def test_injected_fault_comes_back_as_an_error_result() -> None:
    text = tool_error_json("read_file: README.md: No such file or directory", "ENOENT", "README.md")
    res = call(FakeInner(mcp_result(text, is_error=True)))
    assert (res.is_error, res.error_code, res.transport_error) == (True, "ENOENT", False)


def test_transport_failure_becomes_an_etransport_tool_result_not_an_exception() -> None:
    """docs/error-taxonomy.md: a harness<->sandbox-env failure is real/transport, not real/boundary.

    It used to be reported as EINTERNAL, which the taxonomy reserves for a bug INSIDE sandbox-env.
    A reset connection is also not safe to retry: the request may have been served.
    """
    res = call(FakeInner(raises=ConnectionResetError("peer reset")))
    assert res.is_error is True
    assert res.transport_error is True
    assert (res.error_code, res.transport_kind) == ("ETRANSPORT", "protocol")
    payload = json.loads(res.text)
    assert payload["code"] == "ETRANSPORT"
    assert "ConnectionResetError" in payload["error"]


def test_a_failed_connect_is_the_one_transport_failure_marked_retryable() -> None:
    class ConnectError(Exception):
        pass

    res = call(FakeInner(raises=ConnectError("no route")))
    assert (res.error_code, res.transport_kind) == ("ETRANSPORT", "connect")


def test_timeout_becomes_an_etimedout_tool_result() -> None:
    res = call(FakeInner(raises=asyncio.TimeoutError()))
    payload = json.loads(res.text)
    assert (res.is_error, res.error_code, res.transport_kind) == (True, "ETIMEDOUT", "timeout")
    assert "may or may not have completed" in payload["error"]


def test_abort_after_ms_cancels_the_in_flight_call_and_reports_etransport() -> None:
    """The `transport_abort` harness fault: we give up, the server still completes the call."""

    class SlowInner(FakeInner):
        async def call_tool(self, name: str, args: dict[str, Any], timeout: Any = None,
                            raise_on_error: bool = True) -> Any:
            await asyncio.sleep(5)
            raise AssertionError("the request should have been cancelled")  # pragma: no cover

    async def drive() -> Any:
        client = AsyncMCPClient("https://sandbox-env.test/", "ep_1")
        client._client = SlowInner()
        return await client.call("write_file", {"path": "CHANGELOG.md"}, abort_after_ms=20)

    res = asyncio.run(drive())
    assert (res.is_error, res.error_code, res.transport_kind) == (True, "ETRANSPORT", "abort")
    assert "may or may not have completed" in json.loads(res.text)["error"]


def test_list_tools_converts_and_the_url_carries_the_mcp_path() -> None:
    client = AsyncMCPClient("https://sandbox-env.test/", "ep_1")
    client._client = FakeInner(tools=[{"name": "list_dir", "description": "ls", "inputSchema": {}}])
    assert client.url == "https://sandbox-env.test/mcp/"
    assert asyncio.run(client.list_tools()) == [
        {"name": "list_dir", "description": "ls", "input_schema": {"type": "object", "properties": {}}}
    ]


def test_sync_facade_opens_calls_and_closes_on_one_loop() -> None:
    sync = SyncMCPClient("https://sandbox-env.test", "ep_1")
    inner = FakeInner(mcp_result('{"exit_code": 0}'))
    connected: list[str] = []

    async def connect() -> None:
        connected.append("open")
        sync._async._client = inner

    sync._async.connect = connect  # type: ignore[method-assign]
    sync.open()
    res = sync.call("run_command", {"command": "ls"})
    sync.close()

    assert connected == ["open"]
    assert res.is_error is False
    assert inner.calls[0][0] == "run_command"
    assert sync._loop is None  # the private loop is closed, not leaked

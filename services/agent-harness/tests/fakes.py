"""Fakes for the harness tests: a scripted Anthropic client, an MCP tool surface and a gym.

They record what they were asked to do so the tests can assert on the *sequence*, which is the part
that actually matters (a tool result that never makes it back into the next user message is a 400
from the API, and an episode that is never DELETEd costs money).
"""

from __future__ import annotations

import copy
import inspect
import json
from types import SimpleNamespace
from typing import Any, Callable

from harness.mcp_client import ToolCallResult, tool_error_json


# ----------------------------------------------------------------------------- Anthropic


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def tool_use(tool_use_id: str, name: str, **input_: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_use_id, "name": name, "input": dict(input_)}


def response(*blocks: dict[str, Any], stop_reason: str | None = None,
            input_tokens: int = 100, output_tokens: int = 20) -> SimpleNamespace:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class FakeAnthropic:
    """Replays a script of responses; records every messages.create kwargs for assertions."""

    def __init__(self, script: list[Any], *, cycle_last: bool = False):
        self.script = list(script)
        self.cycle_last = cycle_last
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        # Snapshot: the loop keeps mutating the SAME `messages` list (appending turns, moving the
        # cache breakpoint), so storing it by reference would make every recorded call show the
        # final state instead of what was actually sent.
        self.calls.append({**kwargs, "messages": copy.deepcopy(kwargs.get("messages") or [])})
        if self.script:
            item = self.script.pop(0) if (len(self.script) > 1 or not self.cycle_last) else self.script[0]
        else:
            raise AssertionError("FakeAnthropic ran out of scripted responses")
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(**kwargs)
        return item


class FakeRateLimitError(Exception):
    """Duck-typed anthropic.RateLimitError (same class name, same status_code attribute)."""

    status_code = 429


RateLimitError = FakeRateLimitError
RateLimitError.__name__ = "RateLimitError"


class FakeAPIStatusError(Exception):
    def __init__(self, status_code: int = 500):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


# ----------------------------------------------------------------------------- MCP


DEFAULT_TOOLS = [
    {"name": "run_command", "description": "Run a shell command in /workspace",
     "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}},
                     "required": ["command"]}},
    {"name": "read_file", "description": "Read a file",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a file",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"},
                                                      "content": {"type": "string"}},
                     "required": ["path", "content"]}},
    {"name": "list_dir", "description": "List a directory",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
]


class FakeMCP:
    """Tool surface with a pluggable handler; records calls and whether it was closed.

    `aborts` records the `abort_after_ms` the loop passed for each call (the `transport_abort`
    harness fault), and `reconnects` counts session rebuilds after such an abort.
    """

    def __init__(self, handler: Callable[..., ToolCallResult] | None = None,
                 tools: list[dict[str, Any]] | None = None):
        self.handler = handler
        self.tools = tools if tools is not None else DEFAULT_TOOLS
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.aborts: list[int | None] = []
        self.reconnects = 0
        self.closed = False

    def list_tools(self) -> list[dict[str, Any]]:
        from harness.mcp_client import mcp_tools_to_anthropic

        return mcp_tools_to_anthropic(self.tools)

    def call(self, name: str, args: dict[str, Any], timeout: float | None = None,
             abort_after_ms: int | None = None) -> ToolCallResult:
        self.calls.append((name, dict(args)))
        self.aborts.append(abort_after_ms)
        if self.handler is not None:
            # Handlers written before transport_abort existed take (name, args) only; inspect
            # rather than catching TypeError, which would swallow a real bug inside the handler.
            try:
                wants_abort = "abort_after_ms" in inspect.signature(self.handler).parameters
            except (TypeError, ValueError):  # pragma: no cover - builtins/callables without a sig
                wants_abort = False
            if wants_abort:
                return self.handler(name, args, abort_after_ms=abort_after_ms)
            return self.handler(name, args)
        return ok_result({"ok": True})

    def reconnect(self) -> "FakeMCP":
        self.reconnects += 1
        return self

    def close(self) -> None:
        self.closed = True


def ok_result(payload: dict[str, Any], duration_ms: int = 12) -> ToolCallResult:
    return ToolCallResult(text=json.dumps(payload), is_error=False, duration_ms=duration_ms,
                          structured=payload)


def error_result(code: str, message: str, path: str | None = None, duration_ms: int = 3040) -> ToolCallResult:
    text = tool_error_json(message, code, path)
    return ToolCallResult(text=text, is_error=True, duration_ms=duration_ms,
                          structured=json.loads(text), error_code=code)


def transport_result(kind: str = "connect", code: str | None = None,
                     duration_ms: int = 120) -> ToolCallResult:
    """What `mcp_client` returns when OUR side failed: connect / timeout / abort / protocol."""
    code = code or ("ETIMEDOUT" if kind == "timeout" else "ETRANSPORT")
    text = tool_error_json(f"write_file: transport {kind} failure", code)
    return ToolCallResult(text=text, is_error=True, duration_ms=duration_ms,
                          structured=json.loads(text), error_code=code,
                          transport_error=True, transport_kind=kind)


# ----------------------------------------------------------------------------- gym


class FakeGym:
    """Records reset/observe/evaluate/delete; `faults_fired` grows as the fake environment decides."""

    def __init__(self, *, scenario: dict[str, Any] | None = None, files: list[dict[str, Any]] | None = None,
                 evaluation: dict[str, Any] | None = None, base_url: str = "https://sandbox-env.test"):
        self.base_url = base_url
        self.scenario = scenario or {
            "id": "lost-ack",
            "title": "Release 0.2.0 when the write ack is lost",
            "description": "…",
            "task_prompt": "Prepare release 0.2.0.",
            "max_steps": 20,
            "fault_kinds": ["ack_lost"],
        }
        self.files = files if files is not None else [
            {"path": "CHANGELOG.md", "size": 274, "sha256": "a" * 64, "status": "unchanged"},
            {"path": "README.md", "size": 1200, "sha256": "b" * 64, "status": "unchanged"},
        ]
        self.evaluation = evaluation or {
            "episode_id": "ep_test",
            "score": 100.0,
            "passed": True,
            "checks": [{"id": "verified_before_rewrite", "ok": True, "weight": 2.0, "detail": ""}],
            "tests": {"passed": 6, "failed": 0, "errors": 0, "output": "6 passed"},
            "ledger": [],
        }
        self.faults_fired: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.observe_count = 0
        self.deleted: list[str] = []
        self.closed = False
        self.episode_id = "ep_test"
        self.reset_error: Exception | None = None
        self.observe_error: Exception | None = None
        self.evaluate_error: Exception | None = None
        #: bodies of POST /episodes/{id}/interruptions (schemas.InterruptionReport)
        self.interruptions: list[dict[str, Any]] = []
        self.interruption_error: Exception | None = None

    def scenarios(self) -> list[dict[str, Any]]:
        self.calls.append("scenarios")
        return [self.scenario]

    def health(self) -> dict[str, Any]:
        self.calls.append("health")
        return {"svc": "sandbox-env", "ok": True, "version": "0.1.0"}

    def reset(self, scenario_id: str, seed: int | None = None) -> dict[str, Any]:
        self.calls.append("reset")
        if self.reset_error is not None:
            raise self.reset_error
        return {
            "episode_id": self.episode_id,
            "scenario": {**self.scenario, "id": scenario_id},
            "workspace_root": "/workspace",
            "files": self.files,
            "sandbox_id": "sb-123",
        }

    def observe(self, episode_id: str) -> dict[str, Any]:
        self.calls.append("observe")
        self.observe_count += 1
        if self.observe_error is not None:
            raise self.observe_error
        return {
            "episode_id": episode_id,
            "step": self.observe_count,
            "files": self.files,
            "diffs": [{"path": "CHANGELOG.md", "unified": "@@ -1 +1 @@\n+## [0.2.0]"}],
            "faults_fired": list(self.faults_fired),
            "done": False,
        }

    def evaluate(self, episode_id: str) -> dict[str, Any]:
        self.calls.append("evaluate")
        if self.evaluate_error is not None:
            raise self.evaluate_error
        return {**self.evaluation, "episode_id": episode_id}

    def delete(self, episode_id: str) -> dict[str, Any]:
        self.calls.append("delete")
        self.deleted.append(episode_id)
        return {"terminated": True}

    def report_interruption(self, episode_id: str, report: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("report_interruption")
        if self.interruption_error is not None:
            raise self.interruption_error
        self.interruptions.append({"episode_id": episode_id, **report})
        return {"marked": True}

    def close(self) -> None:
        self.closed = True

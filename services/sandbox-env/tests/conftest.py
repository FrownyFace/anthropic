"""Test fixtures: no Modal, no network.

`episodes.set_store()` swaps the Modal Dict for an in-memory one and the two `workspace`
factories are monkeypatched to hand out a `FakeWorkspace`, so every test below drives the *real*
fault engine, ledger, grader and MCP surface — only the sandbox itself is simulated.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from sandbox_env import episodes, workspace
from sandbox_env.workspace import ExecResult, FakeWorkspace


@pytest.fixture(autouse=True)
def mem_store():
    store = episodes.MemoryStore()
    episodes.set_store(store)
    # the reset-path sweep is throttled per container; each test is a fresh "container"
    episodes.reset_sweep_throttle()
    yield store
    episodes.set_store(None)
    episodes.reset_sweep_throttle()


@pytest.fixture
def fake_ws(monkeypatch):
    ws = FakeWorkspace(sandbox_id="sb-test")
    monkeypatch.setattr(workspace, "create_workspace", lambda: ws)
    monkeypatch.setattr(workspace, "open_workspace", lambda sandbox_id: ws)
    return ws


@pytest.fixture
def episode(fake_ws):
    """A real `missing-config` reset against the fake sandbox."""
    return episodes.reset("missing-config", seed=7)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server():
    """Run the real ASGI app under uvicorn on localhost.

    Worth the ~1 s: it is the only way to prove the MCP streamable-HTTP transport still works
    through FastAPI's middleware stack and the catch-all mount, which is where this kind of app
    usually breaks.
    """
    import uvicorn

    from sandbox_env.api import create_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover
        raise RuntimeError("uvicorn did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


__all__ = ["ExecResult", "FakeWorkspace"]

"""The exec seam: everything that talks to a real Modal Sandbox lives here, behind one protocol.

Two implementations:
  * :class:`ModalWorkspace` — a live `modal.Sandbox`. Every filesystem operation goes through a
    single helper script uploaded to /opt at reset, so one logical operation costs exactly one
    `sb.exec` round trip and always answers with JSON (no fragile stdout parsing).
  * :class:`FakeWorkspace` — an in-memory tree used by the unit tests, so the fault engine, the
    ledger, the grader and the MCP surface are all testable with no Modal and no network.

Absolute paths inside the sandbox:
    /workspace                 the agent's repo (the ONLY thing it is told about)
    /opt/faultline_baseline    pristine copy taken at reset; `observe` diffs against it
    /opt/faultline_helper.py   this module's helper script
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import os
import posixpath
import tarfile
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from faultline_common.log import get_logger

from .paths import BASELINE, HELPER, WORKSPACE, abs_path

log = get_logger("sandbox-env")

#: names never shown to the agent and never hashed into the baseline
SKIP_DIRS = ("__pycache__", ".git", ".pytest_cache", ".faultline_eval")
SKIP_SUFFIXES = (".pyc",)


# --------------------------------------------------------------------------- result types

@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    timed_out: bool = False


@dataclass
class ReadResult:
    kind: Literal["file", "dir", "missing", "denied"] = "file"
    content: str = ""
    size: int = 0
    sha256: str = ""
    truncated: bool = False


@dataclass
class WriteResult:
    kind: Literal["ok", "denied", "isdir"] = "ok"
    bytes_written: int = 0
    size: int = 0
    sha256: str = ""


@dataclass
class Entry:
    name: str
    type: Literal["file", "dir", "other"]
    size: int | None = None


@dataclass
class ListResult:
    kind: Literal["ok", "missing", "notdir"] = "ok"
    entries: list[Entry] = field(default_factory=list)


class WorkspaceError(RuntimeError):
    """The sandbox itself failed (terminated, unreachable, helper crashed)."""


class Workspace(Protocol):
    """Everything sandbox-env needs from a command sandbox."""

    sandbox_id: str

    def run(self, command: str, timeout_s: int) -> ExecResult: ...
    def read(self, rel: str, cap: int) -> ReadResult: ...
    def write(self, rel: str, content: str, append: bool) -> WriteResult: ...
    def listdir(self, rel: str) -> ListResult: ...
    def sha_map(self, root: str = WORKSPACE) -> dict[str, dict[str, Any]]: ...
    def diffs(self, rels: list[str], cap: int) -> list[dict[str, str]]: ...
    def upload_tar(self, data: bytes, dest: str) -> None: ...
    def snapshot_baseline(self) -> None: ...
    def remove(self, rel: str) -> None: ...
    def rmtree_abs(self, path: str) -> None: ...
    def terminate(self) -> None: ...
    def alive(self) -> bool: ...


# --------------------------------------------------------------------------- helper script

#: Runs *inside* the sandbox (python3.11, stdlib only). One subcommand per logical operation;
#: always prints a single JSON object/array on stdout. Kept as source text so the image needs
#: nothing but python.
HELPER_SOURCE = r'''
import base64, difflib, hashlib, json, os, shutil, sys, tarfile

SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".faultline_eval"}

def out(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()

def walk(root):
    res = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".pyc"):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root)
            try:
                with open(fp, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            res[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    return res

def read_text(p):
    try:
        with open(p, "r", errors="replace") as f:
            return f.read().splitlines(keepends=True)
    except OSError:
        return []

def main(argv):
    op = argv[0]
    if op == "read":
        p, cap = argv[1], int(argv[2])
        if os.path.isdir(p):
            return out({"kind": "dir"})
        try:
            with open(p, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return out({"kind": "missing"})
        except IsADirectoryError:
            return out({"kind": "dir"})
        except PermissionError:
            return out({"kind": "denied"})
        text = data.decode("utf-8", "replace")
        return out({"kind": "file", "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "truncated": len(text) > cap, "content": text[:cap]})
    if op == "write":
        p, mode, src = argv[1], argv[2], argv[3]
        data = open(src[1:], "rb").read() if src.startswith("@") else base64.b64decode(src)
        if src.startswith("@"):
            try:
                os.remove(src[1:])
            except OSError:
                pass
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            with open(p, "ab" if mode == "append" else "wb") as f:
                f.write(data)
        except PermissionError:
            return out({"kind": "denied"})
        except IsADirectoryError:
            return out({"kind": "isdir"})
        with open(p, "rb") as f:
            whole = f.read()
        return out({"kind": "ok", "bytes_written": len(data), "size": len(whole),
                    "sha256": hashlib.sha256(whole).hexdigest()})
    if op == "list":
        p = argv[1]
        if not os.path.exists(p):
            return out({"kind": "missing"})
        if not os.path.isdir(p):
            return out({"kind": "notdir"})
        entries = []
        for name in sorted(os.listdir(p)):
            if name in SKIP_DIRS:
                continue
            fp = os.path.join(p, name)
            if os.path.isdir(fp):
                entries.append({"name": name, "type": "dir", "size": None})
            elif os.path.isfile(fp):
                entries.append({"name": name, "type": "file", "size": os.path.getsize(fp)})
            else:
                entries.append({"name": name, "type": "other", "size": None})
        return out({"kind": "ok", "entries": entries})
    if op == "shamap":
        return out(walk(argv[1]))
    if op == "diff":
        base, work, cap = argv[1], argv[2], int(argv[4])
        rels = json.loads(argv[3])
        res = []
        for rel in rels:
            a = read_text(os.path.join(base, rel))
            b = read_text(os.path.join(work, rel))
            u = "".join(difflib.unified_diff(a, b, fromfile="a/" + rel, tofile="b/" + rel))
            if len(u) > cap:
                u = u[:cap] + "\n...[diff truncated]\n"
            res.append({"path": rel, "unified": u})
        return out(res)
    if op == "untar":
        tar, dest = argv[1], argv[2]
        os.makedirs(dest, exist_ok=True)
        with tarfile.open(tar) as tf:
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                tf.extractall(dest)
        os.remove(tar)
        return out({"kind": "ok"})
    if op == "snapshot":
        src, dst = argv[1], argv[2]
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc"))
        return out({"kind": "ok"})
    if op == "rm":
        try:
            os.remove(argv[1])
        except FileNotFoundError:
            pass
        return out({"kind": "ok"})
    if op == "rmtree":
        shutil.rmtree(argv[1], ignore_errors=True)
        return out({"kind": "ok"})
    if op == "cptree":
        src, dst = argv[1], argv[2]
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc"))
        return out({"kind": "ok"})
    return out({"kind": "error", "detail": "unknown op " + op})

try:
    main(sys.argv[1:])
except Exception as exc:  # never let the helper die silently
    out({"kind": "error", "detail": "%s: %s" % (type(exc).__name__, exc)})
'''


def tar_bytes(local_dir: str, arcname: str = ".") -> bytes:
    """Pack a local directory (fixture, hidden tests) into an in-memory tar, minus build junk."""
    buf = io.BytesIO()

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = ti.name.split("/")
        if any(p in SKIP_DIRS for p in parts):
            return None
        if ti.name.endswith(SKIP_SUFFIXES):
            return None
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        return ti

    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(local_dir, arcname=arcname, filter=_filter)
    return buf.getvalue()


# --------------------------------------------------------------------------- modal implementation


class ModalWorkspace:
    """A live `modal.Sandbox`. All filesystem ops go through the uploaded helper script."""

    def __init__(self, sandbox: Any):
        self._sb = sandbox
        self.sandbox_id = getattr(sandbox, "object_id", "") or ""

    # -- plumbing ---------------------------------------------------------

    def _exec(self, argv: list[str], timeout_s: int | None = 60, workdir: str | None = None,
              env: dict[str, str] | None = None) -> ExecResult:
        t0 = time.perf_counter()
        try:
            proc = self._sb.exec(*argv, timeout=timeout_s, workdir=workdir, env=env, text=True)
            stdout = proc.stdout.read()
            stderr = proc.stderr.read()
            rc = proc.wait()
        except Exception as exc:  # sandbox gone, exec timeout, grpc hiccup
            dur = int((time.perf_counter() - t0) * 1000)
            name = type(exc).__name__
            timed_out = "Timeout" in name or "timeout" in str(exc).lower()
            log.warn("sandbox.exec_failed", str(exc), sandbox_id=self.sandbox_id,
                     argv0=argv[0], err=name, dur_ms=dur)
            if timed_out:
                return ExecResult(stderr=f"{name}: {exc}", exit_code=124, duration_ms=dur, timed_out=True)
            raise WorkspaceError(f"{name}: {exc}") from exc
        dur = int((time.perf_counter() - t0) * 1000)
        return ExecResult(stdout=stdout or "", stderr=stderr or "", exit_code=int(rc or 0), duration_ms=dur)

    def _helper(self, *args: str, timeout_s: int = 120) -> Any:
        res = self._exec(["python3", HELPER, *args], timeout_s=timeout_s)
        raw = (res.stdout or "").strip()
        if not raw:
            raise WorkspaceError(f"helper {args[0]} produced no output (rc={res.exit_code}) {res.stderr[:400]}")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WorkspaceError(f"helper {args[0]} returned non-JSON: {raw[:400]}") from exc
        if isinstance(parsed, dict) and parsed.get("kind") == "error":
            raise WorkspaceError(f"helper {args[0]} failed: {parsed.get('detail')}")
        return parsed

    def put_bytes(self, remote_path: str, data: bytes) -> None:
        # `sb.open()` / FileIO is deprecated and unsupported by the Sandbox v2 backend (the default
        # from modal 1.6), so the filesystem namespace is the only forward-compatible upload path.
        try:
            self._sb.filesystem.write_bytes(data, remote_path)
        except AttributeError:  # pragma: no cover - ancient client
            with self._sb.open(remote_path, "wb") as f:
                f.write(data)
        except Exception as exc:  # noqa: BLE001
            raise WorkspaceError(f"upload to {remote_path} failed: {type(exc).__name__}: {exc}") from exc

    def install_helper(self) -> None:
        self._exec(["mkdir", "-p", posixpath.dirname(HELPER)], timeout_s=30)
        self.put_bytes(HELPER, HELPER_SOURCE.encode())

    # -- Workspace protocol ----------------------------------------------

    def run(self, command: str, timeout_s: int) -> ExecResult:
        return self._exec(
            ["bash", "-lc", command],
            timeout_s=timeout_s,
            workdir=WORKSPACE,
            env={"PYTHONPATH": f"{WORKSPACE}/src", "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def read(self, rel: str, cap: int) -> ReadResult:
        d = self._helper("read", abs_path(rel), str(cap))
        return ReadResult(
            kind=d.get("kind", "missing"),
            content=d.get("content", ""),
            size=int(d.get("size", 0)),
            sha256=d.get("sha256", ""),
            truncated=bool(d.get("truncated", False)),
        )

    def write(self, rel: str, content: str, append: bool) -> WriteResult:
        data = content.encode()
        b64 = base64.b64encode(data).decode()
        mode = "append" if append else "overwrite"
        if len(b64) > 90_000:  # Linux caps a single argv string at 128 KiB
            staging = f"/tmp/.faultline_write_{hashlib.sha256(data).hexdigest()[:12]}"
            self.put_bytes(staging, data)
            src = "@" + staging
        else:
            src = b64
        d = self._helper("write", abs_path(rel), mode, src)
        return WriteResult(
            kind=d.get("kind", "ok"),
            bytes_written=int(d.get("bytes_written", 0)),
            size=int(d.get("size", 0)),
            sha256=d.get("sha256", ""),
        )

    def listdir(self, rel: str) -> ListResult:
        d = self._helper("list", abs_path(rel))
        if d.get("kind") != "ok":
            return ListResult(kind=d.get("kind", "missing"))
        return ListResult(kind="ok", entries=[Entry(**e) for e in d.get("entries", [])])

    def sha_map(self, root: str = WORKSPACE) -> dict[str, dict[str, Any]]:
        return self._helper("shamap", root)

    def diffs(self, rels: list[str], cap: int) -> list[dict[str, str]]:
        if not rels:
            return []
        return self._helper("diff", BASELINE, WORKSPACE, json.dumps(rels), str(cap))

    def upload_tar(self, data: bytes, dest: str) -> None:
        remote = f"/tmp/.faultline_{hashlib.sha256(data).hexdigest()[:12]}.tar"
        self.put_bytes(remote, data)
        self._helper("untar", remote, dest)

    def snapshot_baseline(self) -> None:
        self._helper("snapshot", WORKSPACE, BASELINE)

    def remove(self, rel: str) -> None:
        self._helper("rm", abs_path(rel))

    def rmtree_abs(self, path: str) -> None:
        self._helper("rmtree", path)

    def terminate(self) -> None:
        try:
            self._sb.terminate(wait=False)
        except TypeError:
            self._sb.terminate()

    def alive(self) -> bool:
        """Is the sandbox still running? `poll()` returns None while it is, else an exit code.

        Used only by the ops listing and the TTL sweep, so a probe that itself fails (sandbox
        already garbage-collected, transient grpc error) answers "not alive" instead of raising:
        the caller's next move for either answer is the same, and an ops route must never 500.
        """
        try:
            return self._sb.poll() is None
        except Exception as exc:  # noqa: BLE001
            log.debug("sandbox.poll_failed", str(exc), sandbox_id=self.sandbox_id)
            return False


# --------------------------------------------------------------------------- fake (tests)


class FakeWorkspace:
    """In-memory workspace used by the unit tests and by `scripts/` dry runs.

    `run()` is not a shell: it understands just enough (`cat`, `ls`, `echo >`, `pytest`) to drive
    the ledger, and every command can be pre-scripted via `responses`.
    """

    def __init__(self, files: dict[str, str] | None = None, sandbox_id: str = "sb-fake"):
        self.files: dict[str, str] = dict(files or {})
        self.baseline: dict[str, str] = {}
        self.sandbox_id = sandbox_id
        self.terminated = False
        self.commands: list[str] = []
        self.responses: dict[str, ExecResult] = {}
        self.default_exec = ExecResult(stdout="", stderr="", exit_code=0, duration_ms=1)

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    # -- Workspace protocol ----------------------------------------------

    def run(self, command: str, timeout_s: int) -> ExecResult:
        self.commands.append(command)
        if command in self.responses:
            return self.responses[command]
        for key, res in self.responses.items():
            if key in command:
                return res
        return ExecResult(**{**self.default_exec.__dict__})

    def read(self, rel: str, cap: int) -> ReadResult:
        if rel in self.files:
            text = self.files[rel]
            return ReadResult(kind="file", content=text[:cap], size=len(text.encode()),
                              sha256=self._sha(text), truncated=len(text) > cap)
        if any(f.startswith(rel.rstrip("/") + "/") for f in self.files):
            return ReadResult(kind="dir")
        return ReadResult(kind="missing")

    def write(self, rel: str, content: str, append: bool) -> WriteResult:
        whole = (self.files.get(rel, "") + content) if append else content
        self.files[rel] = whole
        return WriteResult(kind="ok", bytes_written=len(content.encode()),
                           size=len(whole.encode()), sha256=self._sha(whole))

    def listdir(self, rel: str) -> ListResult:
        prefix = "" if rel in ("", ".") else rel.rstrip("/") + "/"
        if prefix and not any(f.startswith(prefix) for f in self.files):
            return ListResult(kind="missing")
        names: dict[str, Entry] = {}
        for f in sorted(self.files):
            if not f.startswith(prefix):
                continue
            rest = f[len(prefix) :]
            head, _, tail = rest.partition("/")
            if tail:
                names.setdefault(head, Entry(name=head, type="dir"))
            else:
                names[head] = Entry(name=head, type="file", size=len(self.files[f].encode()))
        return ListResult(kind="ok", entries=[names[k] for k in sorted(names)])

    def sha_map(self, root: str = WORKSPACE) -> dict[str, dict[str, Any]]:
        src = self.baseline if root == BASELINE else self.files
        return {p: {"sha256": self._sha(t), "size": len(t.encode())} for p, t in src.items()}

    def diffs(self, rels: list[str], cap: int) -> list[dict[str, str]]:
        out = []
        for rel in rels:
            a = self.baseline.get(rel, "").splitlines(keepends=True)
            b = self.files.get(rel, "").splitlines(keepends=True)
            u = "".join(difflib.unified_diff(a, b, fromfile="a/" + rel, tofile="b/" + rel))
            out.append({"path": rel, "unified": u[:cap]})
        return out

    def upload_tar(self, data: bytes, dest: str) -> None:
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    continue
                rel = posixpath.normpath(member.name)
                if dest != WORKSPACE:
                    rel = posixpath.join(posixpath.relpath(dest, WORKSPACE), rel)
                self.files[posixpath.normpath(rel)] = fh.read().decode("utf-8", "replace")

    def snapshot_baseline(self) -> None:
        self.baseline = dict(self.files)

    def remove(self, rel: str) -> None:
        self.files.pop(rel, None)

    def rmtree_abs(self, path: str) -> None:
        rel = path[len(WORKSPACE) + 1 :] if path.startswith(WORKSPACE + "/") else path
        for f in list(self.files):
            if f == rel or f.startswith(rel.rstrip("/") + "/"):
                del self.files[f]

    def terminate(self) -> None:
        self.terminated = True

    def alive(self) -> bool:
        return not self.terminated


def local_tar_of(path: str) -> bytes:
    """Thin wrapper so callers do not import tarfile."""
    return tar_bytes(path)


# --------------------------------------------------------------------------- provisioning seam

#: the *command* sandbox image. Deliberately boring — a plain dev box with python + pytest — so
#: nothing inside it hints that failures are being injected one layer up.
SANDBOX_APP_NAME = os.environ.get("FAULTLINE_SANDBOX_APP", "faultline-sandboxes")
SANDBOX_TIMEOUT_S = int(os.environ.get("FAULTLINE_SANDBOX_TIMEOUT", "1800"))
SANDBOX_IDLE_TIMEOUT_S = int(os.environ.get("FAULTLINE_SANDBOX_IDLE_TIMEOUT", "600"))


def sandbox_image():
    """Built at deploy time by the `prewarm` function in modal_app.py so reset stays fast."""
    import modal

    return (
        modal.Image.debian_slim(python_version="3.11")
        .pip_install("pytest==7.4.4")
        .run_commands(
            "mkdir -p /workspace /opt",
            'ln -sf "$(command -v python3)" /usr/local/bin/python || true',
        )
        .env({"PYTHONDONTWRITEBYTECODE": "1"})
    )


def create_workspace() -> "Workspace":
    """Provision a fresh command sandbox and install the helper. Monkeypatched in tests."""
    import modal

    t0 = time.perf_counter()
    sandbox_app = modal.App.lookup(SANDBOX_APP_NAME, create_if_missing=True)
    sb = modal.Sandbox.create(
        app=sandbox_app,
        image=sandbox_image(),
        workdir=WORKSPACE,
        timeout=SANDBOX_TIMEOUT_S,
        idle_timeout=SANDBOX_IDLE_TIMEOUT_S,
        cpu=1,
        memory=1024,
        block_network=True,  # the agent's shell has no egress at all
    )
    ws = ModalWorkspace(sb)
    log.info(
        "sandbox.created", "command sandbox up",
        sandbox_id=ws.sandbox_id, dur_ms=int((time.perf_counter() - t0) * 1000),
        block_network=True, timeout_s=SANDBOX_TIMEOUT_S, idle_timeout_s=SANDBOX_IDLE_TIMEOUT_S,
    )
    ws.install_helper()
    return ws


def open_workspace(sandbox_id: str) -> "Workspace":
    """Attach to an existing sandbox by id (api containers are stateless). Patched in tests."""
    import modal

    try:
        return ModalWorkspace(modal.Sandbox.from_id(sandbox_id))
    except Exception as exc:  # noqa: BLE001
        raise WorkspaceError(f"sandbox {sandbox_id} is unreachable: {type(exc).__name__}: {exc}") from exc


__all__ = [
    "ExecResult", "ReadResult", "WriteResult", "Entry", "ListResult", "Workspace",
    "WorkspaceError", "ModalWorkspace", "FakeWorkspace", "HELPER_SOURCE", "tar_bytes",
    "local_tar_of", "SKIP_DIRS", "sandbox_image", "create_workspace", "open_workspace",
    "SANDBOX_APP_NAME",
]

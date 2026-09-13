"""Workspace path safety.

Every path the agent hands us is workspace-relative. Absolute paths are accepted only when they
live under /workspace; anything else, and any `..` component, is EINVAL before we touch the
sandbox. Pure module, no I/O.
"""

from __future__ import annotations

import posixpath

WORKSPACE = "/workspace"
#: baseline copy of the pristine fixture; deliberately OUTSIDE the workspace so `observe`
#: can diff against it and the agent never sees it in its tree.
BASELINE = "/opt/faultline_baseline"
#: helper script uploaded at reset; runs file ops/diffing inside the sandbox in one round trip.
HELPER = "/opt/faultline_helper.py"
#: hidden tests are copied here at evaluate time and removed immediately afterwards.
EVAL_DIR = ".faultline_eval"


class PathError(ValueError):
    """Raised for a path that escapes the workspace. Callers map this to ToolError(EINVAL)."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def normalize_rel(path: str | None) -> str:
    """Return a clean workspace-relative path ('.' for the root), or raise PathError.

    Accepts: 'README.md', './README.md', 'src/x.py', '/workspace/src/x.py', '/workspace', ''.
    Rejects: '/etc/passwd', '../escape', 'src/../../escape', anything with a NUL byte.
    """
    raw = "" if path is None else str(path)
    if "\x00" in raw:
        raise PathError(raw, "invalid path")
    p = raw.strip()
    if not p:
        return "."
    if p.startswith("/"):
        if p == WORKSPACE:
            p = "."
        elif p.startswith(WORKSPACE + "/"):
            p = p[len(WORKSPACE) + 1 :]
        else:
            raise PathError(raw, f"path outside {WORKSPACE} is not allowed")
    # reject any explicit parent traversal, even one that would normalise back inside
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise PathError(raw, "'..' traversal is not allowed")
    norm = posixpath.normpath("/".join(parts)) if parts else "."
    if norm.startswith("/") or norm.startswith(".."):
        raise PathError(raw, "path escapes the workspace")
    return norm or "."


def abs_path(rel: str) -> str:
    """Workspace-relative -> absolute path inside the sandbox."""
    rel = rel.strip()
    if rel in ("", "."):
        return WORKSPACE
    if rel.startswith("/"):
        return rel
    return posixpath.join(WORKSPACE, rel)


def parent_of(rel: str) -> str:
    """Directory containing `rel`, as a workspace-relative path ('.' at the root)."""
    d = posixpath.dirname(rel.rstrip("/"))
    return d or "."


def basename_of(rel: str) -> str:
    return posixpath.basename(rel.rstrip("/"))

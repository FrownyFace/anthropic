"""Path safety: the agent never gets to address anything outside /workspace."""

from __future__ import annotations

import pytest

from sandbox_env.paths import PathError, abs_path, basename_of, normalize_rel, parent_of


@pytest.mark.parametrize(
    "raw,want",
    [
        ("README.md", "README.md"),
        ("./README.md", "README.md"),
        ("src/ratelimiter/limits.py", "src/ratelimiter/limits.py"),
        ("/workspace/src/limits.py", "src/limits.py"),
        ("/workspace", "."),
        ("", "."),
        (".", "."),
        ("  config/settings.json  ", "config/settings.json"),
        ("a//b", "a/b"),
        ("./a/./b", "a/b"),
        (None, "."),
    ],
)
def test_normalize_accepts(raw, want):
    assert normalize_rel(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        "/etc/passwd",
        "/opt/faultline_baseline/README.md",  # the grader's private copy stays private
        "/workspacex/evil",
        "../escape",
        "../../etc/passwd",
        "src/../../escape",
        "src/../ok",  # rejected too: any '..' component at all
        "a/b/../../../c",
        "bad\x00name",
    ],
)
def test_normalize_rejects(raw):
    with pytest.raises(PathError):
        normalize_rel(raw)


def test_abs_and_parents():
    assert abs_path(".") == "/workspace"
    assert abs_path("a/b.py") == "/workspace/a/b.py"
    assert parent_of("src/ratelimiter/limits.py") == "src/ratelimiter"
    assert parent_of("README.md") == "."
    assert basename_of("src/ratelimiter/limits.py") == "limits.py"

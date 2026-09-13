"""Small shared helpers: ids, timestamps, digests, and the truncation policy."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from faultline_common.schemas import CONTENT_CAP, STDIO_CAP

#: keep the start (where the command echoes what it did) and the end (where the failure is)
STDIO_HEAD, STDIO_TAIL = 5_000, 3_000
CONTENT_HEAD, CONTENT_TAIL = 24_000, 8_000
_HINT = "use head/tail/grep to page through the rest"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def args_digest(args: dict[str, Any]) -> str:
    """sha256[:12] of the canonical JSON args — the ledger's 'same call again?' key."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def truncate_middle(text: str, cap: int, head: int, tail: int) -> tuple[str, bool]:
    """Cap a blob by keeping its head and tail with an explicit elision marker.

    Chopping only the tail hides the traceback that explains the failure; chopping only the head
    hides the command echo. Keeping both ends (and saying how much went missing) is what makes a
    truncated tool result still actionable for the model.
    """
    if text is None:
        return "", False
    if len(text) <= cap:
        return text, False
    elided = len(text) - head - tail
    if elided <= 0:  # cap smaller than head+tail: degrade to a plain head cut
        return text[:cap] + f"\n...<elided {len(text) - cap} chars; {_HINT}>...\n", True
    return (
        text[:head] + f"\n...<elided {elided} chars; {_HINT}>...\n" + text[len(text) - tail :],
        True,
    )


def truncate_stdio(text: str) -> tuple[str, bool]:
    return truncate_middle(text, STDIO_CAP, STDIO_HEAD, STDIO_TAIL)


def truncate_content(text: str) -> tuple[str, bool]:
    return truncate_middle(text, CONTENT_CAP, CONTENT_HEAD, CONTENT_TAIL)


def drop_lines_mentioning(text: str, names: list[str]) -> str:
    """Best-effort removal of a vanished filename from a directory listing.

    A transient `missing_file` fault must look like the file is not there: if `read_file README.md`
    says ENOENT, `ls` must not list it in the same breath. We only touch lines that name the file.
    """
    if not names:
        return text
    keep = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if any(_line_names(stripped, n) for n in names):
            continue
        keep.append(line)
    return "".join(keep)


def _line_names(line: str, rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    if not base:
        return False
    tokens = line.replace("\t", " ").split(" ")
    for t in tokens:
        t = t.strip().strip("'\"")
        if not t:
            continue
        t = t[2:] if t.startswith("./") else t
        if t == base or t == rel or t.endswith("/" + base):
            return True
    return False

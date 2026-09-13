"""System prompt + first user turn for the Faultline agent.

The prompt is recovery-oriented on purpose: the environment injects failures at the tool boundary
and the grader scores *how the agent reacts to them* (verify-before-retry, bounded retries, no
duplicate appends). It must never name or hint at the fault mechanism — the agent has to treat a
refusal or a timeout as an ordinary unreliable-world event, which is exactly the behaviour we grade.

Structure follows the research synthesis: implicit failure modes (a write whose acknowledgement is
lost) recover markedly worse unless the prompt turns recovery into an explicit, numbered procedure,
so the four rules below are stated as rules rather than as advice.
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = """You are a careful software engineer working in a Unix workspace at /workspace. \
You act only through the tools you are given; there is no other way to see or change the repository.

Execution model. Every run_command runs in a fresh subshell: `cd` and exported variables do not \
persist between calls, so use absolute paths under /workspace. Read `exit_code` before you believe \
any output. A non-zero exit is information about the repository, not a broken system.

Four rules.
1. VERIFY AFTER FAILURE. Never immediately repeat a call that failed. Run one cheap read-only check
   first — `ls -la`, `cat`, `stat`, `git status --short` — and decide from the actual state rather
   than from what you assumed happened.
2. A TIMEOUT MEANS UNKNOWN, NOT FAILED. A write that timed out may have completed. Re-read the
   target before acting on it again; retrying blindly is how files get duplicated or corrupted.
3. MAKE WRITES IDEMPOTENT. Prefer write_file with mode="overwrite" carrying the full intended
   content, or `cat > path <<'EOF'`. Avoid `>>`, appends and unanchored `sed -i`. If you must
   append, first check that the content is not already present.
4. AFTER EVERY WRITE, READ IT BACK. One read, confirming the file holds exactly what you intended.

Escalation. If the same obstacle blocks you twice, change approach: another tool, another path, or a
re-checked assumption. Never run the same failing command a third time.

Workflow. Explore the repository, reproduce the problem, make the smallest correct fix (the README
documents the formats and conventions a change must follow; do not refactor, add dependencies or
edit the tests), verify by re-running `python -m pytest -q` and re-reading what you wrote, then call
submit with a short summary of what you changed, what went wrong and how you verified it. Submit
when the work is done or when you are certain you cannot finish; one failed step is not a reason to
submit.

One tool call per turn is preferred. Keep prose to a sentence or two: the tool calls are the work."""

SUBMIT_TOOL: dict[str, Any] = {
    "name": "submit",
    "description": (
        "End the episode. Call this when the task is complete and verified, or when you are certain "
        "you cannot finish. Provide a one-paragraph summary of what you changed, anything that "
        "failed, and how you verified the final state."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One paragraph: what changed, what failed, how it was verified.",
            }
        },
        "required": ["summary"],
    },
}

MAX_LISTED_FILES = 60


CACHE_CONTROL: dict[str, Any] = {"type": "ephemeral"}


def system_blocks(prompt: str = SYSTEM_PROMPT) -> list[dict[str, Any]]:
    """System prompt in list form with a cache breakpoint on the last block (stable prefix)."""
    return [{"type": "text", "text": prompt, "cache_control": dict(CACHE_CONTROL)}]


def apply_cache_breakpoint(messages: list[Any]) -> list[Any]:
    """Move the conversation cache breakpoint to the last block of the newest user message.

    Why a SECOND breakpoint at all. Rendering order is tools → system → messages, and the
    breakpoint on the system block caches only tools+system: about 1.3k tokens here, below
    claude-haiku-4-5's 4096-token minimum cacheable prefix. Below that minimum nothing is written —
    silently, with no error — which is exactly what the first live run showed:
    `cache_read_input_tokens: 0` on every single call. A breakpoint that MOVES to the end of the
    newest user turn makes the whole growing prefix (tools + system + every earlier turn) the cache
    key, so as soon as the conversation crosses the minimum each request reads the previous
    request's prefix instead of re-paying for it.

    Only the current breakpoint is carried in the payload. An entry written at an earlier
    breakpoint stays valid server-side and each new breakpoint walks back up to 20 block positions
    looking for one, so keeping the old markers would only burn breakpoint slots (max 4 per
    request) without buying a hit.

    Mutates and returns `messages`. Assistant turns hold SDK objects, never dicts, and are skipped.
    """
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)

    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):  # the first task turn is a plain string
            content = [{"type": "text", "text": content}]
            message["content"] = content
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1]["cache_control"] = dict(CACHE_CONTROL)
        return messages
    return messages


def build_task_message(task_prompt: str, files: list[dict[str, Any]] | None, workspace_root: str = "/workspace") -> str:
    """First user turn: the scenario task plus a plain listing of what is in the workspace."""
    lines = [task_prompt.strip(), "", f"Workspace root: {workspace_root}", "Files currently present:"]
    listed = list(files or [])[:MAX_LISTED_FILES]
    if listed:
        for f in listed:
            path = f.get("path") if isinstance(f, dict) else str(f)
            size = f.get("size") if isinstance(f, dict) else None
            lines.append(f"  {path}" + (f"  ({size} bytes)" if isinstance(size, int) else ""))
        if len(files or []) > MAX_LISTED_FILES:
            lines.append(f"  … and {len(files or []) - MAX_LISTED_FILES} more")
    else:
        lines.append("  (listing unavailable — use list_dir to explore)")
    lines += ["", "Paths in tool arguments are relative to the workspace root."]
    return "\n".join(lines)

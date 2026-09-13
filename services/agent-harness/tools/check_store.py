#!/usr/bin/env python3
"""Live check of the harness Store, identity scoping and the conversation routes (PLAN.md §2.9).

    services/agent-harness/.venv/bin/python services/agent-harness/tools/check_store.py \
        --user u_<uuid4> --out runs/<ts>_store/store_check.json

Every assertion runs against the DEPLOYED harness over HTTP — nothing here imports the service.
Exit code 0 iff every check passes. Run it once before a redeploy and once after: the same command
then proves the database survived (`--expect-conversations N`, `--expect-run <run_id>`).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import uuid
from typing import Any

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "packages" / "common"))

from faultline_common.schemas import (  # noqa: E402
    Conversation,
    ConversationDetail,
    ConversationSummary,
    RunRecord,
)

DEFAULT_HARNESS = "https://appliedlabsai-local--faultline-harness-api.modal.run"


class Checks:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, ok: bool, name: str, detail: Any = "") -> bool:
        self.rows.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {str(detail)[:160]}" if detail else ""))
        return bool(ok)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.rows if r["ok"])

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if not r["ok"])


def main() -> int:
    ap = argparse.ArgumentParser(description="Check the harness Store against a deployed harness.")
    ap.add_argument("--harness", default=DEFAULT_HARNESS)
    ap.add_argument("--user", required=True, help="the browser identity whose data must be there")
    ap.add_argument("--expect-conversations", type=int, default=1)
    ap.add_argument("--expect-run", default=None, help="run id that must still be readable")
    ap.add_argument("--out", default=None, help="write the evidence JSON here")
    ap.add_argument("--label", default="store_check")
    args = ap.parse_args()

    base = args.harness.rstrip("/")
    me = {"X-Faultline-User": args.user}
    stranger = {"X-Faultline-User": f"u_{uuid.uuid4()}"}
    c = Checks()
    evidence: dict[str, Any] = {"harness": base, "user": args.user, "label": args.label}

    with httpx.Client(timeout=60.0, follow_redirects=True) as http:
        # ------------------------------------------------------------------ health
        health = http.get(f"{base}/health").json()
        evidence["health"] = health
        store = (health.get("detail") or {}).get("store") or {}
        c.add(store.get("ok") is True, "health.store.ok", store.get("error") or "")
        c.add("last_checkpoint_at" in store, "health.store.last_checkpoint_at",
              store.get("last_checkpoint_at"))
        c.add(health.get("has_provider_key") is False, "health.has_provider_key is false")

        # ------------------------------------------------------------------ identity
        mine = http.get(f"{base}/me", headers=me).json()
        evidence["me"] = mine
        c.add(mine.get("user_id") == args.user, "GET /me returns this user", mine.get("user_id"))
        c.add(http.get(f"{base}/me").status_code == 400, "GET /me without an id is a 400")
        c.add(http.get(f"{base}/me", headers={"X-Faultline-User": "bogus"}).status_code == 400,
              "GET /me with a malformed id is a 400")

        # ------------------------------------------------------------------ conversations
        listing = http.get(f"{base}/conversations", headers=me).json()
        rows = listing if isinstance(listing, list) else listing.get("conversations") or []
        evidence["conversations"] = rows
        c.add(len(rows) >= args.expect_conversations,
              f"GET /conversations has >= {args.expect_conversations} for this user", len(rows))
        for row in rows:
            ConversationSummary.model_validate(row)
        c.add(True, "every conversation row validates as ConversationSummary", len(rows))
        c.add(mine.get("conversations") == len(rows), "GET /me count matches the list",
              f"{mine.get('conversations')} vs {len(rows)}")

        strangers = http.get(f"{base}/conversations", headers=stranger).json()
        stranger_rows = strangers if isinstance(strangers, list) else strangers.get("conversations") or []
        c.add(stranger_rows == [], "a fresh browser id sees an empty list", len(stranger_rows))

        if rows:
            cid = rows[0]["id"]
            detail_raw = http.get(f"{base}/conversations/{cid}", headers=me).json()
            evidence["conversation_detail"] = detail_raw
            detail = ConversationDetail.model_validate(detail_raw)
            c.add(True, "GET /conversations/{id} validates as ConversationDetail",
                  f"{len(detail.messages)} messages / {len(detail.runs)} runs")
            c.add(len(detail.messages) > 0, "the persisted transcript is not empty",
                  len(detail.messages))
            blocks = [b for m in detail.messages for b in m.blocks]
            tool_uses = {b.tool_use_id for b in blocks if b.type == "tool_use"}
            results = {b.tool_use_id for b in blocks if b.type == "tool_result"}
            c.add(tool_uses and tool_uses == results,
                  "every tool_use block has its tool_result (joined by tool_use_id)",
                  f"{len(tool_uses)} calls / {len(results)} results")
            c.add(detail.messages[0].role == "user" and detail.messages[0].blocks[0].type == "text",
                  "the transcript starts with the task prompt as a user message",
                  (detail.messages[0].blocks[0].text or "")[:80])
            c.add([m.seq for m in detail.messages] == sorted(m.seq for m in detail.messages),
                  "messages are ordered by seq")
            c.add(http.get(f"{base}/conversations/{cid}", headers=stranger).status_code == 404,
                  "another browser id gets 404 (not 403) for this conversation")

        # ------------------------------------------------------------------ the run itself
        if args.expect_run:
            got = http.get(f"{base}/runs/{args.expect_run}", headers=me)
            c.add(got.status_code == 200, f"GET /runs/{args.expect_run} still exists", got.status_code)
            if got.status_code == 200:
                record = RunRecord.model_validate(got.json())
                evidence["run"] = {"run_id": record.run_id, "status": record.status,
                                   "events": len(record.events),
                                   "score": (record.evaluation.score if record.evaluation else None),
                                   "conversation_id": record.conversation_id,
                                   "usage": record.usage.model_dump()}
                c.add(record.status in {"ok", "truncated"}, "the run is still terminal", record.status)
                c.add(len(record.events) > 0, "its events survived", len(record.events))
                c.add([e.id for e in record.events] == list(range(len(record.events))),
                      "event ids are contiguous and 0-based (SSE resume depends on it)")
                c.add(record.evaluation is not None and record.evaluation.score is not None,
                      "the evaluation survived",
                      record.evaluation.score if record.evaluation else None)
                c.add(record.user_id == args.user, "the run is still owned by this user",
                      record.user_id)
                c.add(http.get(f"{base}/runs/{args.expect_run}", headers=stranger).status_code == 404,
                      "another browser id gets 404 for this run")
                c.add(record.usage.cache_read_input_tokens >= 0, "usage carries the cache counters",
                      record.usage.model_dump())

        scoped = http.get(f"{base}/runs", headers=me).json().get("runs") or []
        c.add(all(r.get("run_id") for r in scoped), "GET /runs rows carry run_id", len(scoped))
        c.add(len(http.get(f"{base}/runs", headers=stranger).json().get("runs") or []) == 0,
              "GET /runs is scoped: a fresh id sees nothing")

        # ------------------------------------------------------------------ CRUD on a throwaway
        made = http.post(f"{base}/conversations", json={"scenario_id": "lost-ack"}, headers=me)
        c.add(made.status_code == 200, "POST /conversations", made.status_code)
        if made.status_code == 200:
            conv = Conversation.model_validate(made.json())
            patched = http.patch(f"{base}/conversations/{conv.id}", json={"title": "renamed by check"},
                                 headers=me).json()
            c.add(patched.get("title") == "renamed by check", "PATCH /conversations/{id} renames")
            c.add(http.delete(f"{base}/conversations/{conv.id}", headers=me).json() == {"archived": True},
                  "DELETE /conversations/{id} archives")
            after = http.get(f"{base}/conversations", headers=me).json()
            after_rows = after if isinstance(after, list) else after.get("conversations") or []
            c.add(all(r["id"] != conv.id for r in after_rows),
                  "the archived conversation is gone from the list", len(after_rows))

    evidence["checks"] = c.rows
    evidence["summary"] = {"passed": c.passed, "failed": c.failed}
    if args.out:
        path = pathlib.Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence, indent=2, default=str))
        print(f"\nevidence: {path}")
    print(f"STORE CHECK {'PASS' if c.failed == 0 else 'FAIL'} :: {c.passed} passed, {c.failed} failed")
    return 0 if c.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

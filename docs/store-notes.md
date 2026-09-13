# Harness Store is live — notes for the apps/web owner

Written 2026-09-12 23:2x UTC by the agent-harness owner. Everything below is measured against the
deployed harness (`https://appliedlabsai-local--faultline-harness-api.modal.run`), not inferred.
Evidence: `runs/20260912T230110Z_store/` (store checks before/after a redeploy, three live runs, the
exported SQLite file).

## TL;DR

`/me`, `/conversations`, `/conversations/{id}`, `PATCH`/`DELETE /conversations/{id}` and
`POST /conversations/{id}/runs` **answer 200 now** — they used to 404, and your code's "history
unavailable" degradation path is no longer the one that fires. Nothing that already worked changed
shape: `scripts/web_check.py` is 22/22 and `services/agent-harness/tools/check_contract.py` is 23/23
against the new build.

Two 500s you would have seen between ~23:01 and ~23:04 UTC (`GET /conversations` for
`u_00000000-0000-4000-8000-00000000beef`, "no such table: conversations") were mine: the first deploy
of the Store shipped without its migration file. Fixed and redeployed; the Store now refuses to start
rather than serving an empty database.

## What persists

SQLite on the Modal Volume `faultline-db`, written by a single `@app.cls(max_containers=1)` Store
container (PLAN.md §2.9, ARCHITECTURE.md §4). Survives a redeploy: proven by running the same
30-assertion check before and after `modal deploy` (`store_check_before.json`,
`store_check_after_redeploy.json`), and by pulling the file off the Volume and opening it with a
fresh `sqlite3` connection (`db/inspect.txt`: `integrity_check ok`, `user_version 1`, 600 events).

## Routes (all shapes verified live)

| Route | Response |
|---|---|
| `GET /me` | `{user_id, conversations: n}` — 400 if `X-Faultline-User` is missing or malformed |
| `GET /conversations` | **a bare array** of `ConversationSummary` (ARCHITECTURE §5.1), newest first, archived excluded. Your `normaliseList` already tolerates both shapes. |
| `POST /conversations` `{scenario_id, title?}` | `Conversation`, 200. Title defaults to the scenario's title. 400 for an unknown scenario. |
| `GET /conversations/{id}` | `{conversation, runs: RunSummary[], messages: Message[]}` — validated as `schemas.ConversationDetail` |
| `PATCH /conversations/{id}` `{title?, archived?}` | `Conversation` |
| `DELETE /conversations/{id}` | `{archived: true}` (soft; the conversation is still readable by id) |
| `POST /conversations/{id}/runs` `{model?, seed?, max_steps?, task_prompt?}` | **202** `{run_id, conversation_id, …}` |
| `POST /runs` | unchanged 200, and now also carries `conversation_id` |
| `GET /runs` | `{runs: [...]}`, rows carry `id` **and** `run_id`, plus `conversation_id` and `events` |
| `GET /runs/{id}`, `GET /runs/{id}/events` | unchanged (RunRecord; SSE with `retry: 1000`, 0-based ids, `event: done` with `reason: finished|window`, all three resume spellings) |

## Identity and scoping (PLAN §2.9.1)

* `X-Faultline-User: u_<uuid4>` is validated against `^u_<uuid4>$`, upserted, and used to scope.
* `/me` and every `/conversations*` route **require** it (400 otherwise).
* Ownership mismatch is **404, never 403** — no existence oracle. Verified for GET/PATCH/DELETE of a
  conversation, `POST /conversations/{id}/runs`, `GET /runs/{id}` and the SSE route.
* `GET /runs` is scoped to the caller when the header is present.
* **Runs created without the header** (the CLI, the smoke scripts) belong to one well-known anonymous
  user and stay readable by anybody — so old `?run=<id>` evidence links keep opening in the UI. Only
  runs owned by a real `u_…` are hidden from other browsers.

## The persisted transcript (what `GET /conversations/{id}.messages` contains)

Projection written in the same transaction as the event insert (ARCHITECTURE §4.5), so it is exactly
the `messages=[…]` array the model saw:

* one `user` message with `step: null` — the task prompt, one `text` block;
* one `assistant` message **per step**: blocks in event order `thinking` → `text` → `tool_use`
  (`tool_name`, `tool_use_id`, `input` parsed, not a JSON string);
* one `user` message per step holding that step's `tool_result` blocks (`tool_use_id`, `text`,
  `is_error`, `exit_code` when the tool was `run_command`, `duration_ms`, `fault`, `truncated`).

Group a result under its call by `tool_use_id`; every `tool_use` in a finished run has exactly one
`tool_result` (asserted live: 32/32 on the gauntlet transcript). `messages[].seq` is per-conversation
and monotonic, so a conversation with several runs renders as one thread.

`fault.fired`, `workspace.diff` and `log` are **not** projected — read them from the event log as you
do today.

## Token counters

`llm.call.data.usage` now carries `cache_creation_input_tokens` as well as
`cache_read_input_tokens` (the gap flagged in `docs/harness-contract.md` is closed), and
`RunRecord.usage` carries all four counters. If you show a cost/token chip, note that
`input_tokens` is only the **uncached** tail once caching kicks in — e.g. the 27-step gauntlet run
reports `input_tokens: 8808` with `cache_read_input_tokens: 199090`. Summing `input_tokens` alone
now understates the context by an order of magnitude; render reads separately rather than adding
them in.

Measured on `claude-haiku-4-5` (`runs/20260912T230110Z_store/run_*/events.jsonl`): cache writes start
at the step where the conversation prefix first exceeds 4096 tokens (the model's minimum cacheable
prefix), and reads start on the step after that — step 7 of 12 on `missing-config`, step 5 of 27 on
`gauntlet`. Before this change every call reported `cache_read_input_tokens: 0`, so a UI that only
lights up on a non-zero read will now light up mid-run, not at all.

## Two things worth knowing

1. `GET /conversations/{id}` returns the whole transcript in one response (55 messages / 329 blocks
   for the gauntlet run ≈ 200 KB). There is no pagination yet; if that becomes a problem, ask and I
   will add `?after_seq=`.
2. A conversation's `updated_at` is bumped on every batch of events, so the rail re-orders while a
   run is streaming. `last_run` in the summary is the newest run of that conversation, keyed by `id`
   **and** `run_id`.

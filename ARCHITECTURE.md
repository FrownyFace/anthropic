# Faultline — architecture

> Companion to `PLAN.md` (the living checklist). This document describes *how the system is built and
> why*; the plan tracks *what is left to do*. When a decision here changes, update §9 and the plan
> together. Status of facts below: verified against `modal==1.5.5`, Node 22 (≥ 22.12) with pnpm 9.15.4, and the
> public docs on 2026-09-12. Code is canonical for schema and routes; this document summarises them. Secret name in Modal env `local`: `anthropic-secret`.

## 1. What it is

Faultline is a mini agent harness with a browser view. A Claude agent runs real shell commands (read
files, edit code, run tests) inside an isolated Modal Sandbox while the environment deliberately injects
failures at the tool boundary: a missing file, a denied write, a write that lands but whose acknowledgement
times out. The browser shows the run as a chat transcript (ChatGPT / claude.ai shape): the task prompt,
every model turn, every tool call with its output and any fault that hit it, the workspace diff, and a
graded score. Runs are grouped into conversations that belong to an anonymous browser identity and are
persisted in SQLite on a Modal Volume, so a reviewer can close the tab, come back, and re-read the
transcript.

Three deployables, one repo:

| Deployable | Modal app | Purpose |
|---|---|---|
| `apps/web` | `faultline-web` | static Vite + React SPA (shadcn/ui + Beautiful UI) served by a Modal Server |
| `services/agent-harness` | `faultline-harness` | HTTP API, the model loop (`run_episode`), and the persistence actor (`Store`) |
| `services/sandbox-env` | `faultline-sandbox-env` | the gym: episodes, fault plan, MCP tools, grader; drives Modal Sandboxes |

Shared code: `packages/common/faultline_common` (`schemas.py` = wire contracts, `log.py` = unified
JSON-lines logging), mounted into every image.

## 2. Topology and trust boundaries

```
 browser  (apps/web on Modal Server)  ── anonymous id in localStorage + cookie mirror
    │  HTTPS JSON + SSE (reconnecting, Last-Event-ID); every request: X-Faultline-User: u_<uuid>
    ▼
 faultline-harness
    ├─ api          FastAPI @modal.asgi_app. No provider secret. Validates the user id, owns the HTTP
    │               contract (§5), spawns run_episode, and reads/writes state ONLY through Store.
    ├─ Store        @app.cls(max_containers=1, min_containers=1, volumes={"/data": faultline-db}).
    │               The single SQLite writer (§4). No secret, no fault plan, no network beyond Modal.
    └─ run_episode  @app.function(secrets=[anthropic-secret]). The ONLY code that sees ANTHROPIC_API_KEY.
                    Model loop over MCP tools; appends events to Store; evaluates; always deletes the episode.
    │  HTTPS: gym REST (reset / observe / evaluate / delete) + MCP streamable HTTP at /mcp (step)
    ▼
 faultline-sandbox-env
    └─ api          FastAPI + FastMCP. Stateless containers; episode state {sandbox_id, fault_plan,
                    ledger, baseline} in modal.Dict "faultline-episodes". Every tool call: load episode →
                    consult fault plan → (maybe) exec in the Sandbox → append to the ledger.
    │  modal.Sandbox.from_id(sid).exec(...)   block_network=True, no secrets, one per episode
    ▼
 Modal Sandbox      fixture repo at /workspace, python3 + pytest. Sees nothing else.
```

What each process can see:

| Process | Provider key | Fault plan / grader | Workspace files | Persistent DB | Network |
|---|---|---|---|---|---|
| browser | ✗ | ✗ (events about faults, after the fact) | via observe/events | via api only | harness only |
| harness `api` | ✗ | ✗ | ✗ | via Store RPC | Store + spawn |
| harness `Store` | ✗ | ✗ | ✗ (only what events carry) | ✓ (sole writer) | Volume only |
| harness `run_episode` | ✓ | ✗ | via MCP only | via Store RPC | Anthropic + sandbox-env |
| sandbox-env `api` | ✗ | ✓ | via Sandbox API | ✗ | Modal Sandbox only |
| Modal Sandbox (agent's shell) | ✗ | ✗ | ✓ | ✗ | none |

Why three apps: the harness must be replaceable (different model / prompt) without touching the
environment; the environment must be gradeable without trusting the agent; the UI is static. The secret
boundary is enforced by Modal's per-function secrets: `api` asserts at startup that `ANTHROPIC_API_KEY`
is *absent* from its environment and reports `has_provider_key: false` on `/health`.

## 3. Life of a run

1. **Identity.** On first load the SPA mints `u_<uuid4>`, stores it in `localStorage["faultline.user_id"]`
   and mirrors it to the `faultline_uid` cookie. Every API call carries `X-Faultline-User`. `GET /me`
   upserts the `users` row.
2. **Conversation.** The user picks a scenario in the composer. `POST /conversations {scenario_id}` creates
   a `conversations` row titled after the scenario. A conversation is a thread of runs; the user's
   "message" is the task prompt (default = scenario prompt, editable before Run).
3. **Run.** `POST /conversations/{id}/runs {model?, seed?, max_steps?, task_prompt?}` → Store creates a
   `runs` row (`queued`) → `api` calls `run_episode.spawn(run_id, …)` and returns `{run_id}` immediately
   (Modal web requests are capped at 150 s, so nothing long-running happens inside a request).
4. **Episode.** `run_episode` calls `POST /episodes` on sandbox-env (reset: create Sandbox, upload
   fixture, apply sticky faults, snapshot baseline), emits `run.started` and `episode.reset`, then loops:
   `messages.create` → for each `tool_use` call the MCP tool with `X-Faultline-Episode` → emit
   `tool.call` / `tool.result` (+ `fault.fired` from the ledger delta, `workspace.diff` from observe after
   mutating calls) → repeat until `submit`, `end_turn`, `max_steps` or an unrecoverable API error.
   Every `messages.create` also emits `llm.call` (usage, stop reason, request id, attempt).
5. **Persistence.** Events are appended to Store in per-step batches
   (`Store().append_events.remote(run_id, events)`), which inserts `events` rows and, in the same
   transaction, projects them into `messages` / `blocks` / `llm_calls` (§4.5). The loop keeps the events in
   memory too and re-sends the full list at `run.finished` (idempotent), so a Store restart self-heals.
6. **Streaming.** The browser tails `GET /runs/{id}/events` (SSE). `api` polls `Store.events_after(run_id,
   after_seq)` every 400 ms, closes the stream at ~110 s, and the browser reconnects with
   `Last-Event-ID` (= `events.seq`). A page refresh restores from `GET /runs/{id}` (whole record) or
   `GET /conversations/{id}` (persisted transcript) and resumes the tail.
7. **Grading.** `POST /episodes/{id}/evaluate` uploads hidden tests, runs pytest, removes them, and scores
   recovery checks from the ledger (`score = 60·tests_pass + 40·weighted checks`). `episode.evaluated`
   and `run.finished` update `runs.score / evaluation_json / status / tokens`; `DELETE /episodes/{id}`
   runs in `finally`.
8. **Checkpoint.** Store snapshots the DB to the Volume after `run.finished` (§4.3).

## 4. Persistence layer: SQLite on a Modal Volume

### 4.1 Requirements

Persist, per anonymous browser user: conversations, runs, the full event log of each run, and the LLM
turns including tool calls and their results, in a shape that can (a) render a chat transcript without
replaying events, and (b) rebuild the exact `messages=[…]` array the model saw at each step. Survive page
reloads, browser restarts, and redeploys. No external database service.

### 4.2 Modal facts that constrain the design

- Volumes are "write-once, read-many". Writes are visible to other containers only after a commit
  (Modal background-commits every few seconds and on container shutdown; `.commit()` forces one).
  Other containers must `.reload()` to see committed changes, and reload fails while files are open.
- "Last write wins in case of concurrent modification of the same file" — there is no distributed file
  locking. Concurrent writers to one SQLite file would corrupt it.
- The mount is a FUSE filesystem. SQLite's WAL mode relies on a shared-memory-mapped `-shm` file and
  POSIX advisory locks; their behaviour on the mount is undocumented. A background commit is not a
  point-in-time snapshot across files, so a live DB + its journal could be captured inconsistently.
- Modal's own SQLite example (the Datasette cron) builds the database on local disk, copies it onto the
  Volume, and commits. That is the pattern to copy.
- Volume v2 must be requested explicitly (`version=2`); the 1.5.5 CLI still marks it experimental. v1 is
  fine for one file.

### 4.3 Design: one writer, hot copy on local disk, consistent snapshots on the Volume

```
 api ──.remote()──▶ ┌───────────────── Store (1 container) ─────────────────┐
 run_episode ──────▶│ /tmp/faultline.sqlite3   WAL, synchronous=NORMAL, FK on │
                    │   ▲ restore on @modal.enter        checkpoint ▼         │
                    │ /data/faultline.sqlite3  ◀── VACUUM INTO .tmp + os.replace + vol.commit()
                    └────────────────────────────────────────────────────────┘
                                   Modal Volume "faultline-db" (v1)
```

- **Single writer.** `Store` is a Modal class with `max_containers=1`. It is the only process that ever
  opens the database, so SQLite's locking assumptions hold trivially and nobody needs `reload()` — the
  reader *is* the writer. `min_containers=1` keeps it warm (an SSE poll must not pay a cold start plus a
  restore). `@modal.concurrent(max_inputs=32)` lets many `api` polls and one run's appends interleave; a
  `threading.Lock` serialises access to the single `sqlite3` connection (ops are sub-millisecond).
- **Hot copy on local disk.** The live file is `/tmp/faultline.sqlite3` on the container's own disk with
  ordinary SQLite settings (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`,
  `busy_timeout=5000`). No FUSE questions.
- **Checkpoint.** `VACUUM INTO '/data/faultline.sqlite3.tmp'` writes a complete, consistent, single-file
  copy; `os.replace()` renames it over `/data/faultline.sqlite3`; `vol.commit()` publishes it. Because
  only complete files are ever renamed into place, a background commit can never capture a half-written
  database. Triggers: after every `run.finished`, every 15 s while there are unsaved writes, and in
  `@modal.exit` (as implemented, `run.finished` currently snapshots twice: once in `append_events` and
  again in `finish_run`). A `checkpoint_now` local entrypoint forces one for evidence/export.
- **Restore.** `@modal.enter` copies `/data/faultline.sqlite3` → `/tmp` if present (else creates an empty
  DB), then applies migrations: `PRAGMA user_version` gates `harness/migrations/000N_*.sql`, applied in
  order (intended: one transaction each; today `executescript` runs, then a separate `PRAGMA
  user_version`, so a crash mid-migration is not atomic — fix pending with the Store owner).
- **Durability model.** Completed runs are durable as soon as their post-`run.finished` checkpoint commits
  (seconds). If the Store container dies mid-run, at most the last ≤15 s of that run's events are lost
  from the Volume — and they are not lost from the system: `append_events` is idempotent on
  `(run_id, seq)` (`INSERT OR IGNORE`), and `run_episode` re-sends its complete in-memory event list at
  `run.finished`, which back-fills any gap. A redeploy briefly overlaps old and new Store containers; the
  same idempotent re-send covers the overlap for runs that finish, and the ≤15 s window is documented for
  runs that don't.
- **Ids.** `<prefix>_<hex ms timestamp><12 hex random>` (sortable, no extra dependency): `c_`, `r_`, `m_`,
  `b_`, `l_`. User ids are minted by the browser as `u_<uuid4>`.
- **Migration (done 2026-09-12, ~19:00 EDT).** `harness/store.py` `RunStore` is now a thin client over
  `Store` (`harness/sqlite_store.py`), so `loop.py` and `api.py` kept their verbs; the loop appends
  events per step. The Modal Dict `faultline-runs` is no longer used; its 21 runs were imported once with
  `modal_app.py::import_legacy_runs` (`runs/20260912T230110Z_store/`).
- **Ops.** `modal volume get faultline-db faultline.sqlite3 runs/` yields a consistent file for local
  `sqlite3` inspection and for the evidence folder. Retention: none for the take-home (a `DELETE`
  archives; nothing is hard-deleted).
- **Known limits (accepted).** One Store container; full-file snapshot per checkpoint. Comfortable to
  hundreds of MB; past that, ship incrementally (Litestream-style, or `modal-mosql`) or move to Postgres.
  A single-writer actor is also a throughput ceiling (thousands of events/s — orders of magnitude above
  need).

### 4.4 Schema (`services/agent-harness/harness/migrations/0001_init.sql`)

The migration file is canonical; this section summarises it (a full copy of the DDL used to live here and
drifted). All timestamps are ISO-8601 UTC; JSON columns hold pydantic dumps from `faultline_common.schemas`.

| Table | Key | Columns worth knowing |
|---|---|---|
| `users` | `id` (`u_<uuid4>`, minted by the browser, §6) | `created_at`, `last_seen_at`, `user_agent` |
| `conversations` | `id` (`c_…`) | `user_id`, `scenario_id`, `title` (defaults to the scenario title), `updated_at` (bumped on every event batch; drives list order), `archived_at` (soft delete) |
| `runs` | `id` (`r_…`) | `conversation_id`, `user_id` (denormalised for ownership checks), `scenario_id`, `model`, `seed`, `max_steps`, `task_prompt`, `episode_id`, `status` CHECK over all seven `RunStatus` values (`queued running ok error truncated unevaluated interrupted`), `score`, `evaluation_json`, token totals, `error`, timestamps, `steps`, `summary` (the agent's submit summary), `extra_json` (additive `RunRecord` fields) |
| `events` | `(run_id, seq)`, `WITHOUT ROWID` | append-only source of truth; `seq` == `Event.id` == SSE id; `type`, `step`, `data` |
| `messages` | `id` (`m_…`), unique `(conversation_id, seq)` | `run_id`, `role` user\|assistant, `step` (NULL for the task prompt) |
| `blocks` | `id` (`b_…`), unique `(message_id, seq)` | `type` text\|thinking\|tool_use\|tool_result, `text`, `tool_name`, `tool_use_id` (join key), `input_json`, `is_error`, `exit_code`, `duration_ms`, `fault_json`, `truncated` |
| `llm_calls` | `id` (`l_…`), unique `(run_id, step, attempt)` | `model`, `request_id`, `stop_reason`, four token counters, `started_at`, `duration_ms`, `error` |

Relationships: `users 1─∞ conversations 1─∞ runs 1─∞ events`; `runs 1─∞ messages 1─∞ blocks`;
`runs 1─∞ llm_calls`. `events` is the source of truth; `messages`/`blocks`/`llm_calls` are projections
written in the same transaction (dropping them is scope cut #5 in the plan — the web reducer can rebuild
a transcript from events alone).

### 4.5 Event → transcript projection

Performed inside `Store.append_events`, per event, in the transaction that inserts the event row:

| Event | Effect |
|---|---|
| `run.started` | `runs.status='running'`, `started_at`; `conversations.updated_at` |
| `episode.reset` | `runs.episode_id`; user message (`step NULL`) with one `text` block = `task_prompt` |
| `turn.thinking`, `turn.text`, `tool.call` (same `step`) | one assistant message for that step (created on first block); blocks appended in event order: `thinking`, `text`, `tool_use{tool_name, tool_use_id, input_json}` |
| `tool.result` (same `step`) | one user message for that step (Anthropic requires tool results in a user turn); `tool_result{tool_use_id, text=output, is_error, exit_code, duration_ms, fault_json}` |
| `llm.call` | `llm_calls` row; running totals into `runs.input_tokens/output_tokens` |
| `episode.evaluated` | `runs.score`, `runs.evaluation_json` (no message is projected; the submit summary is stored in `runs.summary`) |
| `run.finished` | `runs.status`, `finished_at`, `error`, final usage; `conversations.updated_at`; marks the DB dirty for checkpoint |
| `fault.fired`, `workspace.diff`, `log` | event row only; the UI reads them from the event log |

Rebuilding the model's view of step *n*: take the conversation's messages for the run with `seq` up to
the assistant message of step *n − 1* plus its tool-result message, expand blocks in `seq` order, map
`text/thinking/tool_use/tool_result` one-to-one onto Anthropic content blocks. The UI instead groups each
`tool_result` under its `tool_use` by `tool_use_id` and renders it as a tool chip inside the assistant turn.

## 5. HTTP contracts

### 5.1 Harness API (`faultline-harness/api`, CORS `*`, all routes below `/scenarios` and `/health` require `X-Faultline-User`)

| Route | Body → Response |
|---|---|
| `GET /health` | `Health{svc:"harness", ok, model_default, sandbox_env_url, has_provider_key:false, detail:{store:{ok,last_checkpoint_at}}}` |
| `GET /scenarios` | proxied from sandbox-env (public fields only) |
| `GET /me` | upsert → `{user_id, conversations: n}` |
| `GET /conversations` | `[{id, title, scenario_id, created_at, updated_at, last_run:{id,status,score}|null}]` newest first, archived excluded |
| `POST /conversations` | `{scenario_id, title?}` → `Conversation` |
| `GET /conversations/{id}` | `{conversation, runs:[RunSummary], messages:[Message{blocks:[Block]}]}` |
| `PATCH /conversations/{id}` | `{title?, archived?}` → `Conversation` |
| `DELETE /conversations/{id}` | soft archive → `{archived:true}` |
| `POST /conversations/{id}/runs` | `{model?, seed?, max_steps?, task_prompt?}` → `{run_id}` (202) |
| `POST /runs` | `{scenario_id, model?, seed?, max_steps?}` → creates a conversation then a run → `{run_id, conversation_id}` |
| `GET /runs/{id}` | `RunRecord` (record + all events + evaluation + usage) |
| `GET /runs/{id}/events` | SSE; `id:` = `seq`; honours `Last-Event-ID`; server closes at ~110 s |

Ownership: every `/conversations/*` and `/runs/*` route resolves the resource, compares `user_id` with
the header, and returns 404 on mismatch (not 403 — no existence oracle).

Wire shapes (JSON; the pydantic models in `faultline_common.schemas` and `apps/web/src/lib/types.ts`
mirror these field for field; DB columns `input_json` / `fault_json` / `evaluation_json` are parsed
into `input` / `fault` / `evaluation` on the wire):

```ts
type User                = { id: string; created_at: string; last_seen_at: string }
type Conversation        = { id: string; user_id: string; scenario_id: string; title: string;
                             created_at: string; updated_at: string; archived_at: string | null }
type ConversationSummary = { id: string; title: string; scenario_id: string; created_at: string; updated_at: string;
                             last_run: { run_id: string; status: RunStatus; score: number | null } | null }
type Block               = { id: string; seq: number; type: 'text' | 'thinking' | 'tool_use' | 'tool_result';
                             text?: string | null; tool_name?: string | null; tool_use_id?: string | null;
                             input?: Record<string, unknown> | null; is_error?: boolean | null; exit_code?: number | null;
                             duration_ms?: number | null; fault?: FaultFired | null; truncated: boolean }
type Message             = { id: string; conversation_id: string; run_id: string; seq: number;
                             role: 'user' | 'assistant'; step?: number | null; created_at: string; blocks: Block[] }
type LlmCall             = { id: string; run_id: string; step: number; attempt: number; model: string;
                             request_id?: string | null; stop_reason?: string | null;
                             input_tokens?: number | null; output_tokens?: number | null;
                             cache_read_tokens?: number | null; cache_write_tokens?: number | null;
                             started_at: string; duration_ms?: number | null; error?: string | null }
type RunSummary          = { id: string; run_id: string /* same value, kept for the CLI */; status: RunStatus;
                             scenario_id: string; model: string; score: number | null; created_at: string;
                             finished_at: string | null; events?: number }
// RunRecord gains three optional fields: conversation_id?, user_id?, task_prompt?
// Event.data for the two new types:
//   'llm.call'      { attempt, model, stop_reason, usage: { input_tokens, output_tokens,
//                     cache_read_input_tokens?, cache_creation_input_tokens? }, request_id?, duration_ms }
//   'turn.thinking' { text }
// Responses:
//   GET  /me                        → { user_id: string; conversations: number }
//   GET  /conversations             → ConversationSummary[]
//   POST /conversations             → Conversation
//   GET  /conversations/{id}        → { conversation: Conversation; runs: RunSummary[]; messages: Message[] }
//   PATCH /conversations/{id}       → Conversation          DELETE → { archived: true }
//   POST /conversations/{id}/runs   → { run_id: string; conversation_id: string }
//   POST /runs                      → { run_id: string; conversation_id: string }
```

As implemented, a request to `GET /runs` without `X-Faultline-User` is not scoped to a user (the CLI and
the contract checkers rely on it), which contradicts "never listed" in §6 — open item with the Store
owner.

### 5.2 Sandbox-env (gym + MCP)

As `PLAN.md` §2.2: REST `POST /episodes` (reset), `GET /episodes/{id}` (observe),
`POST /episodes/{id}/evaluate`, `DELETE /episodes/{id}`, `GET /scenarios`, `GET /health`, plus the ops
routes `GET /episodes` and `POST /episodes/sweep` (`docs/sandbox-env-ops.md`); MCP tools
`run_command`, `read_file`, `write_file`, `list_dir` at `/mcp`, episode selected by the
`X-Faultline-Episode` header so the model can never name another episode.

### 5.3 Event stream

Event types and payloads are defined in `faultline_common.schemas.Event` (`run.started`, `episode.reset`,
`turn.thinking`, `turn.text`, `tool.call`, `tool.result`, `llm.call`, `fault.fired`, `workspace.diff`,
`episode.evaluated`, `run.finished`, `log`; §5.4 adds `interruption`, `run.resumed` and `episode.sandbox`
to the contract, not yet emitted). `Event.id` is the 0-based sequence within the run and doubles
as the SSE id, the `events.seq` primary key, and the resume cursor. Events are produced by exactly one
writer per run (`run_episode`) and are never edited.

### 5.4 Reliability event contract (confirmed with the harness lead 2026-09-12 ~22:45 UTC)

**Status (2026-09-12 19:40 EDT): contract only.** Every field below is defined in `schemas.py` and the
docs, but no service emits it yet: sandbox-env still answers a lost sandbox with `EINTERNAL` and has no
`/interruptions` route, and the harness has no crash/resume path. The lead is implementing it.

Canonical: `packages/common/faultline_common/schemas.py` (the `Event` docstring lists every data
shape), labels in `docs/error-taxonomy.md`, injector behaviour in `services/sandbox-env/FAULTS.md`.
The UI is a ten-second reliability read — *task → fault (simulated or real, which layer) → did the
command execute (executed / failed / not executed / unknown are four different answers) → recovery
(agent actions vs harness retries; did the sandbox survive) → diff + independent checks* — and it
**never derives an outcome from error text**: anything absent renders as "unknown" with the raw
payload expandable. Fields (all additive):

| Where | Field | Meaning |
|---|---|---|
| `tool.result.data` | `outcome: executed \| failed \| not_executed \| unknown` | executed = ran and succeeded; failed = ran and reported failure; not_executed = refused/short-circuited before the sandbox saw it; unknown = may or may not have run (ack lost, transport, worker interrupted) |
| `tool.result.data` | `error_class` (iff `is_error`): `{origin: injected\|staged\|real, layer: boundary\|filesystem\|sandbox\|transport\|harness\|model\|gym, code, kind?, label, outcome_known, side_effect_applied?, detail?}` | provenance and layer; `label` is a fixed string rendered verbatim; `error_code` kept for compatibility; raw text stays in `output` |
| `tool.result.data` | `attempts`, `sandbox: {id, alive}` | harness-level retries of this call (1 = none); identity/liveness of the workspace that answered |
| `fault.fired.data` | `origin` (injected \| staged, never real), `layer` (boundary for injected, filesystem for staged), `description` | one plain sentence per fault |
| `episode.reset.data` | `sandbox_id` (short), `attempt` | workspace identity; >1 when re-provisioned |
| `episode.sandbox` (new) | `{sandbox_id, status: alive\|terminated\|replaced, reason, step}` | the environment noticed the worker changed |
| `interruption` (new) | `Interruption {step?, layer, code, label, tool_use_id?, tool?, path?, outcome_known, planned, resumed, worker_generation, at, detail?}` | always real; `planned: true` when a scenario `harness_faults` entry triggered it |
| `run.resumed` (new) | `{worker_generation, resumed_from_event_id, dangling_tool_use_id?, resumed_at}` | a fresh worker picked the run up |
| `run.finished.data` | `evaluation_status: ok\|failed\|skipped`, `evaluation_error?`, `steps`, `score?` | a run that could not be graded is `unevaluated`, never "ok" |
| `RunStatus` | `+ unevaluated`, `+ interrupted` | "ok" now implies a score |
| `RunRecord` | `error_class?`, `interruptions[]`, `worker_generation` | why the run ended as it did |
| `GET /scenarios` | `checks: [{id, description, weight}]`, `faults_public: [{kind, origin, layer, description}]`, `harness_faults: [{kind: worker_crash\|transport_abort, tool, path, nth, after_ms}]` | intended outcome and fault classes known before the run; the agent never sees them |
| codes (final) | `ENOENT EACCES ETIMEDOUT EINVAL ESANDBOX ETRANSPORT EHARNESS EMODEL EGYM EINTERNAL ENOEPISODE` | `ETIMEDOUT` is ack-lost *or* a real client timeout — origin/layer tell them apart |

Older records (e.g. `r_e9bc5c8c6739`) predate these fields; the UI resolves what it can from the
evaluate ledger (`outcome: ack_lost` ⇒ the write landed, `short_circuit` ⇒ nothing ran) and says
"unknown" otherwise.

## 6. Identity

- **Minting.** `src/lib/identity.ts`: read `localStorage["faultline.user_id"]`, else the `faultline_uid`
  cookie, else mint `"u_" + crypto.randomUUID()`; write back to both. Cookie attributes:
  `SameSite=Lax; Secure; Max-Age=31536000; Path=/`. Not `HttpOnly` (JS writes it). All storage access is
  wrapped in try/catch; a browser that blocks storage still works for the session with an in-memory id.
- **Transport.** The harness API lives on a different origin from the SPA, so cookies are never sent to
  it. The id travels as `X-Faultline-User` on every request; the harness CORS config allows that header.
- **Why both stores.** localStorage is the primary (no size/attribute quirks, not sent anywhere);
  the cookie only exists so the id survives a "clear site data → local storage" that leaves cookies.
- **Threat model.** This is scoping, not authentication (explicit non-goal). Ids are 122-bit random and
  never listed, so they are unguessable in practice, but anyone holding one can read that browser's
  transcripts. Nothing sensitive is stored: scenarios are bundled, the workspace is a fixture, and the
  provider key never leaves `run_episode`. There is no per-id rate limit on `POST …/runs` yet.

## 7. Web application

### 7.1 Stack

Vite 8 + React 19 + TypeScript 6, Tailwind v4 (`@tailwindcss/vite`), shadcn v4 CLI (style `base-nova`
on Base UI, `neutral` base colour, CSS variables, the `cn` helper package, lucide icons) for primitives,
Beautiful UI for the AI-native composites, vitest (jsdom). Path routes with no router dependency
(`src/lib/router.ts`, §7.3). **Node ≥ 22.12 is required** (Vite 8
`^20.19 || >=22.12`, vitest 5 `^22.12 || ^24`; `.nvmrc` pins 22 and the Modal image installs Node 22).
Built into `dist/` (in the Modal image, or prebuilt via `FAULTLINE_WEB_PREBUILT`) and served by a Modal
Server running `serve.py`: SPA fallback for extension-less paths, `no-store` on `index.html` and
`config.json`, immutable `/assets/`, unified JSON request logs. `serve.py` writes `/config.json` from
`$HARNESS_URL` at container start; the SPA resolves the harness URL as injected
`window.__FAULTLINE_CONFIG__` → `/config.json` → `VITE_HARNESS_URL` → default, so the URL can change
without a rebuild.

### 7.2 Beautiful UI: what it is and how it is integrated

`https://www.beautifului.dev` is an MIT-licensed gallery (© 2026 Shane Levine) of 21 React + Tailwind
components "for AI-native interfaces". It is **not** an npm package and **not** a shadcn registry: each
component has a *Copy code* button, and the TSX source is embedded in the page. Integration is therefore
copy-paste into `src/components/bui/` with a LICENSE file, plus three adaptations that the source makes
necessary:

| Adaptation | Reason |
|---|---|
| icons → `lucide-react` | `SidebarNav` imports twelve icons from `@central-icons-react/*`, a paid set |
| unpublished atoms → shadcn | components import `@/components/atoms/{Button,StreamText,Shimmer,ValuePill,EntityChip}` and `@/components/primitives/GlideMenu`, which the gallery does not publish; shadcn `Button`, `Skeleton`, `Badge`, `DropdownMenu` stand in |
| drop `glimm` | `PromptBar` imports a shader/sound helper; the branch is deleted |
| `accent*` classes → `brand*` | shadcn already owns `accent` / `accent-foreground` (its subtle hover surface); Beautiful UI uses `accent` as the brand colour. Renaming on copy keeps both palettes intact |

Components as ported (gallery name → ours): `SidebarNav` → the shadcn sidebar block in `app-sidebar.tsx`
(conversation rail), `PromptBar` → `Composer` (scenario `/` command, model and seed), `StreamingText` →
`AssistantText` (assistant prose), `ThinkingState` → `ThinkingTrace` (per-step trace), `ToolChips` →
`ToolCallChips` (output, exit code, duration, fault badge), `CodeBlock` (outputs and unified diffs),
`TaskRows` → `ScoreRows` (score card), `LoadingState`, `DiffTable` → `FileStatusTable` (workspace file
status). No animation library
is required; the components use CSS transitions and `useLayoutEffect`.

Theme bridge: Beautiful UI is written against its own Tailwind tokens (`ink`, `ink-2`, `ink-3`, `canvas`,
`surface`, `line`, `line-strong`, and `accent`, `accent-tint`, `accent-ink` → renamed `brand*`). They are
defined once in the existing `@theme inline` block of `src/index.css` as aliases of shadcn's CSS
variables (`--foreground`, `--muted-foreground`, `--background`, `--card`, `--border`, `--input`,
`--primary`, `--primary-foreground`), so both libraries follow the same light/dark palette (exact block
in `PLAN.md` §2.10.2).

### 7.3 Layout, routes, state

```
┌──────────────┬──────────────────────────────────────────┬──────────────────────┐
│app-sidebar   │top bar: breadcrumb · status chips        │Workspace (resizable) │
│ + New run    │         step n/max · tokens · elapsed    │ Files (FileStatus…)  │
│ conversations│──────────────────────────────────────────│ Diffs (CodeBlock)    │
│  · lost-ack  │[user] task prompt                        │ Timeline             │
│  · locked…   │[assistant] AssistantText                 │ Logs (virtualized)   │
│ replays      │   ThinkingTrace ▸ ToolCallChips          │                      │
│  · gauntlet  │     (CodeBlock) · fault badge            │                      │
│              │[assistant] summary + ScoreRows           │                      │
│              │──────────────────────────────────────────│                      │
│ user menu:   │Composer  /scenario  model ▾  seed  Run   │                      │
│ id · theme   │                                          │                      │
└──────────────┴──────────────────────────────────────────┴──────────────────────┘
```

- Landing (`/`): five bullets explaining the app, a centered composer (`/` picks a scenario, model and
  seed chips), and below it a wide shadcn data table of scenarios (TanStack Table v9, fixed layout)
  with separate columns for title, description, max turns, failure badges, Live run and Replay;
  every injected-fault scenario ships a real recorded replay; harness reachability is a composer hint
  and a line in the identity menu, not a card.
- Routes (path based, `src/lib/router.ts`, no router dependency): `/` = the landing page;
  `/conversations/:id` = a persisted transcript, `?run=<run_id>` focuses one run (live tail if
  unfinished); `/runs/:id` = a run outside a conversation; `/replay/:demoId` = a bundled recorded run
  with a scrubber. `serve.py` answers extension-less paths with `index.html`, so deep links survive a
  refresh.
- One pure reducer (`src/lib/reducer.ts`, already present) builds the view model for live SSE, for
  `GET /runs/{id}` restore, and for replay of `public/demo/*.json`. Persisted transcripts from
  `GET /conversations/{id}` are mapped to the same view model, so every path renders through the same
  components.
- `src/lib/api.ts` (already present) reconnects SSE with `Last-Event-ID` and, after two failed opens,
  falls back to polling `GET /runs/{id}`; identity is added to it as a default header.
- Theme: light / dark / system, chosen from a radio group of icon buttons in the user menu (bottom-left
  sidebar footer) and stored
  per browser (`faultline.theme`); "system" follows `prefers-color-scheme` live; a one-off
  `?theme=light|dark|system` in the URL sets and persists the choice (for sharing a link in a given
  look, and for screenshot tooling). The choice is applied
  by toggling the `dark` class on `<html>` (shadcn's dark variant) both from an inline pre-paint script
  in `index.html` and from `src/lib/theme.ts`, so the first paint is already right. Semantic colour
  tones are written as light/dark pairs so status badges keep contrast on both backgrounds.
- Workspace panel: shadcn Resizable panels (`react-resizable-panels` 4.x) on ≥1280 px — draggable
  handle, collapsible to zero, split persisted in localStorage, the top-bar toggle bound to the
  panel's imperative handle; an overlay sheet below that width. The Logs tab virtualizes its rows
  (`@tanstack/react-virtual`, rows measured after render because lines wrap) and follows the tail
  while a run is live.
- Unified logging mirrors the server's JSON shape to the console with `svc:"web"`.

### 7.4 Verification of the product flows

`.claude/skills/verify-faultline` covers the browser: `features/web-ui.md` is the maintained list of
user flows (landing, composer, live run → conversation, persisted transcript, replays,
workspace, identity, theme, failure modes, sidebar); `apps/web/e2e/flows.spec.ts` is the
executable form (Playwright in the installed Google Chrome, one test per flow, a fresh browser
context per test); `helpers/verify_web.py` runs an HTTP doctor of the deployed site and then the
flows, writing `runs/<ts>_verify_web/` in the same layout as the backend runner (commands log, raw
outputs, screenshots, scores for a live run, `verification.json` with the browser-started
`run_id`, `manifest.json`). The rule that keeps it current: a change to a flow in `apps/web`
changes its map entry and its test in the same commit.

## 8. Deployment and configuration

| Item | Value |
|---|---|
| Workspace / environment | `appliedlabsai` / `local` → web functions at `https://appliedlabsai-local--<app>-<fn>.modal.run`; the static site is a Modal **Server**, whose URL is `https://appliedlabsai-local--faultline-web-site.us-east.modal.direct` (read it back with `modal.Server.from_name("faultline-web", "site").get_url()`; the `.modal.run` shape answers 404 for Servers) |
| Deploy order | sandbox-env → harness (creates Volume `faultline-db` on first deploy) → web. Live URLs (2026-09-12): harness `https://appliedlabsai-local--faultline-harness-api.modal.run`, sandbox-env `https://appliedlabsai-local--faultline-sandbox-env-api.modal.run`, web `https://appliedlabsai-local--faultline-web-site.us-east.modal.direct` |
| Secret | `anthropic-secret` (`ANTHROPIC_API_KEY`, optional `ANTHROPIC_WORKSPACE`; name overridable via `FAULTLINE_ANTHROPIC_SECRET`), attached only to `run_episode` |
| Volume | `faultline-db` (v1), mounted at `/data` on `Store` only |
| Env | `ANTHROPIC_MODEL` (default `claude-haiku-4-5`), `SANDBOX_ENV_URL`, `HARNESS_URL`, `LOG_LEVEL`, `MODAL_ENVIRONMENT`, `FAULTLINE_ANTHROPIC_SECRET`, `HARNESS_HAIKU_THINKING` (opt-in), `FAULTLINE_WEB_PREBUILT` (see `.env.example`) |
| Warm containers | web Server, harness `api`, harness `Store` (`min_containers=1`) |
| Web image | Node 22 + pnpm; `pnpm i --frozen-lockfile && pnpm build`; `config.json` written from `HARNESS_URL` at container start |
| Sandbox guardrails | `timeout=1800`, `idle_timeout=600`, `block_network=True`, delete in `finally`, `reap` helper |
| Export | `modal run … ::checkpoint_now` then `modal volume get faultline-db faultline.sqlite3 runs/` |

## 9. Decision log

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| 1 | Three Modal apps with a strict secret boundary | one app | harness swappable, environment gradeable without trusting the agent, static UI; the provider key exists in exactly one function |
| 2 | Faults injected at the tool boundary, not inside the shell | `chmod`, deleting files live, network faults | deterministic, seedable, invisible to the agent, gradeable from a ledger; ack-lost is only expressible at the boundary |
| 3 | `spawn` + event log + reconnecting SSE | run the loop inside the request | Modal web requests cap at 150 s |
| 4 | Persistence = SQLite on a Modal Volume | Modal Dict only; hosted Postgres; Modal Dict as hot path + SQLite archive | user asked for it; zero external services; a single file is trivial to export as evidence |
| 5 | Single-writer `Store` actor (`max_containers=1`) | every function opens the DB on the Volume; per-run DB files | Volumes have no distributed locking and last-write-wins; one process owning the file removes every consistency question |
| 6 | Hot DB on local disk, `VACUUM INTO` snapshots renamed atomically onto the Volume | run SQLite directly on the FUSE mount | WAL/locking on FUSE is undocumented, and background commits are not multi-file snapshots; Modal's own example copies-then-commits |
| 7 | Volume v1 | v2 | v2 needs an explicit flag and is marked experimental in the 1.5.5 CLI; no benefit for one file |
| 8 | `events` as source of truth + `messages`/`blocks`/`llm_calls` projections | events only; projections only | replay/SSE need the log; the UI and "what did the model see" need the message shape; projections are a cheap same-transaction write and a listed scope cut |
| 9 | Identity via `X-Faultline-User` header, minted in the browser, mirrored to a cookie | cookie-only; server-minted id | the API is cross-origin so cookies never reach it; localStorage is the reliable store; the cookie is a backup |
| 10 | Beautiful UI by copy-paste with lucide/shadcn substitutions | Vercel AI Elements (a real registry); shadcn only | matches the requested look; MIT; the private deps are small and swappable; scope cut #4 if it drags |
| 11 | Haiku 4.5 default, Sonnet/Opus 5 allowlisted | Sonnet default | cost and latency for a demo; the ack-lost scenario is more interesting when a cheaper model sometimes fails it |
| 12 | Path routes through a small in-house router (`src/lib/router.ts`: `pushState` + `useSyncExternalStore`) | `react-router-dom`; query params only (the scaffold's first design) | four screens with deep links that survive a refresh (`serve.py` SPA fallback); no dependency in a static SPA |
| 13 | Node 22 pinned (`.nvmrc`, `engines`, Modal image) | stay on the machine default 21.6 | Vite 8 and vitest 5 refuse to start on 21.6 (`styleText` missing from `node:util`) |

## 10. Open questions

- Should the composer allow editing the task prompt, or only choosing a scenario? Current answer: editable,
  stored in `runs.task_prompt`, because it makes the "conversation" framing honest. Revisit if it invites
  prompt-injection-shaped demos that distract from the reliability story.
- Do we need per-user rate limiting beyond `POST …/runs`? Probably not for review traffic.
- Checkpoint cadence (15 s) vs. cost of `VACUUM INTO` as the DB grows — measure once real runs exist and
  record the number in `PLAN.md` §0.

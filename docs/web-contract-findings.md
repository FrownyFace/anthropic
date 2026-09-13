# apps/web ↔ faultline-harness — contract findings

> **Superseded (2026-09-12 19:45 EDT).** Written before the SQLite Store landed (it reports `/me` and `/conversations` as missing and the
> Modal Dict as the backend). Kept as part of the development record; do not
> build against it. Current sources: `docs/store-notes.md`, `ARCHITECTURE.md` §4–§5, `packages/common/faultline_common/schemas.py`.


Written 2026-09-12 22:2xZ by the integration agent that owns `services/agent-harness` and
`services/sandbox-env`. **Nothing under `apps/web` was read-write to me — I changed nothing there.**
This is the mismatch list between:

- the **live** harness `https://appliedlabsai-local--faultline-harness-api.modal.run` (curled, not assumed),
- `packages/common/faultline_common/schemas.py`,
- `ARCHITECTURE.md` §5.1 / §5.3 and `PLAN.md`,
- the `apps/web` sources on disk at the time of writing (`src/lib/{types,api,config,reducer}.ts`,
  `src/hooks/*`).

Fixes that belonged to the harness are **already applied and redeployed** (§1). Everything in §2 is
for the `apps/web` owner; §3 is "no action, just so you know".

Reproduce the whole check with:

```bash
services/agent-harness/.venv/bin/python scripts/web_check.py --label web
# → runs/<UTC ts>_web/{summary.json,checks.json,health.json,scenarios.json,sse_head.txt}
```

Last result: **22 passed, 0 failed, 2 skipped** (the 2 skips are `web.index` / `web.config_json` —
there is no `faultline-web` app deployed in Modal env `local` yet). Evidence:
`runs/20260912T222032Z_web/`.

---

## 0. What is verified working end to end (no action needed)

| The UI expects | Live harness | Verdict |
|---|---|---|
| `GET /health` → `Health` with `svc, ok, version, has_provider_key, model_default, sandbox_env_url, detail` | exactly that; `has_provider_key:false`, `detail.sandbox_env_reachable:true` | ✅ |
| `GET /scenarios` → array **or** `{scenarios:[…]}` | `{"scenarios":[…]}` — `normaliseScenarios()` unwraps it | ✅ |
| `Scenario{id,title,description,task_prompt,max_steps,fault_kinds}` | all present, 4 scenarios: `gauntlet`(30 steps, all 3 kinds), `locked-file`, `lost-ack`, `missing-config` | ✅ |
| `POST /runs {scenario_id,model?,seed?,max_steps?}` → `{run_id}` | returns `{run_id,status,scenario_id,model,max_steps,seed}`; 200, not 202 | ✅ |
| `GET /runs/{id}` → full `RunRecord` | all fields the reducer reads, plus `steps`/`summary`/`task_prompt` | ✅ |
| SSE `id:` = 0-based contiguous `Event.id` | verified over a 61-frame run | ✅ |
| SSE frames named per `EventType` | `event: run.started`, `event: tool.result`, … | ✅ |
| `event: done` with `{reason:"finished"|"window", status, last_event_id, run_id}` | exactly that | ✅ |
| **Resume via `?last_event_id=<n>` query param** (EventSource cannot set headers) | honoured — `?last_event_id=59` replayed only id 60. The `Last-Event-ID` header and a legacy `?after=` both work too | ✅ |
| CORS for a cross-origin `credentials:'omit'` fetch | `access-control-allow-origin: *` on `/health`, `/scenarios`, `/runs/*` and the SSE route | ✅ |
| **CORS preflight allowing `X-Faultline-User`** | `OPTIONS /runs` → 200, `access-control-allow-headers: content-type,x-faultline-user`, methods `GET,POST,PATCH,DELETE,…` | ✅ |
| Secret boundary | `/health` `has_provider_key:false`; the key exists only inside `run_episode` | ✅ |
| `apps/web/public/config.json` default URL | identical to the live harness URL, and to `DEFAULT_HARNESS_URL` in `src/lib/config.ts` | ✅ |

---

## 1. Fixed on the harness side (done — redeploy already live)

### 1.1 `llm.call` was never emitted — live token counts sat at zero

`src/lib/reducer.ts` sums live usage from `llm.call`
(`if (!isTerminal(state.status)) next.usage = sumLlmUsage(llmCalls)`), and `llm.call` is declared in
`schemas.EventType` and `ARCHITECTURE.md` §5.1 — but `harness/loop.py` only wrote a `model.call`
*log* line. Consequence: the header's token counter would read 0/0 for the whole run and only jump
to the real number at `run.finished`, and `state.llmCalls` (per-turn model/stop-reason/latency) was
always empty.

**Fixed** in `services/agent-harness/harness/loop.py::create_message`: one `llm.call` event per
`messages.create`, **including failed attempts** (attempt 1 = the 429, attempt 2 = the retry that
worked), which is what the `attempt` field is for. Shape, as ARCHITECTURE §5.1 specifies:

```json
{"attempt":1,"model":"claude-haiku-4-5","stop_reason":"tool_use",
 "request_id":"req_011CezKdwpFapsFvmy2zEj2Z",
 "usage":{"input_tokens":1947,"output_tokens":170,"cache_read_input_tokens":0},
 "duration_ms":1593}
```

plus `"error": "<Type>: <msg>"` on a failed attempt (usage zeroed). `Event.step` is the harness step.
`sumLlmUsage()`'s "highest attempt per step wins" rule means the failed attempts never double-count —
no change needed on your side. Live proof: `runs/20260912T221949Z_r_0918b04a0dea/run.json`, 9
`llm.call` events, real `request_id`s.

> Note: the payload carries `cache_read_input_tokens` but **not** `cache_creation_input_tokens`
> (`usage_from()` does not read it). `asUsage()` already tolerates the absence.

### 1.2 `turn.thinking` was never emitted

Same story: declared in `schemas.EventType`, consumed by `reducer.ts` (`StepView.thinking`) and by
`transcript.ts`, emitted nowhere — thinking blocks became a `debug` log line only.

**Fixed**: `harness/loop.py` now emits `turn.thinking {text}` for each non-empty `thinking` block, at
the step that produced it. `redacted_thinking` stays a log line (no readable text). The text is
capped at 8 000 chars (`THINKING_CAP`) with `truncate()`'s explicit `…[truncated N chars]` marker,
because a whole run record lives in one `modal.Dict` value.

**This changes nothing on the default demo path**: `claude-haiku-4-5` is sent no `thinking` param at
all (`config.thinking_for`), so you will only see these events on `claude-sonnet-5` / `claude-opus-5`.

Harness tests: `109 passed` (`cd services/agent-harness && .venv/bin/python -m pytest`), including 5
new ones in `tests/test_loop.py` covering both events, and the canonical event-sequence test updated
to include `llm.call`.

---

## 2. For the `apps/web` owner — mismatches on your side

### 2.1 (cosmetic-but-fix-it) `fault.fired` attaches by the wrong `step`

`reducer.ts` case `'fault.fired'` matches the timeline step with `s.step !== fault.step`, i.e. the
`step` **inside the `FaultFired` payload**. That number is sandbox-env's *ledger* step (it counts
MCP tool calls), not the harness step the timeline is keyed by. They routinely differ — from a real
run:

```json
{"id":24,"step":4,"type":"fault.fired",
 "data":{"step":6,"kind":"ack_lost","path":"CHANGELOG.md","mode":"transient"}}
```

Event step 4, ledger step 6. So the `fault.fired` branch attaches to nothing.

**Why nothing looks broken today:** the harness also echoes the same `FaultFired` on the
`tool.result` (`data.fault`), and the `'tool.result'` branch attaches it by `tool_use_id`, which is
exact. The `faults` list is also correct (the `fault.fired` branch still appends it). So today the
badge shows up via `tool.result` and `fault.fired` is a silent no-op.

**Why fix it anyway:** when `observe()` is briefly unavailable the harness falls back to *inferring*
the fault from the error code, and a `fault.fired` can then arrive without a matching
`tool.result.fault` — in that case the badge would be lost.

**Fix (apps/web):** in the `'fault.fired'` branch, key the attach off the **event's** `step`
(the `step` variable already in scope from `Event.step`), not `fault.step`; keep `fault.step` only
for the `faults`-list dedupe key. One-line change:

```ts
// reducer.ts, case 'fault.fired'
if (s.step !== step) return s        // was: if (s.step !== fault.step) return s
```

I did **not** change this — `data.step` is `FaultFired.step` per `schemas.py` and ARCHITECTURE §5.3,
so the payload is right and the consumer is wrong. (If you would rather the harness renumbered the
fault to its own step, say so and I will — but it would then disagree with the graded ledger.)

### 2.2 `/me` and all `/conversations*` routes are 404 on the live harness

Confirmed by curl, every one of them:

```
GET    /me                       404      POST   /conversations            404
GET    /conversations            404      GET    /conversations/{id}       404
PATCH  /conversations/{id}       404      DELETE /conversations/{id}       404
POST   /conversations/{id}/runs  404
```

The SQLite `Store` of `ARCHITECTURE.md` §4 is **not implemented** — `harness/store.py` is still
`RunStore` over the `modal.Dict` `faultline-runs`, there is no `harness/migrations/`, no Volume, no
`Store` class. `RunRecord` on the wire therefore has **no `conversation_id` and no `user_id`**, and
`POST /runs` returns no `conversation_id`.

**No action needed from you — your degradation path already works and is the right one:**
`useConversations` maps 404 → `available:false` (rail hidden), `useStartRun` catches `isNotFound` on
`POST /conversations` and falls through to `POST /runs` → `?run=<id>`, which is fully functional
end to end (verified: a live run reached score 100).

Two things to be careful of while the Store is missing:

1. **`?c=<conversation_id>` is unreachable and `ConversationPage` will only ever render its error
   state.** Make sure nothing links to it when `conversations.available === false` (`useStartRun`
   already doesn't).
2. **Do not treat `/health` `ok:true` as "history works".** They are independent.

I deliberately did not build the Store: it is a large, separate piece of work (single-writer Modal
class, Volume, migrations, event→message projection), it is owned by the ARCHITECTURE §4 workstream,
and this pass was scoped to contract checking. It is the single biggest remaining gap between
ARCHITECTURE.md and the deployed system.

### 2.3 `RunSummary` disagrees with itself across the three sources

Three shapes exist for "a run in a list":

| Source | Key for the run id | Extra |
|---|---|---|
| `schemas.py::RunSummary` and `apps/web/src/lib/types.ts::RunSummary` | **`id`** | `score`, `finished_at` |
| `ARCHITECTURE.md` §5.1 wire block | **`run_id`** | `events` (count), `score` |
| live `GET /runs` (harness, `store.list_runs`) | **`run_id`** | `events` (count), `score` |

Nothing in `apps/web` calls `GET /runs` today (`HarnessClient` has no `listRuns`), and
`ConversationSummary.last_run` is unreachable while §2.2 stands, so **this bites nobody right now**.
When the Store lands, one of the two spellings has to win. My recommendation: make the harness emit
**`id`**, matching `schemas.py`/`types.ts` (the pydantic model is the contract; ARCHITECTURE's prose
block is the outlier). I left `GET /runs` alone rather than change a shape while it has no consumer —
flag it if you want `id` added as an alias now and I will add it additively.

### 2.4 `public/demo/lost-ack.json` predates `llm.call` / `turn.thinking`

The bundled replay has no `llm.call` events, so in demo mode the token counter stays at whatever
`run.finished` reports and `state.llmCalls` is empty — a slightly poorer demo than a live run now
gives. Not a break (`demo.test.ts` still passes; the reducer treats both as optional).

Two real runs with `llm.call` now exist and can be exported over it with the script that already
exists:

```bash
# takes the run DIRECTORY (it reads run.json inside it); add --force to overwrite the current demo
python scripts/export_demo.py runs/20260912T221949Z_r_0918b04a0dea --check   # lost-ack, score 100
```

Your call — the hand-written one is contract-faithful and its story is tighter.

> `scripts/export_demo.py` had a hardcoded `EVENT_TYPES` set that predated `llm.call` /
> `turn.thinking`, so it rejected every real run as soon as the harness started emitting them
> (`event[31].type 'llm.call' is not a known EventType`). I added the two types to that set —
> that file is in `scripts/`, not `apps/web/`. `--check` on the run above now exits 0 and reports
> `score 100.0, passed true`. **I did not write over `apps/web/public/demo/lost-ack.json`.**

---

## 3. Deliberate, documented, no action

- **`GET /scenarios` returns `{scenarios:[…]}`, not a bare array** (PLAN §2.3 says the array).
  `normaliseScenarios()` already accepts both; the envelope is the deployed reality. Same for
  `GET /runs` → `{runs:[…]}`.
- **The SSE request carries no identity** (`EventSource` cannot set headers) and the harness enforces
  no ownership on it. Intended: run ids are unguessable and ownership is checked on the JSON routes
  (once §2.2 exists).
- **`tool.result` is not `is_error` when a command merely exits non-zero.** Check `exit_code` inside
  the parsed `output`. Only a transport/tool failure sets `is_error` (with a `ToolError` JSON body
  carrying `code`).
- **`tool.result.data` carries extra keys** (`error_code`, `summary`, `mutating`) and `tool.call`
  carries `mutating`; `run.started` carries `sandbox_env_url`; `episode.reset` carries `scenario`
  and `workspace_root`; `run.finished` carries `steps` and `score`. All additive inside the
  free-form `Event.data`, all safe to ignore.
- **`RunRecord` carries `steps`, `summary`, `task_prompt`** (now declared in `schemas.py`, mirrored
  in `types.ts`). `conversation_id` / `user_id` stay absent until the Store lands.
- **`anthropic_workspace` is masked** (`"wrkspc…"`) in `run.started`. Render it as-is; it is not a
  secret and is already truncated server-side.
- **`POST /runs` returns 200, not 202**, and does **not** 502 when sandbox-env is unreachable — it
  accepts the run and fails it loudly at reset with `status:"error"`, so the failure is a real run
  record rather than a dead button.
- **No `faultline-web` Modal app exists yet.** `scripts/web_check.py` reports `web.index` /
  `web.config_json` as `SKIP`, not `FAIL`. When you deploy, re-run it — it will then also assert that
  the site's `/config.json` `harnessUrl` points at the harness it just tested
  (`web.config_points_at_harness`).
- **Harness URL default is correct**: `src/lib/config.ts` `DEFAULT_HARNESS_URL` and
  `public/config.json` both equal the live URL, so the SPA works even if `serve.py` fails to rewrite
  `/config.json` at container start.

---

## 4. One operational trap worth repeating

`modal deploy` propagation is **not** instant, and `add_local_python_source(..., copy=False)` mounts
at container start. A live run fired ~8 s after `modal deploy` returned still executed the **old**
`loop.py` and emitted no `llm.call`; the same run 60 s later emitted all 9. If you deploy and
immediately assert, you will chase a ghost. Wait, then assert.

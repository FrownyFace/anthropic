# `faultline-harness` — what the DEPLOYED service actually emits

> **Superseded (2026-09-12 19:45 EDT).** Measured 22:12–22:33 UTC, before the SQLite Store: §3 (`/me`, `/conversations*` → 404), §4 (three terminal
> statuses), §6 ("Store does not exist") and the missing `cache_creation_input_tokens` are no longer true. Kept as part of the development record; do not
> build against it. Current sources: `docs/store-notes.md`, `ARCHITECTURE.md` §4–§5, `schemas.py`; re-measure with `services/agent-harness/tools/check_contract.py`.


Audience: the `apps/web` owner. Everything below was **measured against the live deployment**, not
read off the source. Nothing here is aspirational; where the deployed reality differs from
`PLAN.md` / `ARCHITECTURE.md` / `schemas.py`, the deployed reality is stated and the divergence is
called out.

- Base URL: `https://appliedlabsai-local--faultline-harness-api.modal.run`
- Measured: 2026-09-12 22:12–22:33 UTC, Modal env `local`, build deployed at 22:29 UTC.
- Evidence: `runs/20260912T221236Z_harness_integration/` — `web_contract.json` (23/23 checks),
  `event_shapes.json` (per-type data keys over 3 real runs), `sse_window.json`, `health2.json`,
  `runs_list.json`, `app_logs2.txt`.
- Reproduce: `services/agent-harness/.venv/bin/python services/agent-harness/tools/check_contract.py <finished run_id> <out.json>`
- Companion doc: `docs/web-contract-findings.md` (same system, written by the other integration
  session — read both; they agree, this one is the emitted-shape reference).

---

## 1. Answers to the contract questions, verified live

| Contract point | Verified result |
|---|---|
| SSE frame `id:` == `Event.id`, 0-based, contiguous | ✅ 151-frame gauntlet run: frame ids `0..150`, exactly equal to `Event.id`, no gaps |
| `event: done` when the run reaches `ok` / `error` / `truncated` | ✅ `{"reason":"finished","status":"ok","last_event_id":150,"run_id":"r_…"}` |
| `event: done` with `reason:"window"` on the 110 s self-close | ✅ measured at **110.5 s** on a still-running run: `{"reason":"window","status":"running","last_event_id":1,…}`, then a resume delivered only the new event and closed `reason:"finished"` |
| `?last_event_id=<n>` honoured | ✅ (**was missing — added in this pass**, see §5) |
| `?after=<n>` honoured | ✅ |
| `Last-Event-ID` header honoured | ✅ |
| `tool.result.data.output` is a JSON **string** for structured tools | ✅ 29/29 structured results parsed as JSON on the gauntlet run |
| `fault.fired` data is the `FaultFired` object at top level | ✅ `{"step","kind","path","mode"}` directly in `Event.data` |
| `POST /runs` returns `{run_id}` | ✅ (`{run_id, status, scenario_id, model, max_steps, seed}`) |
| `conversation_id` on `POST /runs` | ❌ **not yet** — the SQLite Store of ARCHITECTURE §4 is not implemented; see §6 |
| `GET /scenarios` returns `{scenarios:[…]}` | ✅ 4 scenarios: `gauntlet`, `locked-file`, `lost-ack`, `missing-config` |

Precedence when more than one resume point is supplied:
**`Last-Event-ID` header → `?last_event_id=` → `?after=`**; an unparsable value falls through to the
next source, and none at all means "replay everything from id 0".

---

## 2. Event stream — exact `data` keys per type

Measured over three real runs (`lost-ack`, `gauntlet`, a truncated `lost-ack`), 151 + 61 + 34
events. `Event` itself is always `{id, ts, run_id, type, step, data}` and **every one of the 246
events validated against `faultline_common.schemas.Event`**.

| `type` | `Event.step` | `data` keys emitted |
|---|---|---|
| `run.started` | `null` | `scenario_id, model, seed, max_steps, anthropic_workspace, sandbox_env_url` |
| `episode.reset` | `null` | `episode_id, files, task_prompt, scenario, workspace_root` |
| `llm.call` | int | `attempt, model, stop_reason, request_id, usage{input_tokens,output_tokens,cache_read_input_tokens}, duration_ms` (+ `error` on a failed attempt) |
| `turn.text` | int | `text` |
| `turn.thinking` | int | `text` — **never seen on the default demo path**: `claude-haiku-4-5` is sent no `thinking` param, so this only appears on `claude-sonnet-5` / `claude-opus-5` |
| `tool.call` | int | `tool, input, tool_use_id, mutating` |
| `tool.result` | int | `tool_use_id, tool, output, is_error, duration_ms, error_code, summary` (+ `fault` when one fired) |
| `fault.fired` | int | `step, kind, path, mode` (the `FaultFired` object itself) |
| `workspace.diff` | int | `files, diffs` |
| `episode.evaluated` | `null` | `episode_id, score, passed, checks, tests, ledger` (a full `EvaluateResponse`) |
| `run.finished` | `null` | `status, usage{input_tokens,output_tokens}, duration_ms, steps, score` (+ `error`) |
| `log` | int or `null` | `svc, lvl, ev, msg` + free extras, each value capped at 500 chars |

Notes that matter when rendering:

- **`Event.step` is `null` on the five lifecycle events.** Anything keyed by step must tolerate it.
- **`usage` on `llm.call` has no `cache_creation_input_tokens`** — only `input_tokens`,
  `output_tokens`, `cache_read_input_tokens`. `schemas.Usage` defaults the missing one to 0.
- `log` events are a large share of the stream (44 of 246 here). They are UI-optional.
- `episode.evaluated.data` carries the **whole ledger** — this is the only place the ledger is
  released, and it is the ground truth behind the score card.

### 2.1 The one real trap: two different `step` counters

```json
{"id":24,"step":4,"type":"fault.fired",
 "data":{"step":6,"kind":"ack_lost","path":"CHANGELOG.md","mode":"transient"}}
```

`Event.step` (4) is the **harness loop turn**. `data.step` (6) is **sandbox-env's ledger index**,
which counts MCP tool calls. They routinely differ — on the gauntlet run the three faults were
`event.step 4 / data.step 9`, `8 / 13`, `19 / 25`.

Attach a fault to a timeline step by **`Event.step`**; use `data.step` only as part of a dedupe key.
This is the same finding as `docs/web-contract-findings.md` §2.1. The harness deliberately does not
renumber `FaultFired.step`: that field is the ledger index the grader scores against, and rewriting
it would make the UI disagree with `evaluate`'s ledger.

Belt and braces: the same `FaultFired` is also echoed on the causing `tool.result` as `data.fault`,
keyed by `tool_use_id`, which is exact. Prefer that path; `fault.fired` is the fallback for the case
where `observe()` was briefly unavailable and the harness had to infer the fault (such an object
carries `"inferred": true`).

---

## 3. REST surface as deployed

| Route | Response |
|---|---|
| `GET /health` | `{svc:"harness", ok:true, version:"0.1.0", has_provider_key:false, model_default:"claude-haiku-4-5", sandbox_env_url:"…", detail:{sandbox_env:{svc,ok,version}, sandbox_env_reachable:true}}` |
| `GET /scenarios` | `{scenarios:[Scenario]}` — proxied from sandbox-env; `502` with `{error, detail, sandbox_env_url}` if the gym is down |
| `POST /runs` | **200** (not 202) `{run_id, status:"queued", scenario_id, model, max_steps, seed}`. `400` on an unknown `scenario_id` (body lists the `known` ids) and on a model outside `MODEL_ALLOWLIST` (body lists `allowed`). Does **not** 502 when the gym is unreachable — the run is accepted and fails loudly at reset with `status:"error"` |
| `GET /runs?limit=n` | `{runs:[…]}`, newest first. Each row: `id`, `run_id`, `status`, `scenario_id`, `model`, `created_at`, `finished_at`, `events` (count), `score`. **`id` and `run_id` are both present and identical** (added in this pass, §5) so a row validates as `schemas.RunSummary` while existing `run_id` consumers keep working |
| `GET /runs/{id}` | the whole `RunRecord`: `run_id, status, scenario_id, model, seed, max_steps, episode_id, created_at, finished_at, events, evaluation, usage, error, task_prompt, steps, summary`. Validates against `schemas.RunRecord`. `404` `{error}` for an unknown id |
| `GET /runs/{id}/events` | `text/event-stream`; `404` JSON for an unknown run |
| `/me`, `/conversations*` | **404 — not implemented**, see §6 |

CORS: `access-control-allow-origin: *` on every route including the SSE one; the `OPTIONS /runs`
preflight returns 200 with `access-control-allow-headers: content-type,x-faultline-user`. The SSE
request carries no identity (an `EventSource` cannot set headers) and no ownership is enforced on it.

Response header `X-Request-Id` is echoed (or minted) on every request and threads into the harness
log lines; send your own to tie a UI action to its server logs.

---

## 4. SSE wire format, byte for byte

```
retry: 1000

id: 0
event: run.started
data: {"id":0,"ts":"2026-09-12T22:20:29.8Z","run_id":"r_…","type":"run.started","step":null,"data":{…}}

: keepalive

event: done
data: {"reason":"finished","status":"ok","last_event_id":150,"run_id":"r_…"}
```

- `retry: 1000` is the first thing on every stream.
- Every data frame is named with `event: <Event.type>` **and** carries the whole `Event` as its
  `data` — so a client may subscribe to named events or to `message`; both work.
- `: keepalive` comments every 10 s (10 of them observed inside one 110 s window).
- The `done` frame has **no `id:`**, so it never advances `Last-Event-ID`.
- **`reason:"window"` is not "finished".** Reconnect with the `last_event_id` from that payload.
  Measured window: 110.5 s (Modal hard-kills a web request at 150 s).
- Terminal statuses are `ok`, `error`, `truncated`.

Resume semantics verified live: after a `window` close at `last_event_id: 1`, an event `id: 2` that
arrived *while the client was reconnecting* was delivered on the next segment, and only that one.

---

## 5. What changed in this integration pass

1. **`?last_event_id=<n>` is now honoured** (`harness/api.py`). It was not before: only the
   `Last-Event-ID` header and `?after=` worked, and `apps/web` sends the query param because
   `EventSource` cannot set headers. Precedence documented in §1. Tests:
   `tests/test_api.py::test_sse_resumes_from_last_event_id_query` and
   `::test_parse_last_event_id_query_precedence`.
2. **`GET /runs` rows now carry `id` alongside `run_id`** (`harness/store.py`), so a row validates as
   `schemas.RunSummary` (which keys on `id`, as does `apps/web/src/lib/types.ts`) without breaking
   the `run_id` spelling in ARCHITECTURE §5.1 and today's consumers. Purely additive. Test:
   `tests/test_store.py::test_list_runs_rows_carry_both_id_spellings_and_validate_as_run_summary`.
3. `scripts/run_episode_cli.py` now reports each 110 s window close on stderr and records them in
   `summary.txt` as `sse_windows`, so the reconnect path leaves evidence.
4. `llm.call` and `turn.thinking` emission landed in the same window from the other integration
   session; both are live and covered above.

---

## 6. Not implemented — do not build against it yet

The SQLite `Store` of `ARCHITECTURE.md` §4 / `PLAN.md` §2.9 **does not exist on the deployed
harness**. `harness/store.py` is still `RunStore` over the `modal.Dict` `faultline-runs`: one whole
`RunRecord` per run, re-put on every event. Consequences, all confirmed by curl:

- `GET /me`, `GET|POST /conversations`, `GET|PATCH|DELETE /conversations/{id}`,
  `POST /conversations/{id}/runs` → **404**.
- `RunRecord` has **no `conversation_id` and no `user_id`**; `POST /runs` returns no
  `conversation_id`.
- `X-Faultline-User` is accepted by CORS but **ignored**; `GET /runs` is not scoped to a user.
- Run history survives a `modal deploy` (the Dict is durable) but there is no transcript projection,
  no volume snapshot and no `GET /conversations/{id}` rehydration.

The degradation path in `apps/web` (404 → `conversations.available = false`, rail hidden,
`POST /conversations` falling through to `POST /runs` → `?run=<id>`) is the correct behaviour and is
fully functional: live runs reach score 100 through it.

Also absent: `modal.Dict` `get`/`put` is read-modify-write, so a single episode's tool calls must not
be fanned out in parallel (the harness does not).

---

## 7. Live results behind this document

| Scenario | run_id | status | score | steps | duration | fault(s) fired |
|---|---|---|---|---|---|---|
| `lost-ack` | `r_63f6ef825868` | ok | **100.0** (3/3) | 8 | 40.0 s | `ack_lost` on `CHANGELOG.md` |
| `missing-config` | `r_83432ff65645` | ok | **100.0** (3/3) | 11 | 55.8 s | `missing_file` transient on `README.md` |
| `locked-file` | `r_d43c638edb1e` | ok | **100.0** (3/3) | 12 | 50.1 s | `denied_write` ×2 on `src/ratelimiter/limits.py` |
| `gauntlet` | `r_f584a2914e89` | ok | **100.0** (5/5) | 24 | 106.4 s | `denied_write` ×2, `ack_lost` |
| `lost-ack --max-steps 3` | `r_aad3e41fad52` | **truncated** | **8.0** (1/3) | 3 | 17.9 s | none (budget ran out first) |
| `lost-ack` (final build) | `r_4575851bc75f` | ok | **100.0** (3/3) | 8 | 46.0 s | `ack_lost` on `CHANGELOG.md` |

All on `claude-haiku-4-5`, no prompt tuning. The truncated run is deliberate evidence that a failed
recovery renders sensibly (`status:"truncated"`, partial checks, `passed:false`).

Secret boundary, read out of `modal app logs faultline-harness -e local`: `has_key=true` appears on
exactly one event, `run_episode.start`; `api.boot` reports `has_key=false`; no key material matches
`sk-ant-…` anywhere in the logs; `ANTHROPIC_WORKSPACE` is surfaced only as `"wrkspc…"`.

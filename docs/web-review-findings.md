# apps/web — cross-review findings (Phase 3)

**For the `apps/web` owner. Nothing under `apps/web` was edited, built or deployed by this review.**
Every finding below is `fixed=false` and carries the exact file:line plus the change I would make.

- Reviewed: the tree as of 2026-09-13 ~01:00 UTC (`StoryBar.tsx`, `RunLayout.tsx`, `index.css` were
  uncommitted/in-flight while I read; I reviewed what was on disk).
- Reviewed **against**: `PLAN.md` §2–§4, `packages/common/faultline_common/schemas.py`,
  `docs/error-taxonomy.md`, `services/sandbox-env/{FAULTS.md,GRADING.md}`,
  `services/agent-harness/PROVENANCE.md`, `docs/store-notes.md`, and — this is the important part —
  **the live deployment**, not the docs:
  `https://appliedlabsai-local--faultline-harness-api.modal.run`.
- Live evidence used below: runs `r_2991dd9a680a` (worker-crash, ok 100, worker_generation 2),
  `r_9606e7fe615f` (lost-ack, ok 100), `r_77368c6c998d` (sandbox-loss, **interrupted**, score null),
  plus `GET /scenarios` (5 scenarios) and `GET /conversations/{id}` for each of those runs.
- Test suite run read-only at review time: `npx vitest run` (node v24.13.1) → **187 passed, 0 failed**,
  22 files. I changed nothing, so that is both "before" and "after".

No harness-side bug was found that needed fixing: in every mismatch below the producer matches
`schemas.py` / the taxonomy and the consumer is the side that is wrong, or the gap can only be
closed in the browser (`schemas.Block` has no `error_class`, and `packages/common` is off-limits).
So `services/agent-harness` was **not** modified and **not** redeployed.

---

## 1. `fault.fired` is attached by the wrong step counter — major

**`src/lib/reducer.ts:779`**

```ts
const updated = state.steps.map((s) => {
  if (s.step !== fault.step) return s      // <-- fault.step is sandbox-env's LEDGER index
```

`FaultFired.step` is sandbox-env's **ledger index** (it counts MCP tool calls). `Event.step` is the
**harness loop turn**. They routinely differ and the harness deliberately does not renumber the
payload, because that field is what the grader scores against
(`docs/harness-contract.md` §2.1, `docs/web-contract-findings.md`). Measured live, this run:

```json
{"id":28,"step":4,"type":"fault.fired",
 "data":{"step":6,"kind":"ack_lost","path":"CHANGELOG.md","mode":"transient",
         "origin":"injected","layer":"boundary",
         "description":"the write was applied but the acknowledgement was withheld"}}
```

(`r_9606e7fe615f`; on `gauntlet` `r_ede9fbb98864` it is `Event.step 6 / data.step 11`,
`8 / 13`, `22 / 27`.)

Today this branch attaches **nothing**, because by the time `fault.fired` arrives the step named by
`data.step` does not exist yet — so the whole `fault.fired` case is effectively dead code that is
carried by the `tool.result.data.fault` echo above it (`reducer.ts:725,733`). It becomes a *wrong*
attachment, not merely a missing one, the moment a `fault.fired` is folded when the later step does
exist — i.e. any out-of-order or late delivery, and the documented fallback case where `observe()`
blipped and there is no `tool.result` echo to carry the fault.

**Fix (one line):**

```ts
case 'fault.fired': {
  const fault = asFault(data) ?? asFault(data.fault)
  if (!fault) return next
  const atStep = typeof ev.step === 'number' ? ev.step : fault.step   // harness turn, not ledger index
  ...
  const updated = state.steps.map((s) => {
    if (s.step !== atStep) return s
```

Keep `fault.step` as the dedupe key (`reducer.ts:763,773`) — that part is correct, it is the ledger
identity.

### 1b. The test that should have caught it does not — minor

**`src/lib/reducer.test.ts:186–200`** — `it('attaches a fault from tool.result.fault and from a fault.fired event')`
builds the fixture as `ev('fault.fired', { step: 2, … }, 2)`: `data.step === Event.step`. The
deployed harness never emits that. The test passes for the wrong reason and actively hides §1.
Change the fixture to `ev('fault.fired', { step: 7, kind: 'missing_file', path: 'README.md', … }, 2)`
and assert the badge lands on the step-2 call.

### 1c. Same mismatch in the story narrative — minor

**`src/lib/story.ts:195`**

```ts
faultsFired.find((f) => f.step === step && (!c.path || normalisePath(f.path) === c.path))
```

`step` here is the harness step, `f.step` the ledger index, so this fallback never matches. Since
`evs` is already the events *of that step*, drop the `f.step === step` clause entirely.

---

## 2. The persisted (SQLite) transcript silently downgrades "unknown" to "failed" — major

**`src/lib/transcript.ts:118–136` (`attachResult`) + `src/lib/callStatus.ts:35–50` (`effectiveOutcome`)**

`ConversationPage.tsx:131` switches to `transcriptFromMessages` as soon as a run is terminal, so the
**persisted projection is the default view of every finished run after a page load** — including the
flagship `worker-crash` demo.

`schemas.Block` carries `is_error`, `exit_code`, `duration_ms`, `fault`, `truncated` — and **no
`error_class`, no `outcome`, no `attempts`, no `sandbox`**. Verified live on `r_2991dd9a680a`:

```json
{"type":"tool_result","tool_name":"write_file","tool_use_id":"toolu_01FC3odo…",
 "text":"{\"error\": \"harness worker was interrupted while this call was in flight; the operation
          may or may not have completed — verify before retrying\", \"code\": \"EHARNESS\",
          \"path\": \"CHANGELOG.md\"}",
 "is_error":true,"exit_code":null,"duration_ms":0,"fault":null,"truncated":false}
```

`fault` is null (nothing was *injected* — the worker really died), so `effectiveOutcome` returns
null and `callStatus` falls through to `failed` at `callStatus.ts:95`. The UI renders the
interrupted write as a **red "error"** — exactly the "blame the agent for infrastructure" mislabel
`PLAN.md` §2.11 exists to eliminate. Same for the ESANDBOX read on `r_77368c6c998d`
(`{"code":"ESANDBOX","detail":"ConflictError: Modal Sandbox is shutting down."}`).

The live SSE / `GET /runs/{id}` path is correct; only the persisted path is wrong, so the same run
changes colour when you reload the page. The `lost-ack` case survives only by luck — its block does
carry `fault: {kind: "ack_lost", …}`.

**Fix — derive from the code the ToolError body already carries.** `parseToolResult` already lifts
it into `result.errorCode` (`reducer.ts:365–367`); it is just never consulted.

```ts
// callStatus.ts, at the end of effectiveOutcome(), before `return null`
// The persisted projection (schemas.Block) has no outcome/error_class; the ToolError body's
// `code` is the only structured signal left, and these codes are never injected
// (docs/error-taxonomy.md "Real-failure codes").
switch (result.errorCode) {
  case 'ETIMEDOUT':
  case 'ETRANSPORT':
  case 'EHARNESS':
    return 'unknown'
  case 'ESANDBOX':
  case 'EINVAL':
  case 'EINTERNAL':
  case 'ENOEPISODE':
    return 'not_executed'
  default:
    return null           // ENOENT / EACCES stay ambiguous → is_error decides
}
```

And, so the red/amber badge and the taxonomy label come back on that path, synthesise the
`ErrorClass` in `transcript.ts::attachResult` for the codes whose origin is unambiguous:

```ts
// transcript.ts, after `const result = parseToolResult(...)`
if (!result.errorClass && result.errorCode) {
  const layer = REAL_LAYER_FOR[result.errorCode]   // ESANDBOX→sandbox, ETRANSPORT→transport,
                                                   // EHARNESS→harness, EMODEL→model, EGYM→gym,
                                                   // EINTERNAL|ENOEPISODE→boundary
  if (layer) {
    result.errorClass = {
      origin: 'real', layer, code: result.errorCode,
      kind: null,
      label: taxonomyLabel('real', null, layer)!,       // lib/codes.ts already has the table
      outcome_known: layer !== 'transport' && layer !== 'harness',
      side_effect_applied: null,
      detail: null,
    }
  }
}
```

Do **not** guess for `ENOENT`/`EACCES`/`ETIMEDOUT` without a `fault` — those three are genuinely
ambiguous between injected and real, and guessing would be the same class of lie in the other
direction.

---

## 3. Run-level provenance is parsed and never rendered — major

The reducer computes all of this and no component reads any of it:

| `ViewState` field | written at | read by |
|---|---|---|
| `errorClass` | `reducer.ts:819`, `:917` | **nothing** |
| `interruptions` | `reducer.ts:830`, `:918` | **nothing** |
| `resumes`, `workerGeneration` | `reducer.ts:844–845` | **nothing** |
| `sandboxEvents`, `sandboxAlive`, `sandboxId` | `reducer.ts:722–723,:867–869` | **nothing** |
| `evaluationStatus`, `evaluationError` | `reducer.ts:821–822` | **nothing** |
| `resetAttempt` | `reducer.ts:629` | **nothing** |
| `ToolResultView.attempts`, `.sandbox` | `reducer.ts:719–720` | **nothing** |

(Verified with a full-tree symbol scan; the only consumers of `errorClass` are the per-call ones in
`ToolCallChips.tsx:210` / `ThinkingTrace.tsx:198`.)

What that costs, concretely, on the live `sandbox-loss` run `r_77368c6c998d`:

```json
"status":"interrupted", "score":null,
"error_class":{"origin":"real","layer":"sandbox","code":"ESANDBOX",
               "label":"real: sandbox terminated or unavailable","outcome_known":true,
               "side_effect_applied":false,
               "detail":"sandbox-env reported the sandbox as unavailable"},
"interruptions":[{"step":1,"layer":"sandbox","code":"ESANDBOX","planned":false,"resumed":false, …}],
"run.finished.data":{"evaluation_status":"skipped","worker_generation":1,"interruptions":1, …}
```

The screen shows the word **`interrupted`** (`RunStatusStrip.tsx:58–65`) and a neutral callout
reading *"Not graded — No evaluation exists for this run."* (`TranscriptView.tsx:208–221`). The
reason — the one sentence the whole §2.11 workstream produced — never appears. Likewise
`r_2991dd9a680a` never tells the reader that the worker died and **worker 2** finished the job,
which is the entire point of the `worker-crash` scenario.

`run.finished.data.error_class` is emitted precisely "so a live SSE viewer can explain the ending
without a second fetch" (`PROVENANCE.md` §Run-level). Right now nothing spends it.

**Fix:**

1. Add `errorClass: ErrorClass | null` and `interruptions: Interruption[]` (and `workerGeneration`)
   to the `Transcript` interface (`src/lib/transcript.ts:32–45`), filled from the ViewState in
   `transcriptFromViewState` and from the overlay in `transcriptFromMessages`.
2. In `TranscriptView.tsx`, before the "Not graded" block, render a callout whenever
   `transcript.errorClass` exists:
   - tone `error` for `origin: 'real'`, `warn` otherwise;
   - title = `errorClass.label` **verbatim** (the taxonomy says render it verbatim);
   - body = `errorClass.detail`, plus `"verify before retrying"` when `outcome_known === false`,
     plus `evaluationError` when `evaluationStatus !== 'ok'`.
3. In `RunStatusStrip.tsx`, when `state.workerGeneration > 1` show a chip
   (`worker ${state.workerGeneration}`, tooltip from the matching `interruption.label` +
   `planned ? 'deliberate chaos trigger' : 'unplanned'`); when `state.interruptions.length > 0`
   show the count. Both are one-line additions next to the existing `Stat`s.
4. In `ToolCallChips.tsx:215`, show `attempts` when `> 1` and a "sandbox gone" marker when
   `result.sandbox?.alive === false` — two facts a reviewer otherwise has to open devtools for.
5. `buildStory` (`src/lib/story.ts:174–249`) only looks at `turn.text` / `tool.call` / `tool.result`
   / `fault.fired` inside each step. `interruption`, `run.resumed` and `episode.sandbox` are
   collected into `byStep` (`:155–162`) and then ignored, so the replay tour narrating a
   `worker-crash` run would walk straight past the worker dying and a new worker taking over —
   the single most interesting sentence in that run. Add, per step: for an `interruption`,
   *"The harness worker itself was interrupted here (`label`); the call had already been
   dispatched, so whether it landed is unknown."* (+ `planned ? ' This one was a deliberate chaos
   trigger.' : ''`), and for a `run.resumed`, *"A fresh worker (generation N) picked the run up from
   the persisted event log and carried on."*

---

## 4. `ledger.resolution` is never consumed, so `side_effect_applied` is never upgraded — major

**`src/lib/reducer.ts:873–878`** treats every `log` event as an opaque line.

After grading the harness emits exactly one extra append-only event
(`PROVENANCE.md` §Events) that resolves the unknowns. Live, on `r_2991dd9a680a`:

```json
{"type":"log","data":{"ev":"ledger.resolution","rows":1,"matched":1,
 "resolutions":[{"tool_use_id":"toolu_01FC3odoxKH7GqphhtcBJuoa","step":4,"ledger_step":6,
                 "tool":"write_file","path":"CHANGELOG.md","outcome":"ok","interrupted":true,
                 "side_effect_applied":true}]}}
```

`callStatus.ts:65–71` is already written to say *"The ledger later confirmed it had landed."* — but
it only reads `errorClass.side_effect_applied`, which for `EHARNESS` is `null` at emission time and
is **only** ever resolved by this event. So the branch never fires on the one scenario it was
written for. (`side_effect_applied` appears nowhere outside `types.ts`, `reducer.ts`, `callStatus.ts`
and two tests; `ledger.resolution` appears nowhere at all.)

**Fix:** in the `log` case, before buffering, special-case `ev === 'ledger.resolution'` and fold
`data.resolutions` back onto the calls by `tool_use_id`, setting
`result.errorClass.side_effect_applied` (creating a minimal `errorClass` if the call has none) and,
when `outcome === 'ok'`, leaving the call's status as `unknown` but with the resolution text. The
stream stays append-only — you are folding a later event, not rewriting an earlier one.

---

## 5. `Scenario.faults_public` is ignored; staged faults are labelled "simulated" — major

**`src/components/ScenarioTable.tsx:105–138`** renders `s.fault_kinds` through `FaultKindBadge`,
which is hard-coded to `ORIGIN_TONE.injected` (`FaultBadge.tsx:33`). `s.faults_public` is never read
anywhere in the app (full-tree scan: zero hits).

Live `GET /scenarios` publishes exactly the distinction the card is flattening:

```json
"missing-config".faults_public = [
  {"kind":"missing_file","origin":"staged","layer":"filesystem",
   "description":"the file was deleted when the episode was created; this ENOENT is a real OS error"},
  {"kind":"missing_file","origin":"injected","layer":"boundary",
   "description":"the read was refused before reaching the sandbox; the file is still on disk"}]
"gauntlet".faults_public   = [staged missing_file, injected denied_write, injected ack_lost]
"worker-crash".faults_public = [{"kind":"worker_crash","origin":"real","layer":"harness", …}]
```

but `fault_kinds` is `["missing_file"]` / `["missing_file","denied_write","ack_lost"]` / `[]`. So the
`missing-config` card shows **one** amber "simulated" badge for two different stories, one of which
is a real deletion. The sandbox-env owner asked for this explicitly: *"render them as separate
badges ('staged' and 'simulated'), not deduped by kind."*

`worker-crash` happens to come out right only because `harness_faults` is rendered separately
(`ScenarioTable.tsx:117–131`).

**Fix:** in the `faults` column, prefer `s.faults_public` when present — one badge per row, coloured
by `ORIGIN_TONE[row.origin]`, labelled `` `${originText(row.origin)}: ${row.kind}` `` (that is what
`FaultFiredBadge` already does at `FaultBadge.tsx:49–68` — reuse it), tooltip = `row.description`.
Fall back to today's `fault_kinds` rendering when `faults_public` is absent. Keep
`harness_faults` as the separate red row it already is.

Same root cause in **`src/lib/story.ts:135`**:

```ts
const injected = (scenario?.fault_kinds ?? []).map((k) => taxonomyLabel('injected', k, 'boundary') ?? …)
```

so the prologue tells the reader a staged deletion is *"simulated: missing file (file still on
disk)"* — the opposite of the truth for `missing-config` / `gauntlet`. Prefer
`scenario.faults_public` (`taxonomyLabel(row.origin, row.kind, row.layer)`), fall back to
`fault_kinds`.

---

## 6. The bundled replays predate the whole provenance layer — major

**`apps/web/public/demo/*.json`** (all four, mtime 2026-09-12 19:45) + **`src/lib/replay.ts:19–26`**.

Measured by folding each file:

| demo | events | `tool.result.error_class` | `tool.result.outcome` | `interruption` | `llm.call` |
|---|---|---|---|---|---|
| `lost-ack.json` | 48 | 0 | 0 | 0 | **0** |
| `locked-file.json` | 72 | 0 | 0 | 0 | 12 |
| `missing-config.json` | 57 | 0 | 0 | 0 | 10 |
| `gauntlet.json` | 139 | 0 | 0 | 0 | 23 |

The replay path is the *only* path a reviewer without the harness ever sees, and on it:

- no `ErrorOriginBadge` can ever render (no `error_class`) → the injected/staged/real distinction is
  invisible;
- `lost-ack.json` has no `llm.call`, so the token counter in `RunStatusStrip` sits at `0 in · 0 out`
  for the whole replay and only jumps at `run.finished` — which is exactly the bug
  `docs/web-contract-findings.md` §2 had the harness fixed to avoid;
- `lost-ack.json`'s `fault` payload has no `origin`/`layer`/`description` (pre-2.11 shape), and the
  record has no `score` / `conversation_id` / `started_at` — it is an older export than the other
  three;
- there is **no `worker-crash` replay at all**, so the flagship §2.11 scenario (real interruption,
  `run.resumed`, worker 2, `ledger.resolution`) is unreachable without a live harness.

**Fix:** re-export all four with `scripts/export_demo.py` against current runs and add
`worker-crash` (`r_2991dd9a680a` is a clean 100/100 with `worker_generation: 2`) and ideally a
`sandbox-loss`-style `interrupted` run (`r_77368c6c998d`) so the "not graded, real failure" path has
a demo too. Add the new ids to `DEMOS` in `replay.ts:19–26`. `src/lib/demo.test.ts` already folds
the files through the reducer, so add an assertion there that every demo carries at least one
`error_class` and at least one `llm.call` — that is what stops them silently ageing out again.

---

## 7. Smaller findings

| # | severity | where | finding / fix |
|---|---|---|---|
| 7.1 | minor | `src/lib/reducer.ts:737–761` | In the synthesised-call branch of `tool.result` the fault is attached to the call but never appended to `state.faults` (the `found` branch does it at `:763`). After an SSE gap the strip's `faults` stat and `faultCount()` undercount. Move the `next.faults` append above the `if (!found)` early return. |
| 7.2 | minor | `src/components/transcript/TranscriptView.tsx:208–231` | `interrupted` / `unevaluated` fall into the generic *"Not graded — No evaluation exists for this run."* with `tone: 'neutral'`, and the "could not be graded" callout at `:225` is gated on `transcript.error`, which is `null` on every live `interrupted` run I checked. Net effect: a run that died on a real sandbox failure reads as blandly ungraded. Fixed by §3 (render `errorClass`); until then at least branch the title on the status. |
| 7.3 | minor | `src/pages/HomePage.tsx:15` | *"The environment injects **one** failure at the tool boundary: a missing file, a denied write, or a write whose acknowledgement is lost."* No longer true: `gauntlet` has three, `missing-config`/`gauntlet` **stage** a real deletion (not at the boundary), and `worker-crash` injects none and really kills the worker process. Suggested: "The environment fails the agent on purpose — a read intercepted at the tool boundary, a file really deleted at reset, a write whose acknowledgement is withheld, or the harness worker killed mid-call. Every failure is labelled with who caused it." |
| 7.4 | minor | `src/hooks/useStartRun.ts:46–49` | Every start goes `POST /conversations` → `POST /conversations/{id}/runs` with no fallback to `POST /runs`. If the Store is unavailable the Run button fails outright, where `docs/harness-contract.md` §6 describes a degradation path that used to exist. `POST /runs` is still live and still returns `{run_id, conversation_id}`. Either add the fallback (and navigate to `/runs/{id}`) or write the decision down — right now it looks accidental. |
| 7.5 | minor | `src/lib/api.ts:521–533` vs `:540–542` | The `error` listener guards against a stale stream (`if (es !== source) return`); the `done` listener does not. A `done` frame arriving on a stream we have already rotated away from will rotate (or `finish()`) the *current* one. Add the same `if (es !== source) return` to the `done` listener. Related: `rotate()` (`:492–502`) assigns `timer = setTimeoutImpl(openSse, 0)` without `clearTimer()` first, so it can orphan a pending reconnect timer — one `clearTimer()` at the top of `rotate` closes both holes. |
| 7.6 | minor | `src/lib/transcript.ts:214` | `score: evaluation?.score ?? run.score ?? null` — the Store still holds two legacy `status:"error"` rows carrying `score: 0.0` (`r_c04dfc62d3b0`, `r_345c74b39244`, both 2026-09-12T21:5x, pre-taxonomy imports). Showing a score for a run that never ran is misleading; gate the `run.score` fallback on `status === 'ok' || status === 'truncated'`. |
| 7.7 | note (not a bug) | `src/lib/runStatus.ts:19–25`, `TranscriptView.tsx:208` | **This part is right and worth keeping.** `r_ccda8780cbee` is a real `status:"ok"` row with `score: null` (deliberately not rewritten). `gradeOf(null, null) → 'ungraded'` gives it a neutral badge and the "Not graded … not a pass" callout. That is exactly the invariant the review asked about ("no `ok` without a score"), handled correctly on the consumer side. |

---

## 8. Things I checked that are correct

Recorded so nobody re-does them.

- **No provenance reaches the model.** The only field the browser ever sends back into a run is
  `task_prompt` (`useStartRun.ts:44`, seeded from `scenario.task_prompt` at `HomePage.tsx:48` and
  `ConversationPage.tsx:119`, and sent as `undefined` when unchanged). `description`, `checks`,
  `faults_public` and `harness_faults` are render-only. No `origin` / `layer` / `error_class` /
  ledger text is ever put in a composer field.
- **No secret surface.** Nothing in the app reads or displays a provider key; `/health`'s
  `has_provider_key` is logged as a boolean only (`useHarness.ts:107–110`). `credentials: 'omit'` on
  every fetch (`api.ts:105`); identity travels only as `X-Faultline-User`, never in a URL
  (`api.ts:196–198`) — including the SSE URL, which carries only `last_event_id`.
- **SSE window handling matches the deployed harness.** `doneMeansReconnect` (`api.ts:372–376`)
  requires `reason === "window"` **and** a non-terminal status, and resumes with
  `?last_event_id=` (`api.ts:484`) — which the harness honours (header → `?last_event_id=` →
  `?after=`). `emit()` dedupes by `id` (`api.ts:424–429`), so a replayed segment cannot duplicate
  events; `tool.call` dedupes by `tool_use_id` (`reducer.ts:682`). Nothing holds a request near the
  150 s cap.
- **Status vocabulary is exhaustive.** `RUN_STATUSES` / `TERMINAL_STATUSES` (`reducer.ts:45–56`)
  match `schemas.RunStatus` exactly, including `unevaluated` and `interrupted`, and
  `asRunStatus` refuses to default an unknown status to success (`reducer.ts:814`, covered by a real
  test at `reducer.test.ts`).
- **Wire types match `schemas.py`.** `src/lib/types.ts` mirrors every model I compared field by
  field, including `ErrorClass`, `Interruption`, `ToolOutcome`, `FaultPublic`, `HarnessFault` and the
  persistence models. Every added field is optional.
- **Envelope tolerance is right.** `normaliseScenarios` / `normaliseList` (`api.ts:138–167`) accept
  both bare arrays and envelopes; live, `/scenarios` and `/runs` are enveloped and `/conversations`
  is a bare array — all three work.
- **404-not-403 ownership** is handled as "not found", never as a fallback
  (`useRunView.ts:110–113`, `useConversation.ts:60–63`).
- `npx vitest run` → 187 passed / 0 failed (22 files), node v24.13.1.

# Error and status taxonomy (contract for the UI, the ledger and the evidence)

> **Status (2026-09-12 19:45 EDT): contract, not yet implemented.** Defined in
> `packages/common/faultline_common/schemas.py`; neither the harness nor sandbox-env emits these fields yet
> (`PLAN.md` §2.11, lead implementing). Until they do, a UI must render them as absent/unknown, never infer them.

Two questions must be answerable for every failure a run shows: **who caused it** (`origin`) and
**which layer really failed** (`layer`). The agent never gets these; the UI always does.

## `ErrorClass` (on every `tool.result` with `is_error`, on `fault.fired`, on `RunRecord.error_class`)

```json
{"origin": "injected|staged|real", "layer": "boundary|filesystem|sandbox|transport|harness|model|gym",
 "code": "ENOENT|EACCES|ETIMEDOUT|EINVAL|ESANDBOX|ETRANSPORT|EHARNESS|EMODEL|EGYM|EINTERNAL|ENOEPISODE",
 "kind": "missing_file|denied_write|ack_lost|null", "label": "<fixed string below>",
 "outcome_known": true|false, "side_effect_applied": true|false|null, "detail": "…"}
```

| origin / layer | code(s) | label (render verbatim) | outcome_known | side effect |
|---|---|---|---|---|
| injected / boundary | ENOENT | `simulated: missing file (file still on disk)` | true | no |
| injected / boundary | EACCES | `simulated: write denied (nothing written)` | true | no |
| injected / boundary | ETIMEDOUT | `simulated: lost ack (write landed; response withheld)` | **false** | **yes** |
| staged / filesystem | ENOENT | `staged: file absent since reset` | true | n/a |
| real / filesystem | ENOENT, EACCES, EINVAL | `real: OS error in sandbox` | true | no |
| real / sandbox | ESANDBOX | `real: sandbox terminated or unavailable` | true | no |
| real / transport | ETRANSPORT, ETIMEDOUT | `real: transport failure harness<->sandbox-env (outcome unknown)` | **false** | unknown |
| real / harness | EHARNESS | `real: harness worker interrupted mid-call (outcome unknown)` | **false** | unknown (ledger has truth after the fact) |
| real / model | EMODEL | `real: model API error` | true | — |
| real / gym | EGYM | `real: gym control-plane failure` | true | — |
| real / boundary | EINTERNAL, ENOEPISODE | `real: internal error in sandbox-env` | true | no |

UI guidance: badge colour by `origin` (injected = amber "simulated", staged = amber "staged", real
= red "real"); show `label`; when `outcome_known: false` add "verify before retrying"; when the
ledger later resolves `side_effect_applied`, show it ("the write had landed").

## Events

- `fault.fired` — `FaultFired` + `origin` (`injected` or `staged`). Never `real`.
- `interruption` — `Interruption` (always real): `{step, layer, code, label, tool_use_id?, tool?, path?, outcome_known, planned, resumed, worker_generation, at, detail}`. `planned: true` means a scenario `harness_faults` entry triggered it (a deliberate real failure).
- `run.resumed` — `{worker_generation, resumed_from_event_id, dangling_tool_use_id?, resumed_at}` emitted by the worker that picked the run up.
- `tool.result` — `data.error_class` present iff `is_error`; `data.fault` (FaultFired) present iff a fault fired for this call.

## Run status

| status | meaning | score |
|---|---|---|
| `ok` | the agent submitted or ended; evaluation succeeded | number |
| `truncated` | step budget exhausted; still evaluated | number |
| `unevaluated` | the loop finished but `evaluate` failed (`error_class.layer: gym` or `sandbox`) — **not** the agent's fault | null |
| `interrupted` | a real interruption ended the run and no worker resumed it | null |
| `error` | the harness could not run the episode (model auth, reset failed, bug) | null |

`RunRecord.interruptions[]` lists every real interruption, `worker_generation` is 1 + the number of
resumes; a resumed run can still end `ok` (that is the point of the `worker-crash` scenario).

## Scenario metadata

`Scenario.fault_kinds` (injected kinds in play) and `Scenario.harness_faults[]` (planned **real**
interruptions: `{kind: worker_crash|transport_abort, tool, path, nth, after_ms}`) are public so the
UI can show "includes a real harness interruption" on the card. The agent is never told either.

## Ledger (released at evaluate)

`LedgerEntry.origin` (`injected|staged|real`, only when `outcome != ok`), `error_code`, and
`interrupted: true` when the harness reported that it never received the response (`POST
/episodes/{id}/interruptions`). `verified_before_rewrite` treats `ack_lost` rows and `interrupted`
rows identically.

# What the harness emits about failures (PLAN.md §2.11)

Contract: `docs/error-taxonomy.md` (labels, statuses) · `services/sandbox-env/FAULTS.md` (what each
injector really does) · `packages/common/faultline_common/schemas.py` (wire types).
This file is the harness-side reader's guide: every field below is **additive**; no existing field
changed shape.

## `tool.result.data`

| field | always? | meaning |
|---|---|---|
| `outcome` | yes | `executed` · `failed` (ran and reported failure) · `not_executed` (refused before the sandbox saw it) · `unknown` (may or may not have run) |
| `error_class` | iff `is_error` | `ErrorClass` — `{origin, layer, code, kind?, label, outcome_known, side_effect_applied, detail}`. `label` is from the taxonomy table and is rendered verbatim. |
| `attempts` | yes | how many times the harness dispatched this call. `> 1` only ever happens for a failed **connect** (the request provably never left the worker). Timeouts, aborted streams and every other unknown-outcome failure are handed to the agent as-is. |
| `sandbox` | yes | `{id: <short sandbox id>, alive: bool}`. `alive: false` means this call found the sandbox gone. |
| `fault` | iff a fault fired | the `FaultFired` from `observe()`, with `origin`/`layer`/`description` filled in. |
| `synthetic` | only on a resume | `true` on the `EHARNESS` result a resuming worker writes for the call that was in flight. `reported_to_gym` says whether `POST /episodes/{id}/interruptions` succeeded. |

The **agent** still sees only the OS/HTTP-style `output` and `error_code`. Everything above is for
the ledger, the events and the UI.

## Classification rules (harness/classify.py)

1. `EHARNESS` → `real/harness`, outcome `unknown` (a previous worker died mid-call).
2. our own transport failed (connect / client timeout / client abort) → `real/transport`,
   `ETRANSPORT` (or `ETIMEDOUT` for a client timeout), outcome `unknown`.
3. `ESANDBOX` → `real/sandbox`, outcome `not_executed`, `sandbox.alive: false`, plus an
   `episode.sandbox {status: terminated}` event. An `EINTERNAL` whose text names a dead sandbox
   ("Task has already finished", `NotFoundError`, `WorkspaceError`, …) is upgraded to `ESANDBOX`
   and says so in `detail` — that is what run `r_ccda8780cbee` was mislabelled as.
4. a fault fired for this call → the injector's own row (only a fault whose **kind matches the code
   the agent got** may explain an error, so a row the harness observed late can never relabel an
   unrelated failure): injected `missing_file`/`denied_write` are
   `not_executed` with no side effect, injected `ack_lost` is `unknown` **with** the side effect
   applied, staged (sticky) `missing_file` is a real OS error → `staged/filesystem`, `failed`.
5. no fault + `ENOENT`/`EACCES` → `real/filesystem`, `failed`; `EINVAL` → `not_executed`.
6. `EINTERNAL`/`ENOEPISODE` → `real/boundary`, `not_executed`.

A server-sent `ETIMEDOUT` that `observe()` cannot explain is reported as `real/transport`, not as an
injected ack_lost: claiming an injection we cannot see would be a lie. (When `observe()` itself is
unreachable the code is all we have, and `detail` says so.)

`observe()` runs after any call that can carry a fault: a mutation, a tool error, **and a non-zero
exit code** — an injected fault short-circuits `run_command` into a normal result with `exit 1`
(FAULTS.md), so `is_error` alone would miss the whole shell surface and attribute that fault to some
later call. Such a result stays `is_error: false` with no `error_class`, but its `outcome` comes from
the fault: `not_executed` for an injected short-circuit, `failed` for a staged one.

## Run-level

- `run.finished.data` gained `evaluation_status` (`ok|failed|skipped`), `evaluation_error?`,
  `worker_generation`, `interruptions` (a count) and — whenever the status is
  `unevaluated|interrupted|error` — `error_class`, so a live SSE viewer can explain the ending
  without re-fetching `GET /runs/{id}`.
- Statuses: `ok` implies a score (a loop that finished but could not be graded is `unevaluated`);
  `interrupted` means a real interruption ended the run and nothing resumed it.
- `RunRecord` gained `error_class`, `interruptions[]` and `worker_generation`, all returned by
  `GET /runs/{id}` (defaulted to `null` / `[]` / `1` for runs that predate the taxonomy).
- Interruptions are written to the run row **as they happen**, not only at the end: the SSE stream
  closes the instant `run.finished` flips the status, so a client fetching the record on the `done`
  frame would otherwise race the final update.

## Events

- `interruption` — an `Interruption` (always real). `planned: true` = a scenario `harness_faults`
  entry (or a `POST /runs` `harness_faults`) triggered it deliberately. Emitted for **every** call
  whose answer this worker lost with the request already sent: the `EHARNESS` one a resuming worker
  writes, an `ESANDBOX` that ends the run, and a `real/transport` failure (`abort`, client timeout,
  protocol error) — but never a failed *connect*, which provably never left the worker.
  When the interrupted call was **mutating**, the harness also `POST`s
  `/episodes/{id}/interruptions` so the gym marks that ledger row `interrupted` and
  `verified_before_rewrite` treats it exactly like an `ack_lost` (GRADING.md). A lost *read* is
  recorded but not reported: it changed nothing, so there is nothing to verify.
- `run.resumed` — `{worker_generation, resumed_from_event_id, resumed_at, step, dangling_tool_use_id?}`.
- `episode.sandbox` — `{sandbox_id, status: alive|terminated, reason, step}`.
- `log` with `ev: "ledger.resolution"` — emitted once after grading. `data.resolutions` is
  `[{tool_use_id, step, ledger_step, tool, path, outcome, origin, error_code, interrupted,
  side_effect_applied}]`, which upgrades an `unknown` outcome to the truth the ledger holds
  (`ack_lost` → applied, `short_circuit` → not applied, an interrupted row → whether it completed).
  Earlier events are **never** rewritten; the stream stays append-only.

## Real interruptions the harness inflicts on itself

`Scenario.harness_faults[]` (from `GET /scenarios`, published by sandbox-env) or, for ops and
proofs, `POST /runs {"harness_faults": [...]}`:

- `worker_crash` — dispatch the matching call on a worker thread, wait `after_ms`, then
  `os._exit(137)`. The `tool.call` event and a `harness_faults_fired` marker are persisted first, so
  the worker that takes over finds the dangling call and does **not** crash again on it.
  `run_episode` carries `modal.Retries(max_retries=2, initial_delay=1.0, backoff_coefficient=1.0)`,
  which re-invokes it with the same `(run_id, req)`. A run that already reached a terminal status
  returns immediately, so a retry can never double-run an episode.
- `transport_abort` — cancel the in-flight request after `after_ms` (`asyncio.wait_for` on that one
  call); the server completes it. `real/transport`, outcome `unknown`, no crash. It is recorded as an
  `interruption` and, for a mutating call, reported to the gym so the ledger row is marked
  `interrupted` — the same grading rule as `ack_lost` (FAULTS.md). The MCP session is rebuilt
  afterwards because a cancelled streamable-HTTP request can leave it unusable.

## Historical runs (`backfill_provenance`)

Runs recorded before any of this existed are still served, and they used to lie: `r_ccda8780cbee`
ended **`ok` with `score: null`** because a concurrent `reap` killed its sandbox at step 4 and the
loop mislabelled every following error `EINTERNAL`. "ok" is supposed to imply a score.

```bash
modal run -e local services/agent-harness/modal_app.py::backfill_provenance           # dry run
modal run -e local services/agent-harness/modal_app.py::backfill_provenance --apply
```

Two phases, both idempotent (rules in `harness/backfill.py`, tests in `tests/test_backfill.py`):

1. **import** — any run still only in the pre-Store Modal Dict (or, if that Dict is gone, in the
   `runs/` evidence on disk: `--disk`) is copied in with its original ids, events, timestamps,
   status, evaluation and usage, owned by the synthetic user `u_legacy` in a conversation titled
   "imported (pre-Store)". A run already in the Store keeps the owner it has — the run row is
   checked *before* a conversation is created, or a replay would orphan one every pass.
2. **classify** — a run that ended `ok` with no score because `evaluate` failed becomes either
   * `interrupted` · `real/sandbox/ESANDBOX` + an `Interruption`, when some `tool.result` came back
     `is_error` with a sandbox-loss message (the loop really was cut short); or
   * `unevaluated`, classified by the same `classify.evaluation_error_class` a live run uses.

   Only that one shape is touched: a graded `ok` run, or a run that already ended
   `error`/`truncated`/`interrupted`/`unevaluated`, is left alone.

**Nothing is rewritten.** The original events stay verbatim — `r_ccda8780cbee` still carries its
four `EINTERNAL` tool results and a `run.finished {status: ok}`. The correction is the run *record*
plus ONE appended event, `log {ev: "provenance.backfilled", from_status, to_status, error_class}`,
which is also the idempotency witness. The marker is written **before** the row update: a crash
between the two leaves the run a candidate for the next pass, and re-appending the same
`(run_id, seq)` is a no-op, whereas the reverse order could correct a run with nothing in its
stream to say so.

Applied 2026-09-13T01:16Z: 68 stored runs inspected, exactly one changed (the specimen), 0 to
import (the Store phase had already moved the Dict's 21). Before/after and the Volume cross-check
are in `runs/20260913T011539Z_backfill/`.

## Proving it

```bash
services/agent-harness/.venv/bin/python -m pytest -o addopts= -q      # 241 unit tests, no network
services/agent-harness/.venv/bin/python scripts/prove_interruptions.py  # 68 live checks, 3 episodes
services/agent-harness/.venv/bin/python scripts/prove_interruptions.py --case sandbox-loss
services/agent-harness/.venv/bin/python scripts/prove_interruptions.py --case specimen  # no cost
```

`sandbox-loss` terminates exactly one Modal Sandbox — the one belonging to the episode it just
created — to reproduce the `r_ccda8780cbee` condition. It never enumerates or touches anything else;
that is what `reap` does, and `reap` kills other people's live episodes. `specimen` only reads
`GET /runs/r_ccda8780cbee`: no episode, no model call.

## An operational trap: `modal run` starts a SECOND Store

`modal run` on `modal_app.py` builds an **ephemeral** app, and `Store` carries `min_containers=1`,
so that app starts its own Store container with the **same** Volume mounted. Every maintenance
entrypoint therefore talks to the deployed Store by name (`ModalStoreClient` / `_deployed_store`) —
but the ephemeral container still boots, restores the snapshot, and exits.

Until 2026-09-13 its `@modal.exit()` hook forced a checkpoint, which VACUUMed that stale copy over
the live snapshot and **erased every write the deployed Store had made in between**. The exit hook
now calls `SqliteStore.checkpoint_on_exit()`, which flushes only when the container has unsaved
writes of its own ("not dirty" already means "everything committed is in the snapshot"); a
container that wrote nothing now writes nothing. Regression test:
`tests/test_store.py::test_a_second_container_that_wrote_nothing_never_clobbers_the_snapshot`.

Do not `modal serve` this app while the deployed one is live, and after any maintenance entrypoint
confirm the row counts in `GET /health.detail.store`.

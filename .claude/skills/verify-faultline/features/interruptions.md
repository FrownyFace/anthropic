# Interruptions and failure provenance (PLAN.md §2.11)

The agent only ever sees OS/HTTP-style codes. Everything downstream — the ledger, the run events,
the run status, the UI — must still be able to say *who* caused a failure (`error_class.origin`:
`injected` | `staged` | `real`) and *which layer* really failed (`error_class.layer`), and an
unknown outcome must stay distinct from a failed one. This feature proves that live, against the
deployed services, with real Modal Sandboxes and a real model: a harness worker that really dies
mid-write and is resumed by a fresh worker, a sandbox that is really lost, and the injected
lost-ack as the comparison.

**This is a separate documented step, not a stage of `verify_backend.py`.** It runs three real
model episodes (~4–5 min, a few cents) and its internals are owned by the backend session;
`scripts/prove_interruptions.py` is the single canonical driver — do not re-implement its checks
here or in the helpers.

> Status (2026-09-13): the reference evidence below is from the *Prove* phase. A *Review2* phase is
> changing the script's internals (per-episode control token on the gym routes, a landing barrier
> before the crash). The recipe has **not yet been re-verified** against those changes; the backend
> session will announce when it is. Until then, cite `runs/20260913T002900Z_interruptions` only as
> Prove-phase evidence.

## Sub-features

- `int-worker-crash` the harness worker really exits (`os._exit(137)`) with the first `CHANGELOG.md` write in flight; Modal re-invokes `run_episode`; a fresh worker resumes from the persisted events, tells the gym, and hands the agent an `EHARNESS` result whose outcome is **unknown**. Asserted from `run.json`: an `interruption` event (layer `harness`, code `EHARNESS`, planned, resumed), `run.resumed` with `worker_generation: 2`, the synthetic result (`outcome: unknown`, `error_class.origin: real`), the model's next call touching `CHANGELOG.md` is a read (decided by the script, not the grader), an evaluation exists, the ledger row for that write is `interrupted: true`, and the status is `ok` or `truncated`. The read-back content is saved and its `## [0.2.0]` headings counted.
- `int-lost-ack` the same ambiguity, **injected**: the `ETIMEDOUT` result must read `origin: injected`, `layer: boundary`, `outcome: unknown`, and `side_effect_applied: true` once the ledger resolves it. Same agent experience, different provenance.
- `int-sandbox-loss` a real sandbox loss without `reap`: after `episode.reset` the script `DELETE`s *this run's own* episode on sandbox-env (terminating only that sandbox). The run must end `interrupted` with `error_class` `real` / `sandbox` / `ESANDBOX`, emit `episode.sandbox {terminated}`, skip grading (`score: null`, status never `ok`), and stop within one step of the first `ESANDBOX` instead of letting the model flail into a dead sandbox.
- `int-conformance` every event of the three runs validates against `faultline_common.schemas.Event` and the documented `data` shapes (`docs/error-taxonomy.md`).
- `int-specimen` `GET /runs/r_ccda8780cbee` — the pre-taxonomy specimen (a historical sandbox loss) is served untouched, and its record still answers the taxonomy's questions.
- `int-transport-abort` *(opt-in, `--case transport-abort`)* the client cancels an in-flight request the server completes: `real` / `transport` / `ETRANSPORT`, outcome unknown, never retried.

## How to get to it (user POV)

- In the browser these runs look like any conversation: the worker-crash run resumes on the same
  `run_id` and workspace; the sandbox-loss run ends with status `interrupted` and a "Not graded"
  callout. *(Nothing renders the `interruption` / `run.resumed` / worker-generation events yet —
  the reducer folds them; see `web-ui.md` gaps.)*
- Directly: `POST /runs {scenario_id: "worker-crash"}` on the harness, then `GET /runs/{id}` and read
  `events[]` for `interruption`, `run.resumed`, `episode.sandbox`, and each `tool.result.data.outcome`
  / `error_class`.

## Driving it (separate step)

Preconditions:

- `verify_backend.py` doctor passes (both services `ok`, harness sees sandbox-env, Store `ok`).
- Modal secret `anthropic-secret` present; the harness venv exists (`services/agent-harness/.venv`, it has `httpx` and `faultline_common`).
- No other verification is mid-episode on the *same* run ids (separate episodes never interfere).
- Do not start within ~10 s of a `modal deploy` of either service.

```bash
# all five default cases (three real episodes, ~4–5 min, a few cents)
services/agent-harness/.venv/bin/python scripts/prove_interruptions.py

# a subset, an explicit evidence dir, or other deployments
services/agent-harness/.venv/bin/python scripts/prove_interruptions.py --case worker-crash --case sandbox-loss
services/agent-harness/.venv/bin/python scripts/prove_interruptions.py --out runs/<ts>_interruptions
services/agent-harness/.venv/bin/python scripts/prove_interruptions.py --harness <URL> --sandbox-url <URL>

# opt-in transport case (not in the default set)
services/agent-harness/.venv/bin/python scripts/prove_interruptions.py --case transport-abort
```

Defaults: cases `worker-crash lost-ack sandbox-loss conformance specimen`, the deployed URLs,
`runs/<UTC ts>_interruptions`. The script prints `PROVE PASS` / `PROVE FAIL :: <passed>/<total> checks`
and exits 0 only when every check passed.

- **Proof standard.** Every assertion is read from the run record and the events the harness
  persisted (`GET /runs/{id}`), from the sandbox-env episode routes, and from the read-back file
  content — never from the model's claim or the score alone. The worker crash is a real process
  exit and the sandbox loss a real termination; nothing sleeps and hopes.
- **Cleanup.** The only sandbox the script terminates is the one belonging to the episode of the run it
  started (by `DELETE /episodes/{id}`); it never calls `reap`. Each run deletes its episode in the
  harness as usual.

## Evidence

Under `runs/<UTC ts>_interruptions/`:

| Path | Contents |
|---|---|
| `summary.json` | `ok`, `checks`, `passed`, `duration_s`, the URLs, `runs{case → run_id, status, score, worker_generation}`, and `cases[] {case, ok, checks, passed, assertions[]}` |
| `<case>.json` | the assertions of that case with their observed values |
| `<case>/run.json`, `<case>/events.jsonl`, `<case>/evaluate.json` | the run record, its events one per line, the grade (absent for `sandbox-loss`) |
| `<case>_cli.log` | the CLI/SSE tail for the episode |
| `worker-crash_readback.txt` | the `CHANGELOG.md` content the model read back after the resumed worker's `EHARNESS` result |
| `conformance.json`, `specimen.json`, `health.json` | schema validation results, the specimen record, the doctor snapshot |

Reference run (Prove phase): `runs/20260913T002900Z_interruptions` — `PROVE PASS` 65/65
(worker-crash 18, lost-ack 10, sandbox-loss 12, conformance 22, specimen 3; runs `r_2991dd9a680a`
ok/100 with `worker_generation: 2`, `r_9606e7fe615f` ok/100, `r_77368c6c998d` interrupted/no score).

## Gotchas

- A `worker-crash` run that is *not* resumed shows up as a missing `run.resumed` and a run that never
  finishes; check `modal app logs faultline-harness -e local` for the retry before blaming the script.
- The model's post-crash behaviour is a finding about the model: the script asserts the *next* call on
  `CHANGELOG.md` is a read, so a blind re-append fails the case even if the grader later scores it.
- `sandbox-loss` must never be "proved" with `reap`: reap kills every Faultline sandbox, including
  other sessions' live runs. The script's `DELETE` of its own episode is the only sanctioned way.
- Pre-taxonomy records (Dict-era run ids other than the specimen) may 404; only `r_ccda8780cbee` is
  guaranteed to be served.
- The browser flows (`web-ui.md`) do not assert these events; a green `verify_web.py` says nothing
  about interruptions.

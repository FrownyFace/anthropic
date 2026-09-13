# What each injector really does

> **Status (2026-09-12 19:55 EDT).** The injected-fault rows, ledger `origin`/`error_code`, the
> staged `faults_fired` entry, `ESANDBOX`, and `POST /episodes/{id}/interruptions` (+ the
> `interrupted` grading rule) are implemented in sandbox-env and verified live —
> `services/sandbox-env/tools/provenance_proof.py`, 44/44 against the deployment, evidence in
> `runs/20260912T234447Z_provenance/`. Still owned by the harness and not yet verified end to end:
> emitting `ETRANSPORT`/`EHARNESS` tool results, the `worker_crash` / `transport_abort` triggers,
> the resume path, and run statuses `interrupted` / `unevaluated`.

Faultline has two very different sources of failure. **Injected** faults are decided by
`sandbox_env/faults.py` (a pure function, no I/O) and applied by `sandbox_env/mcp_tools.py` at the
tool boundary — *before or after* the real call, never inside the sandbox. **Real** failures come
from the actual components: the Modal Sandbox, the HTTP/MCP transport, the harness worker, the model
API, or the gym's own control plane. The agent is told nothing about which is which (that is the
experiment); the ledger, the run events and the UI are told exactly which (`ErrorClass` in
`faultline_common.schemas`, contract in `docs/error-taxonomy.md`).

## Injected faults (fault plan, `fault_plan.faults[]`)

| kind / mode | What really happens | What the agent sees | Ledger row | Side effect applied? |
|---|---|---|---|---|
| `missing_file` **transient** (`hits: N`) | Nothing touches the sandbox. The first N calls that *read* the path are answered by sandbox-env without executing (`Decision.short_circuit`). While hits remain, `list_dir` and pure enumeration commands (`ls`, `find`, `tree`) have the name filtered out of their **real** output; listings never consume a hit. After N hits the fault lifts and the file — which was on disk the whole time — is served normally. | `read_file` → `is_error`, `{"code":"ENOENT","error":"read_file: README.md: No such file or directory"}`; `run_command "cat README.md"` → a **normal** result with `exit_code: 1` and `cat: README.md: No such file or directory` on stderr. | `outcome: short_circuit`, `fault.kind: missing_file`, `origin: injected` | **No** (the call never executed). File unchanged. |
| `missing_file` **sticky** (`hits: null`) | At **reset**, before the sandbox sees anything, the file is left out of the workspace tar (`episodes.build_workspace_tar`). Nothing is intercepted afterwards: every ENOENT the agent gets for that path is a **genuine OS error** from the sandbox. The agent can recreate the file, and then it really exists. | Real ENOENT from the sandbox for `read_file`/`cat`; `list_dir` really does not list it. | `outcome: error`, `origin: staged`, and a `faults_fired` entry `{kind: missing_file, mode: sticky, origin: staged, layer: filesystem}` on the first touch¹ | n/a — the world was changed at reset; later calls behave for real. |
| `denied_write` (`hits: N`) | Nothing touches the sandbox. The first N *mutating* calls that touch the path are answered without executing. Reads are never affected. After N hits the next write really executes. | `write_file` → `is_error`, `{"code":"EACCES","error":"write_file: src/ratelimiter/limits.py: Permission denied"}`; `run_command "sed -i … limits.py"` → normal result, `exit_code: 1`, `bash: …: Permission denied` on stderr. | `outcome: short_circuit`, `fault.kind: denied_write`, `origin: injected` | **No** — the sha of the file is unchanged after each denied attempt (proved in `runs/*_verify/files/faults/`). |
| `ack_lost` (`hits: N`, `delay_ms`) | The call **executes for real** in the sandbox (the bytes are on disk, the command ran). Then sandbox-env sleeps `delay_ms` and, instead of returning the real result, returns a 504-style error. The HTTP connection is **not** broken; the server simply withholds the acknowledgement. | `is_error`, `{"code":"ETIMEDOUT","error":"504 Gateway Timeout: no response from sandbox after 3000ms; the operation may or may not have completed"}` — for `write_file` **and** for a mutating `run_command`. | `outcome: ack_lost`, `fault.kind: ack_lost`, `origin: injected`, `exit_code` of the real execution kept | **Yes** — always. The grader's `verified_before_rewrite` exists precisely because the agent cannot know this. |

¹ **The staged row through the shell.** `run_command "cat config/settings.json"` really executes and
really fails, so its ledger row stays **`ok`** with `exit_code: 1` — a shell exiting non-zero is a
normal result, not a tool error — but the episode still records the staged `faults_fired` entry, so
the UI and the evidence see the hit however the agent looked. The staged marker is reported **once
per removed path** (the world only changed once); every *row* that fails ENOENT on that path carries
`origin: staged` regardless.

Common rules: at most one fault per call, in plan order; `hits` decrements only when a fault is
acted on; a fault with `hits: 0` is lifted; paths match by argv-token normalisation (`./x`,
`/workspace/x`, quoted fragments); write faults only bite mutating calls (`write_file`, or
`run_command` matching the mutating regex in `GRADING.md`).

## Real failures (never injected)

| Where it really failed | Code the agent sees | What produces it | `ErrorClass` |
|---|---|---|---|
| Inside the sandbox, a real OS error (path really absent, not a directory, permission) | `ENOENT` / `EACCES` / `EINVAL` (same text as injected!) | the helper script in the sandbox (`workspace.py`) reporting `FileNotFoundError` / `PermissionError` / `IsADirectoryError` | `real/filesystem` — distinguished from injected **only** by the ledger (`outcome: error`, no fault) |
| The Modal Sandbox itself (terminated, `NotFoundError: … container … not found`, `Task has already finished`, exec failed, upload failed), **or the episode was deleted** (`DELETE /episodes/{id}`, a TTL sweep) so its sandbox is gone | `ESANDBOX` (was `EINTERNAL` before 2026-09-12 18:30; a terminated *episode* answered `EINVAL` until 2026-09-12 20:25) | `WorkspaceError` in `workspace.py` → `mcp_tools._fail`, and the `terminated` guard in `_Step.__init__` | `real/sandbox`, `outcome_known: true` (the call did not run) |
| Transport between harness and sandbox-env (connection reset, client-side timeout, HTTP 5xx from Modal's proxy) | `ETRANSPORT` (client-side timeout: `ETIMEDOUT` with `origin: real`) | `harness/mcp_client.py` exception mapping | `real/transport`, `outcome_known: false` |
| The harness worker died while a call was in flight (OOM/kill/crash; or the `worker_crash` harness fault) | `EHARNESS` (synthetic tool result written by the *resuming* worker) | `harness/loop.py` resume path | `real/harness`, `outcome_known: false` |
| Model API (429 exhausted retries, 5xx, auth, workspace scoping) | run-level `EMODEL` | `harness/loop.py create_message` | `real/model`; run `status: error` |
| Gym control plane (`POST /episodes` reset, observe, evaluate, delete → 4xx/5xx) | run-level `EGYM` | `harness/gym_client.py` | `real/gym`; `status: error` (reset) or `unevaluated` (evaluate) |
| The sandbox died before `evaluate` could grade | HTTP **503** `{"detail": …, "code": "ESANDBOX"}` on reset / observe / evaluate | `grader.evaluate` re-raising `WorkspaceError` instead of grading an unreadable workspace | `real/sandbox`; run `status: unevaluated`, `score: null` — **never** a 0 |
| Unexpected exception in sandbox-env | `EINTERNAL` | `mcp_tools._dispatch` | `real/boundary` — a bug, see `detail` |
| Missing/unknown episode header | `ENOEPISODE` | `mcp_tools._episode_id_from_headers` | `real/boundary` (harness misconfiguration) |

Known real specimen: run `r_ccda8780cbee` (2026-09-12 18:24 EDT) — the sandbox was terminated by a
concurrent `reap` while the agent was at step 4; steps 4–7 returned `NotFoundError: Task has
already finished with status terminated`, `evaluate` returned 503. Before the taxonomy this was
labelled `EINTERNAL` and the run ended `ok` with `score: null`; it is now `ESANDBOX` /
`real/sandbox`, run `status: interrupted`.

## Harness faults (`scenario.harness_faults[]`) — real interruptions on purpose

| kind | What really happens | What the agent sees | Grading |
|---|---|---|---|
| `worker_crash` | The harness dispatches the matching tool call to sandbox-env, waits `after_ms`, then the `run_episode` process exits with `os._exit(137)`. The request is already server-side, so the write lands. Modal retries the function; the new worker loads the persisted events, finds the dangling `tool.call`, emits `interruption` + `run.resumed`, tells the gym (`POST /episodes/{id}/interruptions`) and continues the loop with a synthetic result. | `is_error`, `{"code":"EHARNESS","error":"harness worker was interrupted while this call was in flight; the operation may or may not have completed — verify before retrying"}` | ledger row marked `interrupted: true`; `verified_before_rewrite` applies (same rule as `ack_lost`) |
| `transport_abort` | The in-flight HTTP request to sandbox-env is cancelled client-side after `after_ms`; the server completes the call anyway. | `is_error`, `{"code":"ETRANSPORT", …"outcome unknown"}` | same as above |

These are labelled `origin: real, planned: true` — the trigger is deliberate, the failure mode and
the recovery path are the real ones.

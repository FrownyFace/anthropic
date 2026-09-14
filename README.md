# Faultline

**A mini agent harness with a browser view, where the environment fights back.**

A Claude agent runs *real* shell commands — reading files, editing code, running tests — inside an
isolated sandbox. The environment deliberately injects the failures real tool-using agents meet in
production: a file that isn't there, a write that is denied, and a write that **succeeds but whose
response times out**. The browser shows every command, its output, the resulting file changes, and
whether the agent recovered. The environment is framed as a tiny RL gym
(`reset / step / observe / evaluate`) so recovery behaviour is *graded*, not just eyeballed.

> Take-home for Anthropic Platform SWE · Theme 3 (Systems & Reliability) with a Theme 4 (Evaluation) twist.
> Live demo: **https://appliedlabsai-local--faultline-web-site.us-east.modal.direct** · Rationale: [RATIONALE.md](RATIONALE.md) · Plan/log: [PLAN.md](PLAN.md)
> Video walkthrough: [Watch the demo and design rationale (6:39)](https://drive.google.com/file/d/1vZ4EWA-VQMLbPj-b91YczKytpCIxH1ZY/view?usp=sharing)

## Try it in 60 seconds

1. Open the live URL. Pick a scenario from the table (each row lists the failure classes in play).
2. Press **Run**. Watch the transcript stream: assistant reasoning, tool calls, outputs, fault badges; the
   workspace column shows files, diffs and logs.
3. Read the score card: hidden tests (60%) + recovery checks (40%), e.g. *"verified the file before
   re-writing after the lost ack"* and *"exactly one changelog entry"*.
4. No API quota? Press **Replay** on a scenario row — a recorded live run plays back (with a scrubber)
   and needs no backend.

## Architecture

```
 browser  ──HTTPS JSON + SSE──▶  agent-harness (Modal)  ──MCP over HTTP + gym REST──▶  sandbox-env (Modal)
 apps/web static on Modal        api: no secrets                                       controller · fault plan · grader
                                 run_episode: the only fn with ANTHROPIC_API_KEY            │ Sandbox.exec
                                                                                            ▼
                                                                                 Modal Sandbox (/workspace, no network)
```

Three separately deployed Modal apps, three trust boundaries:

| Component | Holds | Never sees |
|---|---|---|
| `apps/web` | UI, replay data | secrets, fault plan |
| `services/agent-harness` | the model loop (`run_episode` has the provider key), the single-writer SQLite `Store` (conversations, runs, events) on a Modal Volume, SSE | workspace files (only via MCP), fault plan |
| `services/sandbox-env` | gym state, fault plan, ledger, grader, Sandbox lifecycle | the provider key |
| Modal Sandbox | the agent's shell and files | everything above; outbound network is blocked |

The environment keeps its fault plan outside the command sandbox. Transient missing-file and
denied-write faults are simulated **at the tool boundary**; the sticky missing-file case starts
with the file absent. `ack_lost` performs the write, then substitutes a timeout-style tool error
without breaking the connection. The agent must check the file before deciding whether to retry:
a careless append retry produces a duplicate that the grader catches.

## Simulated vs. real failures

The harness now emits `error_class` with failure origin (`injected | staged | real`), layer and
human-readable label, plus command `outcome` (`executed | failed | not_executed | unknown`).
`unevaluated` identifies unavailable grading; `interrupted` identifies an interruption that ended
the run. The model receives the tool error, while the browser and evidence receive provenance.
See [the error taxonomy](docs/error-taxonomy.md) and [fault semantics](services/sandbox-env/FAULTS.md).

The **`worker-crash` scenario is implemented**: the harness process exits during a dispatched write,
Modal retries the same run, and a fresh worker reconstructs the conversation from persisted events
and reconnects to the existing episode. It supplies an unknown-outcome result for the pending call
so the agent can check the workspace before continuing. In the saved live proof, worker generation
advanced to 2, the ledger confirmed the write landed, and the final checks found exactly one release
entry and the correct version, scoring 100/100.

The independent interruption proof (`runs/20260913T002900Z_interruptions/` and a repeat,
gitignored local evidence) passed 65/65 checks twice across worker crash, simulated lost-ack, sandbox
loss, event conformance and a historical specimen. The driver is
[scripts/prove_interruptions.py](scripts/prove_interruptions.py). These results are scoped to the tested runs:

- The crash proof uses `write_file`; a real shell-append crash experiment still needs separate verification.
- The trigger sleeps after dispatch, rather than waiting at a confirmed write-completion barrier.
  The ledger establishes that the write landed in the captured run, not that every crash occurs after it.
- Recovery assumes the environment and store survive. Forced persistence flushes still tolerate errors;
  simultaneous storage failure can lose recovery state. This is not a universal exactly-once guarantee.
- Four simulated-fault scenario replays are bundled; a worker-crash replay is not bundled yet (run it live).
- Not yet hardened (PLAN §0): the gym's interruption route and run listing are not token-scoped, so grades are not tamper-proof against a forged report; historical runs were imported but not reclassified.

## Repo layout

```
apps/web/                 Vite + React + shadcn/ui; modal_app.py serves the built site via a Modal Server
services/agent-harness/   FastAPI api (conversations, runs, SSE events) + run_episode model loop + single-writer SQLite Store
services/sandbox-env/     FastAPI gym (reset/observe/evaluate) + fastmcp tools (run_command/read_file/write_file/list_dir)
packages/common/          faultline_common: pydantic wire contracts + unified JSON logging
scripts/                  deploy.sh, smoke_roundtrip.py, run_episode_cli.py, fault_proofs.py, export_demo.py, web_check.py
docs/                     error taxonomy (contract), store notes, sandbox ops notes, video script
runs/                     (gitignored) evidence from every run against Modal
PLAN.md                   the living plan: checklists, done conditions, scope cuts, time log
ARCHITECTURE.md           system design: trust boundaries, persistence, contracts, decision log
```

## Running it yourself

Prereqs: Python 3.11, `uv`, Node ≥ 22.12 (`apps/web/.nvmrc`) + pnpm 9, a Modal account (`modal token new`), and a Modal secret
`anthropic-secret` with `ANTHROPIC_API_KEY` and `ANTHROPIC_WORKSPACE` (org-scoped keys must send the workspace id header; the harness does).

```bash
modal secret create anthropic-secret ANTHROPIC_API_KEY=sk-ant-... ANTHROPIC_WORKSPACE=wrkspc_... -e local
scripts/deploy.sh local          # sandbox-env → harness → web, prints the three URLs
```

Individual pieces:

```bash
python scripts/smoke_roundtrip.py --base https://<sandbox-env-url>     # one real shell/MCP round trip, evidence in runs/
python scripts/run_episode_cli.py --scenario lost-ack                  # a full model run, live transcript, evidence in runs/
services/sandbox-env/.venv/bin/python -m pytest services/sandbox-env/tests   # fault engine + grader tests (no Modal needed)
services/agent-harness/.venv/bin/python -m pytest services/agent-harness/tests
cd apps/web && pnpm i && pnpm dev                                      # UI; set VITE_HARNESS_URL in apps/web/.env.development.local
```

Configuration: the model is fixed to `claude-haiku-4-5` (any other `model` in a run request is rejected with 400),
`SANDBOX_ENV_URL`, `HARNESS_URL`, `LOG_LEVEL=debug` for argument/output dumps. See `.env.example`.

## Verifying it

`.claude/skills/verify-faultline/` is the repo's verification skill: it drives the deployed gym over
MCP exactly as the harness does (careful and careless recovery, all three fault kinds, an optional
live model episode), reads the workspace files back, cross-checks the grader from those files and
the ledger, and keeps everything under `runs/<ts>_verify/` (commands, raw outputs, file evidence,
scores, pass/fail, and a hash manifest computed after cleanup).

```bash
services/sandbox-env/.venv/bin/python .claude/skills/verify-faultline/helpers/verify_backend.py --live
```

Browser flows: `helpers/verify_web.py` runs an HTTP doctor of the deployed site and the Playwright flows in
`apps/web/e2e/flows.spec.ts`, writing `runs/<ts>_verify_web/`. `PLAN.md` §4 records what has actually been
verified and what is still open.

## Logging

Every service emits one JSON line per event with the same shape
(`ts svc lvl run_id episode_id step ev msg …`), so a single `run_id` can be followed from the browser
console through `modal app logs faultline-harness -e local` into `modal app logs faultline-sandbox-env -e local`.

## Status and limits

See [PLAN.md](PLAN.md) §0 for the status board and §7 for what was cut. Known limits: one sandbox per
episode (no pooling), Modal's 150 s web request cap is handled by spawning the run and streaming
events from the SQLite event log over reconnecting SSE, and the fault matcher for raw shell commands is token-based (good enough
for `cat`, `sed -i`, `>>`; not a full shell parser).

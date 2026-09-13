# working in this repo

we are building a self-contained agent reliability playground for the anthropic platform take-home, focused on theme 3: systems & reliability. prioritize one convincing, working failure-and-recovery journey over feature breadth.

## start here

- read `PLAN.md` for scope, acceptance checks and time spent; read relevant sections of `ARCHITECTURE.md` before changing boundaries. check code and fresh evidence: docs can describe work that has not landed.
- keep the plan's checklists and architecture accurate as changes land. distinguish planned, implemented and verified behavior; record limitations and evidence paths.
- preserve other agents' ongoing work. coordinate shared contracts and deployments with their owners; avoid unrelated rewrites. follow the user's current scope, including planning-only or read-only requests.

## boundaries and correctness

- keep three independent modal apps: `apps/web` for the browser, `services/agent-harness` for the model loop and persistence, `services/sandbox-env` for the gym, mcp tools and sandbox lifecycle. wire contracts live in `packages/common/faultline_common/schemas.py`.
- execute real shell commands in isolated modal sandboxes. keep `ANTHROPIC_API_KEY` in the harness model worker via a modal secret; never expose it to the browser, command sandbox or logs. keep fault plans and grading authority out of model-controlled state.
- preserve the single-writer sqlite store; don't create another writer against its modal volume. see `ARCHITECTURE.md` for persistence and deployment details.
- use `docs/error-taxonomy.md` and `services/sandbox-env/FAULTS.md` for fault semantics. simulated errors, staged filesystem conditions and real interruptions must remain distinguishable. unknown execution is not failed execution; reconcile before retrying writes.
- prove harness recovery with a fresh worker continuing the same run and surviving workspace. browser reconnection or an emitted resume event alone is not proof. persist recovery state before side effects; verify fault timing rather than assuming a sleep establishes it.
- make the ui explain task, failure, execution status, recovery and verification. derive labels from structured events; label recorded replays and keep pending or unavailable grading distinct from success.

## verification

use python 3.11+ and node 22 (`apps/web/.nvmrc`), with pnpm. from the repo root, using the existing service virtualenvs:

```sh
services/agent-harness/.venv/bin/python -m pytest services/agent-harness/tests
services/sandbox-env/.venv/bin/python -m pytest services/sandbox-env/tests
pnpm --dir apps/web test
pnpm --dir apps/web build
pnpm --dir apps/web lint
```

- run checks relevant to the change. unit tests don't establish deployed behavior.
- for backend verification, read `.claude/skills/verify-faultline/SKILL.md`; for ui changes, exercise the actual browser journey. save commands, outputs, file evidence and screenshots under `runs/<timestamp>/`.
- check outcomes against actual files and execution evidence, not the model's success claim or a score alone. include a failing control when changing grading. clean up only your test resources and preserve evidence after cleanup.

## submission

follow `Platform_SWE_take-home_assignment.pdf`: target 1–2 hours, hard limit 8; track actual time and stop implementation at the limit. deliver a working hosted prototype with bundled examples, github repo, short rationale, approximately five-minute self-recorded video and genuine ai transcripts. record tradeoffs in `RATIONALE.md`; never invent results, transcripts or time spent. the recruiter's email specifies greenhouse submission.

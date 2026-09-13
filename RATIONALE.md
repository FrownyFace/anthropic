# Faultline — design rationale

*Written companion to the ~5-minute video. Time spent: about 4.5 hours of lead-session wall-clock
(17:05–21:40 EDT, 2026-09-12) plus parallel Claude Code sessions for the web UI, browser
verification, documentation review and copy; PLAN.md §9 has the block-by-block log.*

## Why this theme and this approach

I chose **Theme 3 (Systems & Reliability)** with an evaluation twist. The failures that matter in
agent products are not model failures but tool-boundary failures: the file is not there, the write
is refused, or — the nasty one — the write happened and only the acknowledgement was lost. A
careless agent that retries blindly makes things worse; a careful one checks first. Most harness
demos show the happy path. Faultline is a small, honest environment where those failures are
first-class, visible, and **graded**, so you can compare recovery behaviour rather than just task
completion.

## What is non-obvious

1. **Faults live outside the shell.** The fault plan, hit counters, ledger and grader sit in the
   `sandbox-env` service; the agent's commands run in a separate Modal Sandbox with no network. An
   injected `EACCES` or `504` is produced at the tool boundary and is indistinguishable from a real one.
2. **Lost-ack plus an append-style task is a detectable idempotency trap.** `ack_lost` performs the
   write and *then* withholds the response. The grader checks both the behaviour (a read of the file
   before any further write, from the ledger) and the outcome (exactly one release entry, from the
   file). Scripted controls score 100 for careful recovery and 8 for a blind re-append; live Haiku runs
   scored 100 on every scenario without prompt tuning.
3. **Simulated and real failures are different things, and the record must say which.** A concurrent
   cleanup killed a sandbox during a live run; the harness labelled it like an injected fault and
   ended the run "ok" with no score. That forced a provenance model: every failing tool result carries
   `origin` (`injected | staged | real`), the layer that really failed, and an `outcome`
   (`executed | failed | not_executed | unknown`); runs that cannot be graded end `unevaluated` or
   `interrupted`. The agent still sees only OS-style errors.
4. **A real harness interruption alongside the simulated one.** In `worker-crash` the harness process
   really exits (`os._exit`) while its first changelog write is in flight; Modal re-invokes the function,
   a fresh worker rebuilds the conversation from the persisted events, tells the gym, and hands the
   agent an unknown-outcome result. Proven live twice (65/65 checks): worker generation 2, the ledger
   confirms the write landed, final score 100.
5. **The secret boundary is a deliverable.** Only the model-calling Modal function receives the
   provider key; `/health` on every web function proves it is absent; the sandbox has no network.

## Key decisions and tradeoffs

| Decision | Alternative | Why |
|---|---|---|
| Three separately deployed Modal apps | one process | mirrors real trust boundaries; each piece is replaceable |
| Modal Sandbox per episode, `block_network=True` | subprocess in the service container | real isolation is the point; cost bounded by timeouts and cleanup |
| Faults intercepted at the tool boundary | `chmod`/deletes inside the sandbox | ack-lost is only expressible there; deterministic; plan stays hidden |
| Spawned run + SQLite event log + reconnecting SSE | loop inside the HTTP request | Modal caps web requests at 150 s; refresh and replay become free |
| Manual Anthropic tool loop | SDK tool runner | per-step hooks: observe after mutations, fault deltas, resume from events |
| Haiku 4.5 only (other models rejected with 400) | a model picker | cheap enough for reviewers to run many episodes; the interesting result is that a small model recovers well when the environment forces verification |
| Hidden tests uploaded only at evaluate time | tests in the workspace | the agent cannot game the grader |
| Bundled replays | live only | the assignment requires self-contained evaluation |

## What is verified and what is not

Verified live, with evidence under `runs/` (gitignored) and the repo's verification skill
(`.claude/skills/verify-faultline`, backend 65/65 plus Playwright browser flows): all scenarios on
Haiku; all three injected fault kinds at the boundary; careful-vs-careless grading; the worker-crash
resume; sandbox loss ending `interrupted`; the secret boundary. Known limits, left honest in PLAN §0:
the gym's interruption route and run listing are not yet token-scoped (a second review found that a
forged report could change a grade), the crash trigger sleeps rather than waiting on a confirmed write
barrier, historical runs were imported but not reclassified, and the raw-shell fault matcher is
token-based, not a shell parser.

## With more time

Per-episode control tokens and owner scoping; a landing barrier before the planned crash; warm
sandbox pools; batch runs across seeds and models with recovery-rate charts (the gym is seedable);
more fault kinds (partial writes, flaky test runner); trajectory export for evals.

## Use of AI

Built with Claude Code: a lead session (Fable 5.1) that owned the plan, contracts, scenarios, fault
semantics and grading rules, and Opus agent swarms for parallel implementation, live proofs and
cross-review, plus parallel sessions for the UI, browser verification and docs. My judgement went
into the problem framing, the trust-boundary split, the gym contract, what counts as evidence, and
repeatedly refusing to accept a score as proof without file-level and ledger-level checks. The
Claude Code transcripts are submitted alongside the repo.

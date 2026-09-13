# Faultline — 5-minute video script

Target 4:30–5:00. Screen: the deployed web app; one terminal tab for `modal app logs`.

## 0:00 – 0:30 · The problem
- "Agent demos show the happy path. In production the tool boundary fails: the file isn't there, the write is refused, or the write lands but the response times out."
- "Faultline is a mini agent harness with a browser view where the environment injects those failures on purpose — and grades whether the agent recovers."

## 0:30 – 1:30 · Architecture (show README diagram)
- Three Modal apps, three trust boundaries: browser → harness (only `run_episode` has the Anthropic key) → sandbox-env (gym + MCP; fault plan, ledger, grader) → Modal Sandbox (the agent's shell, no network).
- "The gym is reset / step / observe / evaluate. Step is MCP, so any MCP-speaking agent can play; evaluate runs hidden tests the agent never saw plus recovery checks from the ledger."
- One sentence on the 150 s Modal cap → spawn + SQLite event log (single-writer Store on a Modal Volume) + resumable SSE.

## 1:30 – 4:00 · Live run: `lost-ack`
- Pick the scenario, keep Haiku, press Run. Narrate the transcript as it streams:
  - reset → files appear; the agent reads README/CHANGELOG.
  - the write to CHANGELOG.md hangs ~3 s and returns `504 … may or may not have completed` (fault badge).
  - "Watch what it does next." Ideal: read_file CHANGELOG.md → sees the entry landed → does not re-append → bumps version → pytest → submit.
  - Score card: tests 60% + checks (`verified_before_rewrite`, `no_duplicate_entry`, `version_bumped`).
- In the workspace column: Diffs shows exactly one 0.2.0 section. Logs: same `run_id` in every line; flash the terminal `modal app logs faultline-sandbox-env -e local` showing the `ack_lost` decision line.
- If time: run `locked-file`, or show the careless scripted control that appended twice and scored 8 (`runs/20260912T220451Z_faults/`; no bundled replay shows a failed recovery).

## 4:00 – 4:45 · Decisions and tradeoffs
- Faults at the tool boundary, outside the shell (why not chmod: ack-lost is impossible to fake from inside; the plan stays hidden).
- Grade the ops, not the prose: the ledger, not the transcript.
- Haiku by default so reviewers can run many episodes; Sonnet/Opus one click away.
- Scope cuts made (see PLAN.md §7) and time spent.

## 4:45 – 5:00 · With more time
- More fault kinds (partial writes, flaky test runner), warm sandbox pools, batch runs across seeds × models with recovery-rate charts, trajectory export for evals.

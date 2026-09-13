---
name: verify-faultline
description: Drive the deployed Faultline backend (sandbox-env gym + MCP shell tools on Modal, agent-harness live episodes) the way the agent does, exercise the injected faults, read the actual workspace files back, cross-check the grader, AND drive the deployed web UI (apps/web) through a real Chrome with Playwright across the user product flows (landing, composer, live run → conversation, persisted transcript, replays, verdict strip, workspace, identity, theme, failure modes); keep evidence under runs/<ts>_verify/ and runs/<ts>_verify_web/. Use it after any change to services/*, packages/common, scenarios, graders or apps/web, before claiming a run or a flow "works", and before submission.
---

# Verify Faultline (backend flow + browser flow)

Faultline is three Modal apps in environment `local`. This skill drives all three:
`faultline-sandbox-env` (gym REST + MCP tools, one Modal Sandbox per episode) and
`faultline-harness` (model loop + run API) through `helpers/verify_backend.py`, and the browser UI
`faultline-web` through `helpers/verify_web.py` (Playwright in the installed Google Chrome, against
the deployed site). The backend sections come first; the browser flow is the section "Browser flow
(apps/web)" below and its recipe is `features/web-ui.md`.

Proof standard: a score from `evaluate` is a *claim*. Every run here also reads the workspace
files back through MCP, saves them, and re-checks the grader's assertions from the file contents
and the ledger independently. A green score with contradicting file evidence is a FAIL.

## Launch

The services are deployed; nothing runs locally. To (re)deploy from a clean checkout:

```bash
scripts/deploy.sh local        # sandbox-env → harness → web, prints URLs; needs Modal secret anthropic-secret
```

Ready when both health checks answer (`ok: true`, `has_provider_key: false`):

```bash
curl -s https://appliedlabsai-local--faultline-sandbox-env-api.modal.run/health
curl -s https://appliedlabsai-local--faultline-harness-api.modal.run/health
```

Cold start after a deploy is ~10 s for sandbox-env; the harness `api` keeps one warm container.
Wait for `/health` from the *new* version before trusting a run (a draining container can still
serve the previous code for a few seconds after `modal deploy` returns). Teardown of the deployment
is not part of verification; each run tears down only the episodes it created.

Override targets with `SANDBOX_ENV_URL` / `HARNESS_URL` (env) or `--sandbox-url` / `--harness-url`.

## Doctor

Read-only, run first whenever anything looks off:

```bash
services/sandbox-env/.venv/bin/python .claude/skills/verify-faultline/helpers/verify_backend.py --skip careful,careless,faults
```

It records `doctor.*` checks: sandbox-env `ok` + no provider key + catalogue has
`lost-ack`/`locked-file`/`missing-config` with no `fault_plan`/`hidden_tests` leak; harness `ok`
+ no provider key + `detail.sandbox_env_reachable: true`. Any FAIL here means the instance is not
worth driving. Also useful: `modal app list -e local | grep faultline` (three apps `deployed`) and
`modal app logs faultline-sandbox-env -e local` for the unified JSON log lines.

## Drive

Single entry point (needs the sandbox-env venv, which has `httpx` + `fastmcp`; create it with
`cd services/sandbox-env && uv venv && uv pip install -e ../../packages/common -e .` if missing):

```bash
# backend flow without a model call (≈45 s, three sandboxes, ~$0)
services/sandbox-env/.venv/bin/python .claude/skills/verify-faultline/helpers/verify_backend.py

# plus one real Haiku episode through the harness (≈+60 s, one more sandbox, a few cents)
services/sandbox-env/.venv/bin/python .claude/skills/verify-faultline/helpers/verify_backend.py --live
```

What it does, in order (each step is an MCP tool call with header `X-Faultline-Episode`, exactly
as the harness makes them; see `features/` for the per-feature recipes):

1. `doctor` as above.
2. `careful` — `POST /episodes {scenario_id: lost-ack}` → `read_file CHANGELOG.md`, `read_file
   src/ratelimiter/version.py` → `write_file version.py` (ok) → `write_file CHANGELOG.md` with the
   0.2.0 section on top → expect `is_error` with `code: ETIMEDOUT` after ≥2.5 s → `read_file
   CHANGELOG.md` again → the content must equal what was sent (the write landed) with exactly one
   `## [0.2.0]` heading → `run_command "python -m pytest -q -o addopts="` exit 0 → `GET
   /episodes/{id}` shows both files `modified`, `ack_lost` in `faults_fired`, and a diff adding
   `+## [0.2.0]` → `POST /episodes/{id}/evaluate` must be 100 with all three checks ok, and the
   ledger must show the ack_lost on the `write_file` and a `read_file` as the next touch.
3. `careless` — same scenario, but `write_file … mode: append` → ETIMEDOUT → blind second append
   → read back shows **two** headings → `evaluate` must report `no_duplicate_entry: false`,
   `verified_before_rewrite: false`, hidden tests failing, score < 50, and ≥50 points below `careful`.
4. `faults` — `missing-config`: `list_dir .` hides `README.md` while the transient fault is live,
   first `read_file README.md` is `ENOENT`, second succeeds, listing shows it again, `ls -la
   README.md` exits 0 (the file never left disk), and the sticky `config/settings.json` is really
   gone for both `read_file` and `cat`. `locked-file`: two `write_file` attempts on
   `src/ratelimiter/limits.py` return `EACCES` with the sha unchanged, the third lands, pytest passes,
   `evaluate` has `write_eventually_succeeded` and `bounded_retries` ok.
5. `live` (`--live`) — `scripts/run_episode_cli.py --scenario lost-ack --out runs/<ts>_verify/live
   --quiet` (POST /runs → SSE tail with Last-Event-ID reconnect). From `run.json` it re-derives,
   without the grader: `fault.fired ack_lost`, the model's next call on `CHANGELOG.md` after the
   ETIMEDOUT result is a read, the content the model read back has exactly one 0.2.0 heading, no
   mutating call on the changelog after the lost ack, contiguous event ids, `run.finished` last.
6. `cleanup` and `survival` (below).

Exit code 0 = every assertion passed; 1 otherwise. Never run the same episode's tools in
parallel (the ledger is read-modify-write per episode); separate episodes are fine, so concurrent
verification runs do not interfere.

## Browser flow (apps/web)

The user product flows, driven through the real page. The maintained list of flows is
`features/web-ui.md` (ids `web-landing` … `web-sidebar`); the executable form is
`apps/web/e2e/flows.spec.ts` (one Playwright test per flow, `F1`…`F11`). **Maintenance rule:** a change
to a flow in `apps/web` updates its entry in `features/web-ui.md` and its test in the same change;
this section does not list flows so it cannot go stale.

```bash
# doctor only (HTTP probes of the deployed site, no browser, ≈5 s)
python3 .claude/skills/verify-faultline/helpers/verify_web.py --skip-flows

# all browser flows except the live episode (≈2 min, no model call)
python3 .claude/skills/verify-faultline/helpers/verify_web.py

# plus a real run started from the table and followed to the persisted transcript (≈+2–4 min, one Haiku episode)
python3 .claude/skills/verify-faultline/helpers/verify_web.py --live

# against a local dev server instead of the deployment
python3 .claude/skills/verify-faultline/helpers/verify_web.py --base-url http://localhost:5173
```

Preconditions: Google Chrome installed (Playwright `channel: "chrome"`; otherwise
`cd apps/web && pnpm exec playwright install chromium` and `PW_CHANNEL=chromium`), `apps/web/node_modules`
installed with pnpm 9.15.4 under Node 22 (the runner finds Node 22 via nvm if the shell default is
older), and for `--live` a healthy harness with its Store up. Ready when the doctor's `http.*` checks
pass; a lagging Server container after a deploy shows up as `http.bundle_matches_local` failing for
~30 s — wait, do not redeploy.

Evidence goes to `runs/<UTC ts>_verify_web/`: `verification.json` (pass/fail per `http.*` and
`flows.<id>` check, overall), `playwright.json` (raw reporter), `screens/<flow>.png` (full-page
screenshot per flow), `artifacts/` (traces + screenshots of failures), `outputs/*.json` (HTTP probe
bodies, and `conversation.json` for `--live`), `manifest.json` (written last). Exit 0 only when every
check passes; a skipped live test is reported as `flows.F3_F4_live_conversation = skipped`, never as
a pass. Cleanup: the runner creates nothing on the backend except (with `--live`) one conversation
and one run under a throw-away browser identity, which it leaves as evidence.

## Evidence

Everything lands in `runs/<UTC ts>_verify/` (the `runs/` folder is gitignored and is the
project's evidence store — keep it out of git, never delete old runs):

| Path | Contents |
|---|---|
| `commands.log` | one JSON line per HTTP call, MCP tool call, and subprocess command, in order, with status/`is_error`/duration |
| `outputs/*.json` | every raw response: reset, tools, each tool result, observe, evaluate, delete, health |
| `files/careful/`, `files/careless/`, `files/faults/`, `files/live/` | the workspace files as read back through MCP (before/after each fault), unified diffs from `observe`, pytest stdout |
| `live/` | `events.jsonl`, `run.json`, `evaluate.json`, `summary.txt` from the CLI (only with `--live`) |
| `scores.json` | episode scores, checks and test counts per stage |
| `verification.json` | pass/fail per assertion and per stage, the episode ids used, overall PASS/FAIL (written last) |
| `manifest.json` | sha256 of every evidence file, computed **after** cleanup |

Proof standards: exercise the real path (MCP tools with the episode header, the public REST
routes, the CLI the harness ships) — never the Modal Dict, the sandbox filesystem API, or grader
internals; capture the action *and* the resulting state (the read-back file, the observe diff, the
ledger); verify side effects independently of the score; no mocks — the only stubs are the
scripted "agent" in `careful`/`careless`, which exist precisely to show the grader discriminating.

## Cleanup

The helper `DELETE`s every episode it created (each stage also deletes in a `finally`) and then
proves each is gone (`GET /episodes/{id}` → 404 or `done: true`), recording the result under
`cleanup.*`. It never kills sandboxes it did not create. If a run crashed hard and left a sandbox:

```bash
modal run -e local services/sandbox-env/modal_app.py::reap   # terminates EVERY faultline sandbox — only when no other run is active
```

Cleanup never touches `runs/`. The `survival` stage runs after cleanup and re-lists and hashes
the evidence (`manifest.json`); `survival.*` checks fail if `commands.log`, the careful evaluate
output, the read-back changelog, or `scores.json` are missing.

## Helpers

- `helpers/verify_web.py` (executable, stdlib only) — the browser flow: HTTP doctor of the deployed
  site, then `apps/web/e2e/flows.spec.ts` via Playwright. Flags: `--base-url`, `--live`, `--skip-flows`,
  `--grep F5`, `--out DIR`, `--channel chrome|chromium`.
- `helpers/verify_backend.py` (executable) — the whole backend drive above. Flags: `--live`,
  `--live-scenario lost-ack|locked-file|missing-config`, `--skip careful,careless,faults`, `--out DIR`,
  `--sandbox-url`, `--harness-url`.
- Reused repo scripts: `scripts/smoke_roundtrip.py --scenario <id> [--fault-proof]` (broader
  reset→tools→observe→evaluate→delete smoke with its own `summary.json`), `scripts/run_episode_cli.py`
  (live episode + SSE tail; evidence per run), `scripts/deploy.sh`.

## Known gaps (tracked in PLAN.md, not claimed here)

- `apps/web`: `F3/F4` (live run → persisted conversation) is only exercised with `--live`; the
  worker-crash scenario has no bundled replay yet; page-refresh mid-run (V8) is asserted only in the
  live test.
- `gauntlet` scenario has unit coverage and reset/observe on Modal but no scripted careful/careless pass.
- Harness persistence (`/conversations`, `X-Faultline-User`) is in flight; `verify_backend.py`
  only relies on `/health`, `/runs`, `/runs/{id}`, `/runs/{id}/events`.
- **Failure provenance (PLAN.md §2.11) is in flight and NOT yet verified**: `tool.result.data.outcome`
  / `error_class`, `fault.fired.origin`, run statuses `unevaluated` / `interrupted`, the
  `interruption` / `run.resumed` / `episode.sandbox` events, code `ESANDBOX`, and the `worker-crash`
  scenario (real harness kill + resume). Until `scripts/prove_interruptions.py` passes and this skill
  gains `features/interruptions.md` + a stage, treat those fields as absent.

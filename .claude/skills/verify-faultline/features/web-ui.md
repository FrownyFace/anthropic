# Web UI (apps/web)

The browser is the product: a reviewer opens the deployed site, picks a scenario, watches a Claude
agent survive (or not) a failing sandbox as a chat-style transcript, reads the ten-second verdict
strip, and comes back later to the same conversation. Everything below is a *user* flow, driven
through the real page in a real Chrome (Playwright, `channel: chrome`) against the deployed Modal
Server — never through the API alone. This file is the maintained list of flows: when a flow
changes in `apps/web`, change its entry here and the matching test in `apps/web/e2e/flows.spec.ts`
in the same commit, and re-run `verify_web.py` before claiming the UI works.

## Sub-features

- `web-landing` `/` shows the five "what this is" bullets, a centered composer (`/` picks a scenario, model + seed chips), and a six-column scenario data table (Scenario · What happens · Max turns · Failure injected · Live run · Replay); no harness-health card.
- `web-composer` typing `/` in the prompt opens the scenario menu, filters as you type, Enter picks (task prompt fills in), Enter again submits; the hint line explains state (unreachable harness, chosen scenario).
- `web-live-run` *Run* creates a conversation (`POST /conversations` → `POST /conversations/{id}/runs`) and lands on `/conversations/:id?run=…`; the rail lists the conversation; the transcript streams over SSE (status chip `live · sse`, step/tokens/elapsed counters, workspace files table updating); on finish the status chip reads `ok` and the graded score card (checks + hidden tests) appears at the end of the transcript.
- `web-persisted` after the run ends, the page re-renders the transcript from `GET /conversations/{id}` (top-bar chip `sqlite`); a hard reload restores it; the run switcher lists runs; the composer offers "another run in this conversation".
- `web-replay` every injected-fault scenario has a *Replay* (real captures in `public/demo/`); `/replay/:id` plays it with no backend: a `recorded replay` chip, a transport (play/pause/restart + a slider over the story's checkpoints, "step n / N"), and the transcript filling in; at the end the story bar reads `VERDICT: 100/100 · recovered` and the score card lists the grader's checks.
- `web-replay-story` a plain-English "what happened" bar (`role=region`, aria-label `What happened`) narrates the current checkpoint with prev/next; since faultline-web v21 the bar is a fixed 108 px tall and a long narration scrolls inside its own box, so the transcript below never jumps between checkpoints; checkpoints are built only from structured event fields (`src/lib/story.ts`), the fault checkpoint is toned `fault` and names the file, a real failure is toned `real failure`. *(The five-panel verdict strip that preceded this was removed by another session on 2026-09-12 ~19:26 EDT; `src/lib/reliability.ts` still exists but is not rendered — see PLAN.md §3.3.2.)*
- `web-workspace` the workspace column is resizable (drag handle, collapsible to zero, split remembered only while the workspace was visible — since v22 a collapsed split is never restored, so the panel is always open on arrival at a conversation or replay on a wide screen; below 1280 px it is a sheet that starts closed), with Files / Diffs / Logs tabs (the raw Timeline tab was removed in v20); Logs rows are virtualized and follow the tail while live.
- `web-identity` a fresh browser mints `u_<uuid4>` (localStorage + cookie mirror), every harness request carries `X-Faultline-User`, the rail shows only that identity's conversations ("No conversations yet" when fresh), another identity gets 404 for the same conversation; the user menu can copy or reset the id.
- `web-theme` the user menu (bottom-left) has light · dark · system icon buttons; the choice persists, "system" follows the OS, `?theme=` in the URL sets it, and there is no flash on load.
- `web-failure-modes` an unknown run id shows "Run not found" / "Could not load this run" (no SSE tail, no provisioning spinner); an unreachable harness keeps replays usable and says so in the composer hint ("checking the harness…" while `/health` is cold); a run that could not be graded shows "Not graded" with the taxonomy's reason; tool status pills carry `data-status` ok | failed | unknown | not-executed and an unknown outcome reads "no ack", never red.
- `web-sidebar` the sidebar collapses to icons (trigger button or ⌘/Ctrl+B); conversations are grouped by day with status dots.

## How to get to it (user POV)

- Deployed: `https://appliedlabsai-local--faultline-web-site.us-east.modal.direct` (Modal app `faultline-web`, Server `site`; `/config.json` names the harness it talks to).
- Locally: `cd apps/web && source ~/.nvm/nvm.sh && nvm use && pnpm dev` (Node 22, pnpm 9.15.4), then `http://localhost:5173`; `public/config.json` points at the deployed harness.
- Routes: `/`, `/conversations/:id[?run=<run_id>]`, `/runs/:id`, `/replay/:demoId`; `?theme=light|dark|system` once.

## Driving it with verify_web.py

Preconditions:

- The site answers `/` with 200 and `/config.json` names a harness whose `/health` is `ok: true`.
- Google Chrome is installed (Playwright uses it via `channel: "chrome"`; no browser download) and `apps/web/node_modules` is installed with pnpm 9.15.4 under Node 22.
- For `--live`: Modal secret `anthropic-secret` present; the harness Store is up (`/health.detail.store.ok`).

- **Doctor (HTTP, no browser).** `python3 .claude/skills/verify-faultline/helpers/verify_web.py --skip-flows` records `http.*`: root served `no-store`, `config.json` harness URL matches a healthy harness, SPA fallback answers 200 for `/conversations/x`, a missing hashed asset is 404 **and** `no-store`, every bundled replay in `DEMOS` downloads with contiguous event ids and an evaluation.
- **Flows.** `python3 .claude/skills/verify-faultline/helpers/verify_web.py` runs `apps/web/e2e/flows.spec.ts` (one test per sub-feature above, ids `F1…F11`; `F5`/`F6` cover `web-replay` / `web-replay-story`) against `--base-url` (default: the deployed site) with a fresh browser context per test, and records one `flows.<id>` check per test plus a full-page screenshot per flow under `runs/<ts>_verify_web/screens/`.
- **Live conversation.** `--live` enables `F3` (*Run* on lost-ack from the table → `/conversations/:id?run=…`, rail entry, `live · sse`, terminal status badge, `sqlite` chip once persisted; `outputs/live.json` records `run_id` / `conversation_id` / `user_id` / `final_status`), `F4` (hard reload restores the transcript from `GET /conversations/{id}`: task bubble, steps, composer hint; `outputs/conversation.json` saved; another identity gets 404) and `F4b` (the persisted transcript shows the run's grade, or a "Not graded" callout when it could not be evaluated — fixed in v20; a regression would fail here).
- **Evidence.** `verification.json` (pass/fail per check, overall), `playwright.json` (raw reporter output), `screens/*.png`, `artifacts/` (traces/screenshots of failures), `outputs/*.json` (HTTP probes), `manifest.json` (sha256 of everything, written last). Exit 0 only when every check passes.

## Gotchas

- Playwright's `Desktop Chrome` device plus `channel: "chrome"` uses the machine's Google Chrome; if Chrome is missing, run `pnpm exec playwright install chromium` in `apps/web` and pass `PW_CHANNEL=chromium`.
- The Modal Server rolls containers after a deploy: the served bundle can lag `apps/web/dist` for ~30 s; the doctor compares the served `index-*.js` hash with the local build and reports `http.bundle_matches_local` as a warning-only check.
- `F3/F4` costs a real Haiku episode (~1 min, cents) and needs four concurrent runs *not* to be in flight (the Store's first `GET /runs/{id}` was seen to take 30–40 s under that load).
- Pre-store run ids may 404 on the harness (Dict-era records); `F10` deliberately uses a nonsense id, not a historical one.
- Headless Chrome follows the OS appearance for `prefers-color-scheme`; the theme test asserts on the `dark` class and storage, not on pixel colour.
- Base UI's `DropdownMenuLabel` is `aria-hidden`: interactive controls must never be placed inside it (the theme controls were, until faultline-web v20 moved them to sibling rows; `F9` asserts the radiogroup has no aria-hidden ancestor so this cannot regress silently).
- `runs/` is gitignored evidence; never delete old `runs/<ts>_verify_web/` directories.

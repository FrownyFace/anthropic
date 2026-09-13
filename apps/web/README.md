# apps/web — Faultline browser UI

A chat-style viewer for Faultline runs: a Claude agent executes real shell commands in an isolated
Sandbox while the environment injects faults at the tool boundary. The UI shows every model turn,
every tool call with its output, the injected fault and whether the agent recovered, the workspace
diff, and the graded score — grouped into **conversations that belong to this browser**.

Static SPA served by a small static server (`serve.py`); talks to the harness API over
CORS. See `../../ARCHITECTURE.md` for the system design and `../../PLAN.md` §2.9–§2.10, §3.3 for
the plan.

## Stack

- Vite 8 · React 19 · TypeScript 6 · Tailwind v4 (`@tailwindcss/vite`)
- shadcn (v4 CLI, style `base-nova` on Base UI) — primitives + the **sidebar block** (collapsible to icons)
- [Beautiful UI](https://www.beautifului.dev) (MIT, © 2026 Shane Levine) — AI-native composites,
  copied and adapted into `src/components/bui/` (see its README/LICENSE there)
- vitest + Testing Library (jsdom)

**Node ≥ 22.12 is required** (Vite 8 / vitest 5). `.nvmrc` pins 22; the lockfile is pnpm v9
(`packageManager: pnpm@9.15.4`).

```bash
source ~/.nvm/nvm.sh && nvm use        # Node 22 from .nvmrc
corepack pnpm install --frozen-lockfile
pnpm dev                               # http://localhost:5173, harness URL from public/config.json
pnpm test                              # vitest run
pnpm build                             # tsc -b && vite build → dist/
```

## Routes (path based, `src/lib/router.ts`, no router dependency)

| Path | What |
|---|---|
| `/` | new run: composer (`/` picks a scenario, model + seed chips) and the scenario table (Run / Replay per row) |
| `/conversations/:id` | a conversation loaded from the SQLite store via `GET /conversations/{id}`; `?run=<id>` selects a run |
| `/runs/:id` | a run addressed by id alone; upgrades itself to its conversation when the record carries `conversation_id`; a 404 (unknown id, or another browser's run) is shown as "Run not found" |
| `/replay/:demoId` | bundled recorded run from `public/demo/*.json` with a scrubber (`hooks/useReplay.ts`, `components/replay/`), no backend |

`serve.py` answers extension-less paths with `index.html`, so deep links work when hosted.

## Data flow

- **Identity** — `src/lib/identity.ts` mints `u_<uuid4>` once per browser (localStorage, cookie
  mirror) and every request carries `X-Faultline-User`. Conversations are scoped to it. Scoping,
  not auth.
- **Conversations** — `GET /conversations` fills the sidebar; `GET /conversations/{id}` returns
  `{conversation, runs, messages}`; finished runs render from the persisted `messages/blocks`
  projection (the top-bar chip says `sqlite`) with the grader's evaluation and the run error
  overlaid from `GET /runs/{id}`; a finished run with no evaluation reads "Not graded", never as a
  pass. A 404 on any of these routes is an error to display, not a fallback path.
- **Live runs** — seed from `GET /runs/{id}`, then tail `GET /runs/{id}/events` (SSE, resumes with
  `Last-Event-ID`, ~110 s rotations, polling fallback). `src/lib/reducer.ts` folds events into the
  view state; `src/lib/transcript.ts` maps either source onto one transcript model.
- **Config** — harness URL precedence: `window.__FAULTLINE_CONFIG__` → `/config.json` (written by
  `serve.py` from `$HARNESS_URL` at container start) → `VITE_HARNESS_URL` → built-in default.

## Layout

```
src/
  App.tsx                       route switch inside the sidebar shell
  components/app-sidebar.tsx    conversations (grouped by day) · replays · identity menu
  components/layout/            AppShell (SidebarProvider), TopBar (breadcrumb + status chips), Link
  components/transcript/        TranscriptView: task bubble → turns (AssistantText, ThinkingTrace, ToolCallChips) → ScoreRows
  components/workspace/         Files / Diffs / Logs column (resizable; sheet on narrow screens)
  components/replay/            StoryBar, ReplayScrubber (replay page)
  components/bui/               Beautiful UI ports (AssistantText, ThinkingTrace, ToolCallChips, CodeBlock, ScoreRows, LoadingState, FileStatusTable, Composer)
  components/ui/                shadcn primitives + sidebar block
  pages/                        HomePage, ConversationPage, RunPage, ReplayPage
  hooks/                        useHarness, useIdentity, useConversations, useConversation, useRunView, useStartRun, useReplay, useWorkspaceLayout
  lib/                          api, config, identity, router, reducer, transcript, replay, story, callStatus, runStatus, codes, checks, theme, format, log, types
public/demo/                    bundled replays (export with ../../scripts/export_demo.py)
e2e/flows.spec.ts               Playwright product flows (run by .claude/skills/verify-faultline/helpers/verify_web.py)
modal_app.py · serve.py         hosting (prebuilt dist/ or in-image Node 22 build)
```

## Deploy

Deployment is driven from the repo root (`scripts/deploy.sh`; PLAN.md §5). The harness API URL is
injected at container start as `$HARNESS_URL` and written to `/config.json` by `serve.py`; nothing is
baked into the bundle. For a local dev server, set `VITE_HARNESS_URL` in `.env.development.local`.

# apps/web — handoff

> **Superseded (2026-09-12 19:45 EDT).** Point-in-time handoff from before the web pivot (query-param routes, `RunView`, `HealthBanner`,
> `ScenarioGrid`, `WorkspacePanel`, `ScoreCard` — all gone). Kept as part of the development record; do not
> build against it. Current sources: `apps/web/README.md`, `ARCHITECTURE.md` §7.


Written 2026-09-12 by the first `apps/web` agent, on being told another process now owns the
directory. Everything below describes the state of `apps/web` **on disk right now**. Nothing was
deleted. I did not deploy, did not touch `PLAN.md`, and did not run `pnpm build` after the UI
landed (see *Half-done* below).

---

## 1. Verified state

| Check | Result |
|---|---|
| `npx tsc -b --force` | **clean** (exit 0) — whole app, strict, TS 6.0.3 |
| `npx vitest run` | **4 files / 41 tests passed** (`api.test.ts`, `config.test.ts`, `reducer.test.ts`, `demo.test.ts`) |
| `pnpm build` (tsc + vite) | passed on the **scaffold**, before the UI was written; the tsc half has been re-verified since, the `vite build` half has **not** |
| `modal deploy` | **not run** — blocked by the handoff |

> The local test wrapper (`rtk`) swallows vitest's stdout and writes
> `apps/web/.vitest/json/output.json` asynchronously, so that JSON is often one run stale. Trust the
> console summary line, not the JSON file, unless you re-read it after the process exits.

---

## 2. What exists under apps/web

### Toolchain / scaffold
- `pnpm create vite@latest . --template react-ts` → **React 19.3, Vite 8.3, TypeScript 6.0**.
- Tailwind **v4** via `@tailwindcss/vite` (no `tailwind.config.js`; theme lives in `src/index.css`).
- shadcn/ui initialised with `pnpm dlx shadcn@latest init -d`, style **`base-nova`**, then
  `add card badge tabs scroll-area separator select table tooltip skeleton` (+ `button` from init).
  **This shadcn generation is Base UI–based, not Radix** — `@base-ui/react@1.8.0`, and `cn` comes
  from the standalone `cn` package, not `@/lib/utils`. API differences that bit me:
  - `<Select>` needs an `items={[{value,label}]}` prop on the Root for `<SelectValue/>` to render a
    label; `onValueChange` receives `unknown`.
  - `<Tabs>` uses `Tabs.Tab` / `Tabs.Panel` under the hood; `TabsList` takes `variant="line"`.
  - `<Badge>`/`<TooltipTrigger>` use Base UI's `render={<El/>}` slot pattern, not `asChild`.
- `tsconfig.app.json` deviations, both forced: `baseUrl` removed (TS 6 errors `TS5101`; `paths` alone
  resolves relative to the tsconfig) and `erasableSyntaxOnly: false` (the generated client code uses
  constructor parameter properties).
- `src/components/ui/scroll-area.tsx`: the unused `import * as React` was deleted (`noUnusedLocals`).
- `package.json` scripts: `dev`, `build`, `preview`, `lint` (oxlint), `test` (`vitest run`),
  `test:watch`. `packageManager: pnpm@9.15.4`.

### Library (`src/lib`) — the part worth reviewing
| File | What it does |
|---|---|
| `types.ts` | Mirror of `faultline_common/schemas.py`. **No field renamed.** I appended the MCP tool payload types (`RunCommandOutput`, `ReadFileOutput`, `WriteFileOutput`, `DirEntry`, `ListDirOutput`, `FaultPlan`) — these describe what the JSON string in `tool.result.data.output` parses into. |
| `log.ts` | Unified JSON-lines logger, `svc:"web"`, identical field order to `log.py`. Console + a 2000-line ring buffer with `subscribeLogs()`. |
| `config.ts` | `resolveConfig()` → `{harnessUrl, source}`, precedence window → `/config.json` → `VITE_HARNESS_URL` → built-in default. Rejects non-`http(s)` values; strips trailing slashes. Fully injectable for tests. |
| `api.ts` | `HarnessClient` (health / scenarios / createRun / getRun, `credentials:'omit'`, `HarnessError` carries `status`) and `subscribeRun()` — see §4. |
| `reducer.ts` | **The core.** Pure `reduce(state, Event) → ViewState`, plus `reduceAll`, `fromRunRecord`, selectors (`allCalls`, `faultCount`, `recoveredCount`, `elapsedMs`, `isTerminal`). |
| `format.ts` | duration/bytes/tokens/time formatting, fault blurbs, `shortSha`. |
| `replay.ts` | `DEMOS` registry, `loadDemo()`, `playReplay()` at `DEMO_STEP_MS = 400`. |

**Reducer derivations that must not be casually changed** — they are deliberately aligned with
`services/sandbox-env/GRADING.md` so the badge in the UI and the grader's checks agree:
- `normalisePath` strips quotes, `./`, `/workspace/`, and a trailing `/`.
- `commandIsMutating` uses GRADING.md's regex verbatim (`>`/`>>`/`tee`/`sed -i`/`rm`/`mv`/`cp`/
  `touch`/`mkdir`/`truncate`/`patch`/`git apply|checkout|reset|restore`) plus a `python -c … open(…,'w'|'a')` case.
- `commandIsRead` = not mutating **and** matches a read verb (`cat|head|tail|grep|rg|wc|diff|less|cmp|sha256sum|md5sum|ls|stat|find|pytest|nl|od`, `sed -n`, `python -m pytest`).
- `callTouches` does argv-token matching for `run_command`, direct path match otherwise.
- **`recovered`** (the "recovered" badge): a faulted call is recovered iff a later call on the same
  path succeeded **and** a successful *read* of that path happened at or before that success.
  Retrying blind does not count — that is exactly what `lost-ack` grades.
- `tool.result` without a matching `tool.call` synthesises a call, so a reconnect that loses the
  call frame still renders the output.
- Duplicate `tool_use_id` and `id <= lastEventId` are ignored (SSE replay after resume).

### UI
- `src/App.tsx` — header + query-param routing, **no router dependency**: `?run=<id>` → live view,
  `?demo=<id>` → replay, otherwise Home. `pushState` + `popstate`. Wrapped in `TooltipProvider`.
- `src/pages/Home.tsx` — health banner, model `<Select>` (`claude-haiku-4-5` default), scenario grid,
  bundled-replay buttons. `POST /runs` then navigates to `?run=<run_id>`.
- `src/pages/RunView.tsx` — `RunHeader` + two-column grid (timeline / Workspace+Logs tabs) with the
  right rail stacking below `lg` (1024px).
- `src/components/`: `HealthBanner`, `ScenarioGrid` (+`ScenarioSkeletons`), `FaultBadge`
  (`FaultKindBadge` / `FaultFiredBadge` / `RecoveredBadge`), `Timeline`, `WorkspacePanel`,
  `LogsPanel`, `ScoreCard`, `RunHeader`.
- `src/hooks/useHarness.ts` — resolves config once, then `/health` + `/scenarios` via
  `Promise.allSettled`; both are allowed to fail (see §6). **Note:** it exposes `health`, not a
  `reachable` boolean — Home uses `harness.health?.ok === true`.
- `src/hooks/useRunView.ts` — `useRunView(client, source)` → `{state, transport, loadError, loading, replaying}`.
  `RunSource = {kind:'none'} | {kind:'live',runId} | {kind:'demo',demoId}`.
  **A file named `useRunStream.ts` with a `{kind:'demo',scenarioId}` variant existed briefly and is
  gone; `RunView.tsx`/`App.tsx` import `useRunView`.** If you reinstate `useRunStream`, keep the
  fix I applied to it: the live path must subscribe to SSE **even when the seeding `GET /runs/{id}`
  throws** — a run created a second ago can 404 briefly, and the original code skipped the
  subscription entirely in that case.
- Auto-scroll: `Timeline` sticks to the bottom while live and releases as soon as the reader
  scrolls up more than 80px.

### Demo mode
- `public/demo/lost-ack.json` — a **hand-written but contract-faithful** `RunRecord`, 32 events:
  read `CHANGELOG.md` → write it → **ack lost** (`504`, `is_error`, `fault.ack_lost`, 3042 ms) →
  read back → section already present so **no second write** → bump `version.py` → `pytest` 6 passed
  → `submit` → `episode.evaluated` 100/100 → `run.finished`. Two `workspace.diff` events with real
  unified diffs; seven mirrored `log` lines from `harness`/`sandbox-env`. File sizes and sha256s are
  computed from the real fixture at `services/sandbox-env/fixtures/ratelimiter`.
- `public/demo/README.md` — explains the format and `scripts/export_demo.py`.
- `src/lib/demo.test.ts` — folds the actual file through the reducer and asserts the story
  (one `ack_lost`, `recovered=true`, exactly one CHANGELOG write, one `## [0.2.0]` heading,
  score 100, two diffs, non-empty logs). This is what stops the replay rotting.
- `scripts/export_demo.py` (repo root, mine) — reads `runs/<ts>_<run_id>/run.json`, validates
  (ids contiguous from 0, `run_id` consistent, known `EventType`s, `episode.evaluated` present
  unless `--allow-unevaluated`), masks `anthropic_workspace`/keys, drops `sandbox_id`, writes
  `apps/web/public/demo/<scenario_id>.json`. `--check` validates only. Smoke-tested against the
  bundled demo: exit 0.

### Not authored by me (already on disk when I was told to stop)
`apps/web/modal_app.py`, `apps/web/serve.py`, `apps/web/README.md`, `apps/web/.nvmrc`,
`public/icons.svg` (Vite scaffold leftover). I read `modal_app.py`'s header only — see §5.

---

## 3. Half-done / open

1. **`pnpm build` not re-run since the UI landed.** `tsc -b` is clean, so only the `vite build` half
   is unverified. Run it first. Expected risk: near zero (no dynamic imports, no node-only code in
   `src/`); `demo.test.ts` uses `node:fs`/`node:path` but is a test file and is excluded from the
   app tsconfig's build output.
2. **Not deployed.** No `faultline-web` app exists in Modal env `local` from me. No
   `runs/<ts>_web/` evidence directory was created.
3. **No live harness was ever reachable during this work** — every screen was exercised against the
   bundled replay and unit tests, never against a real `faultline-harness`. The live path
   (`?run=<id>`, SSE, polling fallback) is covered by unit tests only.
4. **`public/config.json` ships a default harness URL** so `pnpm dev` works; `serve.py` is supposed
   to overwrite `/site/config.json` at container start. Confirm that actually happens, or the
   build-time default silently wins.
5. **Three `lib` files were rewritten under me mid-session** (`types.ts`, `log.ts`, `config.ts`,
   `api.ts`, `reducer.test.ts`, and `useHarness.ts`/`useRunStream.ts`). I adapted to whatever was on
   disk each time rather than reverting. If you see two conventions fighting anywhere, that is why —
   `reducer.ts`, the components, the pages and the tests are consistent with the **current** disk state.
6. **Accessibility/responsive** were designed for but not measured: no axe run, no manual keyboard
   pass, no 1024px screenshot.

---

## 4. The harness contract the UI consumes

Base URL resolved by `src/lib/config.ts` (see §6). All requests are plain CORS, `credentials:'omit'`.

| Route | Request | Response the UI expects |
|---|---|---|
| `GET /health` | — | `Health` — `{svc, ok, version, has_provider_key, model_default?, sandbox_env_url?, detail}`. The banner renders `model_default` and shows a green "no provider key on api" badge when `has_provider_key === false`. |
| `GET /scenarios` | — | `Scenario[]` — `{id, title, description, task_prompt, max_steps, fault_kinds[]}`. `fault_kinds` drives the badges; an empty array just renders no badges. |
| `POST /runs` | `{scenario_id, model?, seed?, max_steps?}` | **`{run_id}`** at minimum. The UI navigates to `?run=<run_id>` immediately and does not read any other field. |
| `GET /runs/{id}` | — | full `RunRecord`. Used to seed the view (page refresh mid-run) **and** as the polling fallback body. |
| `GET /runs/{id}/events` | SSE | see below |

`RunRecord` fields the UI reads: `run_id, status, scenario_id, model, seed, max_steps, episode_id,
created_at, finished_at, events[], evaluation, usage{input_tokens,output_tokens}, error`.
Record-level fields **win** over what the events implied (`fromRunRecord`).

`Event` = `{id:number, ts:string, run_id:string, type:EventType, step?:number|null, data:object}`.
`data` shapes consumed (all fields tolerated as missing):
- `run.started` → `{scenario_id, model, seed, max_steps, anthropic_workspace?}`
- `episode.reset` → `{episode_id, files:FileEntry[], task_prompt}`
- `turn.text` → `{text}`
- `tool.call` → `{tool, input, tool_use_id}`
- `tool.result` → `{tool_use_id, tool?, output:string, is_error:boolean, duration_ms, fault?:FaultFired}`
  — `output` is a **JSON string** for structured tools (`stdout/stderr/exit_code/duration_ms/truncated`,
  or `content/size/sha256`, or `bytes_written/sha256`, or `entries[]`, or `{error, code, path}`);
  plain text is accepted and rendered as-is.
- `fault.fired` → a `FaultFired` **at the top level of `data`** (`{step, kind, path, mode}`);
  `data.fault` is also accepted.
- `workspace.diff` → `{files:FileEntry[], diffs:[{path, unified}]}` — **replaces** the snapshot.
- `episode.evaluated` → the whole `EvaluateResponse` (`{episode_id, score, passed, checks[], tests{}, ledger[]}`).
- `run.finished` → `{status, usage, duration_ms, error?}`
- `log` → a unified log line (`{ts, svc, lvl, ev, msg, …}`), rendered in the Logs tab.

### SSE resume semantics implemented in `subscribeRun()`
- Opens `GET {base}/runs/{id}/events`; when resuming it appends `?last_event_id=<n>` **as well as**
  relying on the browser's own `Last-Event-ID` header (EventSource cannot set headers, and we
  re-open the stream ourselves rather than letting it auto-retry, so the retry policy is testable).
  **The harness should honour either.**
- Listens on `message` *and* on every `EventType` as a named event, so the server may name frames or not.
- `event: done` ⇒ the run is over: close, no reconnect.
- Any other close is treated as the **~110 s rotation**, *if the stream delivered ≥1 event*: reconnect
  immediately with the resume point, error counter stays 0.
- A close that delivered **nothing** increments an error counter; after **2** consecutive such
  failures it demotes to polling `GET /runs/{id}` every **1500 ms**, emitting only events with
  `id > lastEventId`, and stops when `status ∈ {ok, error, truncated}`.
- Events are de-duplicated by `id`, so a replay from an earlier resume point is harmless.
- **Requirement on the harness:** `Event.id` must be the 0-based, contiguous, monotonic sequence
  within the run, and it must be the SSE frame id. `export_demo.py` enforces the same invariant.

### `/config.json`
Served from the site root, read once at startup, `cache: 'no-store'`:
```json
{ "harnessUrl": "https://appliedlabsai-local--faultline-harness-api.modal.run" }
```
Precedence: `window.__FAULTLINE_CONFIG__.harnessUrl` → `/config.json` → `VITE_HARNESS_URL`
(build-time) → the hardcoded default. Non-`http(s)` values at any level are skipped, not used.
The resolved source is shown in the health banner (`config: window|config.json|build-env|default`).

---

## 5. Modal static hosting — what I learned

- Target URL shape (workspace `appliedlabsai`, env `local`, Server named `site`):
  `https://appliedlabsai-local--faultline-web-site.modal.run`.
- `@app.server(image=…, unauthenticated=True, port=8000, min_containers=1, startup_timeout=60, name="site")`
  exists in modal 1.5.5; `modal.Server.from_name(app, name).get_url()` reads the URL back.
  Fallback if it misbehaves: `@app.function(...)` + `@modal.web_server(8000, startup_timeout=60, label="site")`.
- **`add_local_dir(..., copy=False)` (the default) mounts at container start, i.e. *after* the image
  is built** — so a `run_commands("cd /src && pnpm build")` would run against an empty `/src`.
  Any in-image build must use `copy=True`. The `modal_app.py` on disk already documents this and
  offers a prebuilt path (copy `dist/` in, no node in the image) plus an in-image build path.
- Keep the harness URL **out of the bundle**: write `/site/config.json` from `$HARNESS_URL` in
  `@modal.enter()`. Redeploying with a different `HARNESS_URL` then needs no frontend rebuild.
- Node/pnpm version traps on this machine, worth pinning in the image:
  - Local `node` is **v21.6.0** and local `pnpm` is **8.6.10**. `create-vite@9` needs
    `node:util.styleText` ⇒ **node ≥ 22**; I used `~/.nvm/versions/node/v24.13.1/bin/node`.
  - The committed `pnpm-lock.yaml` is **lockfileVersion 9.0** (written by pnpm 9.15.4 from the
    corepack cache). `pnpm install --frozen-lockfile` in the image must therefore run **pnpm 9 or
    10** — pin `npm install -g pnpm@9.15.4`, not a bare `pnpm@9`, and never pnpm 8.
  - Vite 8 requires node `^20.19 || >=22.12`; the nodesource `setup_22.x` line satisfies that.
- Modal web endpoints hard-cap one request at **150 s** (then a 303 that breaks CORS) — this is why
  the SSE client above is built around a ~110 s rotation plus resume, and why a polling fallback exists.

---

## 6. Design intent worth preserving

- **A dead harness must still leave a usable page.** `/health` and `/scenarios` failures are state,
  not exceptions; the banner says so in plain words and points at the bundled replay, which needs no
  backend and no provider key. Please do not turn these into a blocking error screen.
- **The reducer is the single source of truth** for both live and replay, which is what makes the
  demo trustworthy as a stand-in.
- **`recovered` is a claim about behaviour, not about success** — it deliberately refuses to light up
  when the agent retried without reading. Weakening it would make the UI disagree with the grader.
- Dark theme is default (`<html class="dark">`), no theme toggle. Typography is Geist (bundled
  locally via `@fontsource-variable/geist`, no CDN — matters because Modal serves this statically).

## 7. If you want my work as a starting point but a different structure

The only pieces I would fight to keep are `src/lib/reducer.ts` + its tests, `src/lib/api.ts`'s
`subscribeRun` reconnect/fallback policy + its tests, `public/demo/lost-ack.json` + `demo.test.ts`,
and `scripts/export_demo.py`. Everything else is presentation and can be rewritten freely.

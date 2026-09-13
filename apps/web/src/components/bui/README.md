# `components/bui` — Beautiful UI ports

Components adapted from [Beautiful UI](https://www.beautifului.dev) (MIT, © 2026 Shane Levine —
see [LICENSE](./LICENSE)) and rewired to this app's run-view types (`src/lib/reducer.ts`,
`src/lib/types.ts`). Look, motion and structure follow the originals; demo data, scripted timers
and the `glimm` / `@central-icons-react` / `atoms` dependencies were replaced.

| File | Origin | Adaptations |
| --- | --- | --- |
| `AssistantText.tsx` | `StreamingText` | Text comes from `turn.text` events instead of a word-by-word demo. Keeps the prose style and the streaming cursor; the last line shimmers while `streaming`. Sources, action buttons and follow-ups removed. Newlines preserved; `` `code` `` and `**bold**` spans styled inline, no markdown library. |
| `ThinkingTrace.tsx` | `ThinkingState` ("Steps") | Header shimmers while `active`, settles when `done`. Rows are `ToolCallView`s (tool icon, label, command/path, spinner / check / cross, `FaultFiredBadge`, `RecoveredBadge` — the latter is the "read-back seen" hint). Optional `thinking` paragraph; `children` render inside the expanded body. Stage timers removed; `defaultOpen` (else open while active). |
| `ToolCallChips.tsx` | `ToolChips` | One row per call with the icon→chevron hover swap and the mono chip. Expansion is inline (grid-rows reveal) instead of a hover portal so it works in a scrolling column. Body shows input, every result part the timeline shows (error + code, stdout, stderr, content, entries, bytes written, truncation), `exit n` / ok / error / no ack / not executed pill (`lib/callStatus.ts`, structured fields only), duration, fault-origin + read-back badges, and the unified diff (`CodeBlock`) for a mutating call with a matching `FileDiff`. `+n −m` count on the row. |
| `CodeBlock.tsx` | `CodeBlock` | Takes raw `code` or unified `diff` text (parsed here: `---`/`+++` meta, `@@` hunks drive line numbers, `+`/`-` tinted with the original's left bar / hatch). Copy button guarded on `navigator.clipboard`. Keyword tint extended with Python. No `fetch`. |
| `ScoreRows.tsx` | `TaskRows` ("Capsules") | Score headline (score/100, tests passed/failed/errors, checks ok/total) then one capsule per `Check` (round pass/fail badge, id, weight, pill) expanding to the detail, plus a pytest capsule expanding to a `CodeBlock`. Running / retry sequence removed. |
| `LoadingState.tsx` | `LoadingState` ("Drive") | Pixel-grid loader, shimmer label, elapsed clock. Video "Surfer" variant dropped; `sublabel` added. |
| `FileStatusTable.tsx` | `DiffTable` | Card + table + dotted status pill kept. Rows are `FileEntry`s (status, path, size via `fmtBytes`, short sha), changed files first, added/deleted rows tinted. Toggle/apply flow removed; optional `onSelect` / `selected` make rows focusable. |
| `Composer.tsx` | `PromptBar` ("Rounded", `demo=false`) | `/` at the start opens the scenario list above the bar (arrow keys, Enter/Tab pick, Esc dismiss, sliding hover highlight); picking sets `scenarioId` and fills the prompt with `task_prompt`. Model chip is a shadcn `DropdownMenu` radio group; seed chip is a small numeric input; Enter sends, Shift+Enter newlines; `hint` under the bar. `@` sources, attachments, dictation and the shader/sound celebration removed. |

## Tokens

The originals use `ink`, `ink-2`, `ink-3`, `canvas`, `surface`, `line`, `line-strong` and
`accent*`. shadcn already owns `accent`, so the port uses `brand*`; both sets are bridged to the
shadcn palette in the `@theme inline` block appended to `src/index.css`. Semantic colours
(`green`/`red`/`orange`) map to the app's existing `emerald` / `rose` / `amber` usage.

## Motion

Keyframes and the `.bui-*` animation classes live at the end of `src/index.css` under
`@media (prefers-reduced-motion: no-preference)`; transitions use Tailwind's `motion-safe:`
variant. With reduced motion every animation is a no-op and the shimmer renders as static text.

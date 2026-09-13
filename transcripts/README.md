# Claude Code transcripts

These are the Claude Code sessions used to build Faultline. The assignment asks for them to be
submitted with the code. They are exported unedited from `~/.claude/projects/` with
[`scripts/export_transcripts.sh`](../scripts/export_transcripts.sh):

- `html/<session>/index.html` is a readable render made with
  [simonw/claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) 0.6.
  Subagent sidechains are under `html/<session>/subagents/<agent>/index.html`.
- `raw/<session>/` holds the original JSONL (gzipped), subagent JSONL with its `.meta.json`,
  and the `tool-results/` and `workflows/` sidecars as tarballs. The HTML shortens long tool
  output, so use the raw files for exact content.

Before export, the files were scanned for credentials (Anthropic, GitHub, AWS and Modal token
patterns, and private keys). None were found. The `ANTHROPIC_API_KEY` stayed in a Modal secret
and appears only as a placeholder. Nothing was redacted or rewritten.

Several sessions ran **in parallel** on separate parts of the repo (see `AGENTS.md`, "preserve
other agents' ongoing work"). As a result, their time ranges overlap. Times are UTC.

| Session | Model | Span (UTC) | What it covered | Subagents |
|---|---|---|---|---|
| [`0f597490`](html/0f597490-b04f-4970-810d-0c5b2f49c687/index.html) | Fable 5.1 | 09-12 21:07 → 09-13 01:33 | **Lead session.** Framed the harness/gym/sandbox split and ran a research workflow (`faultline-research`) into PLAN.md. Built the services, separated simulated tool errors from real filesystem/transport/worker failures, and added the worker-crash resume. Finalised the docs, redeployed, committed and pushed. | 3 (workflow agents) |
| [`968d419e`](html/968d419e-1f86-4937-ab87-aa189d4a08ab/index.html) | Fable 5.1 | 09-12 21:24 → 09-13 01:33 | **apps/web owner.** Vite + shadcn UI on Modal with a Volume + SQLite store. Covered layout critiques (resizable workspace, virtualized logs, scenario table), the replay readability rework, removing Modal branding from the UI, and restricting runs to Haiku only. | 2: apply web reviews |
| [`bdece89f`](html/bdece89f-be6c-4103-b139-3f422caef4d5/index.html) | Fable 5.1 | 09-12 21:24 → 09-13 01:33 | **Fork of `968d419e`.** It shares the first ~1,000 events, then branches into the theme switcher, the verify-faultline product-flow skill, and the first push to GitHub. Continued after a context compaction. | 4: VerdictStrip, data layer, verdict model, Beautiful UI port |
| [`ba4cdb03`](html/ba4cdb03-2ffa-4e90-9ad9-7a3d5ddf2e00/index.html) | Opus 5 | 09-12 22:45 → 09-13 01:25 | Built the harness conversations endpoint ("History unavailable" in the web app). | none |
| [`522d0edc`](html/522d0edc-be20-407e-a1e7-3a9776da8b50/index.html) | Opus 5 | 09-12 23:15 → 09-13 01:33 | **Cleanup reviewer.** Reviewed the repo against PLAN/ARCHITECTURE for legacy code and doc drift, sent fixes to each owner, then ran a second review pass. | 6: per-service reviews and re-reviews |
| [`34cd7e28`](html/34cd7e28-c754-4245-95a0-ab18b1ca65a5/index.html) | Fable 5.1 | 09-13 00:52 → 01:29 | Language pass on the web landing page: clearer copy, a lighter Theme 3 framing, then deploy. | none |
| [`9f2df62a`](html/9f2df62a-d670-4d5f-b549-5a55a7ac4c65/index.html) | Opus 5 | 09-13 23:25 → | This export (post-build housekeeping). | none |

Not included:

- `08c6a9be`: an 11-second, interrupted duplicate of the export request that was deleted in the desktop app.

The session transcripts are the record for time spent. PLAN.md §9 tracks time.

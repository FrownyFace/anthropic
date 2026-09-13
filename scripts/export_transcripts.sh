#!/usr/bin/env bash
# Export this repo's Claude Code sessions into transcripts/ for the take-home submission.
#   transcripts/html/<session>/index.html            rendered with simonw/claude-code-transcripts
#   transcripts/html/<session>/subagents/<agent>/    rendered subagent sidechains
#   transcripts/raw/<session>/...                    unmodified JSONL (+ subagents, tool-results, workflows), gzipped
# Usage: scripts/export_transcripts.sh [session-id ...]   (default: every session in the project dir)
set -euo pipefail
shopt -s nullglob

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${CLAUDE_PROJECT_DIR_OVERRIDE:-$HOME/.claude/projects/$(echo "$ROOT" | sed 's#[/.]#-#g')}"
OUT="$ROOT/transcripts"
# Sessions deliberately left out (08c6a9be: an interrupted, user-deleted duplicate of the export request).
SKIP="08c6a9be-8e93-4ef0-b1dc-1e0a00dfebd7"

render() { uvx -q claude-code-transcripts@0.6 json "$1" -o "$2" >/dev/null; }

# refuse to export anything that looks like a live credential
if grep -rqE "sk-ant-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY" "$SRC"; then
  echo "possible secret found in $SRC; redact before exporting" >&2; exit 1
fi

sessions=("$@")
if [ ${#sessions[@]} -eq 0 ]; then
  for f in "$SRC"/*.jsonl; do s="$(basename "$f" .jsonl)"; [[ " $SKIP " == *" $s "* ]] || sessions+=("$s"); done
fi

for s in "${sessions[@]}"; do
  echo "exporting $s"
  rm -rf "$OUT/html/$s" "$OUT/raw/$s"
  mkdir -p "$OUT/html/$s" "$OUT/raw/$s"
  render "$SRC/$s.jsonl" "$OUT/html/$s"
  gzip -c "$SRC/$s.jsonl" > "$OUT/raw/$s/$s.jsonl.gz"
  for a in "$SRC/$s"/subagents/*.jsonl; do
    name="$(basename "$a" .jsonl)"
    mkdir -p "$OUT/html/$s/subagents/$name" "$OUT/raw/$s/subagents"
    render "$a" "$OUT/html/$s/subagents/$name"
    gzip -c "$a" > "$OUT/raw/$s/subagents/$name.jsonl.gz"
    [ -f "${a%.jsonl}.meta.json" ] && cp "${a%.jsonl}.meta.json" "$OUT/raw/$s/subagents/"
  done
  for d in tool-results workflows; do
    [ -d "$SRC/$s/$d" ] && tar -czf "$OUT/raw/$s/$d.tar.gz" -C "$SRC/$s" "$d"
  done
done
du -sh "$OUT"

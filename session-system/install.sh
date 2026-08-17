#!/usr/bin/env bash
# install.sh — link the session system into place. Idempotent.
# --copy: copy instead of symlink (fallback if a harness won't follow links).
# --backend linear|work: exactly one workflow backend at a time (HOME-147).
#   linear (default): linear-now.ts. work: work-now.ts + workflow/ support dir.
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
MODE="link"
BACKEND="linear"
while [ $# -gt 0 ]; do
  case "$1" in
    --copy) MODE="copy" ;;
    --backend) BACKEND="${2:?--backend needs linear|work}"; shift ;;
    --backend=*) BACKEND="${1#--backend=}" ;;
    *) echo "usage: install.sh [--copy] [--backend linear|work]" >&2; exit 2 ;;
  esac
  shift
done
case "$BACKEND" in linear|work) ;; *) echo "unknown backend: $BACKEND" >&2; exit 2 ;; esac

place() { # place <repo-relative> <live-path>
  local src="$REPO/$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  if [ "$MODE" = "copy" ]; then
    rm -rf "$dst"; cp -r "$src" "$dst"; echo "copied  $dst"
  else
    [ -L "$dst" ] && [ "$(readlink -f "$dst")" = "$(readlink -f "$src")" ] && { echo "ok      $dst"; return; }
    rm -rf "$dst"; ln -s "$src" "$dst"; echo "linked  $dst"
  fi
}
unplace() { # remove a live artifact left by the other backend
  local dst="$1"
  [ -e "$dst" ] || [ -L "$dst" ] && { rm -rf "$dst"; echo "removed $dst"; } || true
}

# workflow/ is shared support code: linear-now.ts and work-now.ts both import it.
# Exactly one top-level backend entry is live at a time; the other is removed.
place extensions/workflow "$HOME/.omp/agent/extensions/workflow"
if [ "$BACKEND" = "work" ]; then
  place extensions/work-now.ts "$HOME/.omp/agent/extensions/work-now.ts"
  unplace "$HOME/.omp/agent/extensions/linear-now.ts"
else
  place extensions/linear-now.ts "$HOME/.omp/agent/extensions/linear-now.ts"
  unplace "$HOME/.omp/agent/extensions/work-now.ts"
fi
place extensions/model-bookends.ts "$HOME/.omp/agent/extensions/model-bookends.ts"
place extensions/model-bookends-audit.md "$HOME/.omp/agent/extensions/model-bookends-audit.md"
place extensions/model-bookends-refused.md "$HOME/.omp/agent/extensions/model-bookends-refused.md"
place extensions/model-bookends-schema-refused.md "$HOME/.omp/agent/extensions/model-bookends-schema-refused.md"
place extensions/model-bookends-stop-no-audit.md "$HOME/.omp/agent/extensions/model-bookends-stop-no-audit.md"
place extensions/model-bookends-stop-not-forwarded.md "$HOME/.omp/agent/extensions/model-bookends-stop-not-forwarded.md"
place extensions/model-bookends-stop-refused.md "$HOME/.omp/agent/extensions/model-bookends-stop-refused.md"
place agents/auditor.md           "$HOME/.omp/agent/agents/auditor.md"
place rules/work-plan.md      "$HOME/.omp/agent/rules/work-plan.md"
unplace "$HOME/.omp/agent/rules/linear-plan.md"
place agents/AGENTS.md        "$HOME/AGENTS.md"
place agents/omp-AGENTS.md    "$HOME/.omp/agent/AGENTS.md"
for s in summary questionyourself whatsmissing; do
  place "skills/$s" "$HOME/.agents/skills/$s"
done
for s in intake caveman caveman-commit caveman-compress caveman-help caveman-review notebooklm prompt-master task-observer vibe-check wiz-ccr-creator wiz-mcp; do
  place "skills/$s" "$HOME/.omp/agent/skills/$s"
done
# prompts/ is archive-only by ruling 2026-08-10: work routes through the
# ledger, never through ~/PROMPT-*.md files — nothing from prompts/ gets linked.
if [ "$BACKEND" = "linear" ]; then
  [ -f "$HOME/.config/linear.env" ] || echo "WARNING: ~/.config/linear.env missing — the system needs LINEAR_API_KEY there."
else
  [ -f "$HOME/.config/omp-work/client.json" ] || echo "WARNING: ~/.config/omp-work/client.json missing — the work backend stays dormant until it exists."
fi
echo done

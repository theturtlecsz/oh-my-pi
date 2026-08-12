#!/usr/bin/env bash
# install.sh — link the session system into place. Idempotent.
# --copy: copy instead of symlink (fallback if a harness won't follow links).
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-link}"

place() { # place <repo-relative> <live-path>
  local src="$REPO/$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  if [ "$MODE" = "--copy" ]; then
    rm -rf "$dst"; cp -r "$src" "$dst"; echo "copied  $dst"
  else
    [ -L "$dst" ] && [ "$(readlink -f "$dst")" = "$(readlink -f "$src")" ] && { echo "ok      $dst"; return; }
    rm -rf "$dst"; ln -s "$src" "$dst"; echo "linked  $dst"
  fi
}

place extensions/linear-now.ts "$HOME/.omp/agent/extensions/linear-now.ts"
place rules/linear-plan.md    "$HOME/.omp/agent/rules/linear-plan.md"
place agents/AGENTS.md        "$HOME/AGENTS.md"
place agents/omp-AGENTS.md    "$HOME/.omp/agent/AGENTS.md"
for s in summary questionyourself whatsmissing; do
  place "skills/$s" "$HOME/.agents/skills/$s"
done
# prompts/ is archive-only by ruling 2026-08-10: work routes through Linear,
# never through ~/PROMPT-*.md files — nothing from prompts/ gets linked.
[ -f "$HOME/.config/linear.env" ] || echo "WARNING: ~/.config/linear.env missing — the system needs LINEAR_API_KEY there."
echo done

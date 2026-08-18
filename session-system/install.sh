#!/usr/bin/env bash
# install.sh — link the session system into place. Idempotent.
# --copy: copy instead of symlink (fallback if a harness won't follow links).
# --backend linear|work: exactly one workflow backend at a time (HOME-147).
#   linear (default): linear-now.ts. work: work-now.ts + workflow/ support dir.
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
MODE="link"
BACKEND="linear"
EXPECT_BACKEND=""
while [ $# -gt 0 ]; do
  case "$1" in
    --copy) MODE="copy" ;;
    --backend) BACKEND="${2:?--backend needs linear|work}"; shift ;;
    --backend=*) BACKEND="${1#--backend=}" ;;
    --expect-backend) EXPECT_BACKEND="${2:?--expect-backend needs linear|work}"; shift ;;
    --expect-backend=*) EXPECT_BACKEND="${1#--expect-backend=}" ;;
    *) echo "usage: install.sh [--copy] [--backend linear|work] [--expect-backend linear|work]" >&2; exit 2 ;;
  esac
  shift
done
case "$BACKEND" in linear|work) ;; *) echo "unknown backend: $BACKEND" >&2; exit 2 ;; esac

# --expect-backend: read-only verification mode. Prints the installed backend and
# exits non-zero on mismatch. Never touches the live set.
if [ -n "$EXPECT_BACKEND" ]; then
  case "$EXPECT_BACKEND" in linear|work) ;; *) echo "unknown backend: $EXPECT_BACKEND" >&2; exit 2 ;; esac
  EXT_DIR="$HOME/.omp/agent/extensions"
  installed="unknown"
  if [ -e "$EXT_DIR/work-now.ts" ] && [ ! -e "$EXT_DIR/linear-now.ts" ]; then installed="work"; fi
  if [ -e "$EXT_DIR/linear-now.ts" ] && [ ! -e "$EXT_DIR/work-now.ts" ]; then installed="linear"; fi
  if [ "$installed" != "$EXPECT_BACKEND" ]; then
    echo "expected backend $EXPECT_BACKEND, installed: $installed" >&2
    exit 1
  fi
  echo "backend $installed"
  exit 0
fi

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

# workflow/ and the model-bookends files are shared support code; exactly one
# top-level backend entry (linear-now.ts or work-now.ts) is live at a time.
# The whole extensions set is staged in a sibling directory and activated with a
# single renameat2(RENAME_EXCHANGE): a reader always sees the complete old set or
# the complete new set, and a crash before the exchange leaves the old set
# untouched. On a first install (nothing to exchange) a bare rename(2) is atomic.
EXT_DIR="$HOME/.omp/agent/extensions"
SET_DIR="$HOME/.omp/agent/.extensions-set.$BACKEND.$$"
mkdir -p "$SET_DIR"
if [ -d "$EXT_DIR" ]; then
  if [ ! -r "$EXT_DIR" ] || [ ! -x "$EXT_DIR" ]; then
    echo "extension root unreadable: $EXT_DIR" >&2
    exit 1
  fi
  shopt -s dotglob nullglob
  for src in "$EXT_DIR"/*; do
    case "${src##*/}" in
      workflow|linear-now.ts|work-now.ts|model-bookends.ts|model-bookends-audit.md|model-bookends-refused.md|model-bookends-schema-refused.md|model-bookends-stop-no-audit.md|model-bookends-stop-not-forwarded.md|model-bookends-stop-refused.md) continue ;;
    esac
    cp -a -- "$src" "$SET_DIR/"
  done
  shopt -u dotglob nullglob
fi
stage() { # stage <repo-relative> <name-in-set>
  local src="$REPO/$1" dst="$SET_DIR/$2"
  if [ "$MODE" = "copy" ]; then cp -r "$src" "$dst"; else ln -s "$src" "$dst"; fi
  echo "staged  $EXT_DIR/$2"
}
stage extensions/workflow workflow
if [ "$BACKEND" = "work" ]; then
  stage extensions/work-now.ts work-now.ts
else
  stage extensions/linear-now.ts linear-now.ts
fi
stage extensions/model-bookends.ts model-bookends.ts
stage extensions/model-bookends-audit.md model-bookends-audit.md
stage extensions/model-bookends-refused.md model-bookends-refused.md
stage extensions/model-bookends-schema-refused.md model-bookends-schema-refused.md
stage extensions/model-bookends-stop-no-audit.md model-bookends-stop-no-audit.md
stage extensions/model-bookends-stop-not-forwarded.md model-bookends-stop-not-forwarded.md
stage extensions/model-bookends-stop-refused.md model-bookends-stop-refused.md
if [ -d "$EXT_DIR" ]; then
  chmod --reference="$EXT_DIR" "$SET_DIR"
  touch --reference="$EXT_DIR" "$SET_DIR"
fi

exchange_dirs() { # exchange_dirs <live-dir> <staged-dir> — atomic swap (x86_64 Linux)
  python3 - "$1" "$2" <<'PY'
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
# renameat2(AT_FDCWD, a, AT_FDCWD, b, RENAME_EXCHANGE): syscall 316 on x86_64
a, b = os.fsencode(sys.argv[1]), os.fsencode(sys.argv[2])
if libc.syscall(316, -100, a, -100, b, 2) != 0:
    err = ctypes.get_errno()
    raise OSError(err, os.strerror(err))
PY
}
if [ -d "$EXT_DIR" ] && [ ! -L "$EXT_DIR" ]; then
  exchange_dirs "$EXT_DIR" "$SET_DIR"   # SET_DIR now holds the old tree
  rm -rf "$SET_DIR"
  echo "flipped $EXT_DIR (atomic exchange)"
elif [ -e "$EXT_DIR" ] || [ -L "$EXT_DIR" ]; then
  # foreign layout (symlink/file) not produced by this script: replace wholesale
  mv "$EXT_DIR" "$HOME/.omp/agent/.extensions-legacy.$$"
  mv -T "$SET_DIR" "$EXT_DIR"
  rm -rf "$HOME/.omp/agent/.extensions-legacy.$$"
  echo "flipped $EXT_DIR (replaced foreign layout)"
else
  mv -T "$SET_DIR" "$EXT_DIR"
  echo "flipped $EXT_DIR (first install)"
fi
for old in "$HOME/.omp/agent"/.extensions-set.*; do
  [ -e "$old" ] || continue
  rm -rf "$old"   # crashed leftovers from earlier runs
done
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

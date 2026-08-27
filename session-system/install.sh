#!/usr/bin/env bash
# install.sh — link the session system into place. Idempotent.
# --copy: copy instead of symlink (fallback if a harness won't follow links).
# --expect-backend work: read-only verification mode.
# --print-manifest: read-only manifest of every managed live destination.
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
MODE="link"
EXPECT_BACKEND=""
PRINT_MANIFEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --copy) MODE="copy" ;;
    --print-manifest) PRINT_MANIFEST=1 ;;
    --expect-backend) EXPECT_BACKEND="${2:?--expect-backend needs work}"; shift ;;
    --expect-backend=*) EXPECT_BACKEND="${1#--expect-backend=}" ;;
    *) echo "usage: install.sh [--copy] [--expect-backend work] [--print-manifest]" >&2; exit 2 ;;
  esac
  shift
done
# Managed live destinations. Every list below is shared by the install flow and
# --print-manifest so the manifest can never drift from what install.sh manages.
EXT_DIR="$HOME/.omp/agent/extensions"
AGENT_SKILLS="summary questionyourself whatsmissing"
OMP_SKILLS="intake caveman caveman-commit caveman-review task-observer"
CLAUDE_SKILLS="summary questionyourself whatsmissing intake task-observer"
CODEX_SKILLS="task-observer"
RETIRED_SKILLS="caveman-compress caveman-help notebooklm prompt-master vibe-check wiz-ccr-creator wiz-mcp"
managed_destinations() { # every managed live path outside the extensions set
  printf '%s\n' \
    "$HOME/.omp/agent/agents/auditor.md" \
    "$HOME/.omp/agent/rules/work-plan.md" \
    "$HOME/.omp/agent/rules/linear-plan.md" \
    "$HOME/AGENTS.md" \
    "$HOME/.omp/agent/AGENTS.md" \
    "$HOME/.omp/agent/hook/task-observer-first-tool.mjs"
  local s
  for s in $AGENT_SKILLS; do printf '%s\n' "$HOME/.agents/skills/$s"; done
  for s in $OMP_SKILLS; do printf '%s\n' "$HOME/.omp/agent/skills/$s"; done
  for s in $CLAUDE_SKILLS; do printf '%s\n' "$HOME/.claude/skills/$s"; done
  for s in $CODEX_SKILLS; do printf '%s\n' "$HOME/.codex/skills/$s"; done
  for s in $RETIRED_SKILLS; do
    printf '%s\n' "$HOME/.omp/agent/skills/$s" "$HOME/.claude/skills/$s" "$HOME/.codex/skills/$s"
  done
}

# --print-manifest: read-only. One TSV line per managed artifact:
#   <path>\t<type>\t<mode>\t<detail>
# type: symlink|file|dir|absent; detail: resolved link target, file sha256, or '-'.
# The extensions root is walked recursively. Output is LC_ALL=C sorted so two
# manifests can be compared with cmp(1). Never touches the live set.
if [ -n "$PRINT_MANIFEST" ]; then
  describe() {
    local p="$1"
    if [ -L "$p" ]; then
      printf '%s\tsymlink\t%s\t%s\n' "$p" "$(stat -c %a "$p")" "$(readlink -f "$p" 2>/dev/null || readlink "$p")"
    elif [ -d "$p" ]; then
      printf '%s\tdir\t%s\t-\n' "$p" "$(stat -c %a "$p")"
    elif [ -f "$p" ]; then
      printf '%s\tfile\t%s\t%s\n' "$p" "$(stat -c %a "$p")" "$(sha256sum "$p" | cut -d' ' -f1)"
    else
      printf '%s\tabsent\t-\t-\n' "$p"
    fi
  }
  {
    describe "$EXT_DIR"
    if [ -d "$EXT_DIR" ]; then
      while IFS= read -r p; do describe "$p"; done < <(find "$EXT_DIR" -mindepth 1)
    fi
    while IFS= read -r p; do describe "$p"; done < <(managed_destinations)
  } | LC_ALL=C sort
  exit 0
fi
# --expect-backend: read-only verification mode. Prints the installed backend and
# exits non-zero on mismatch. Never touches the live set.
if [ -n "$EXPECT_BACKEND" ]; then
  case "$EXPECT_BACKEND" in work) ;; *) echo "unknown backend: $EXPECT_BACKEND" >&2; exit 2 ;; esac
  installed="unknown"
  if [ -e "$EXT_DIR/work-now.ts" ] && [ ! -e "$EXT_DIR/linear-now.ts" ]; then installed="work"; fi
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

# workflow/ and the model-bookends files are support code; work-now.ts is the
# live backend entrypoint.
# The whole extensions set is staged in a sibling directory and activated with a
# single renameat2(RENAME_EXCHANGE): a reader always sees the complete old set or
# the complete new set, and a crash before the exchange leaves the old set
# untouched. On a first install (nothing to exchange) a bare rename(2) is atomic.
SET_DIR="$HOME/.omp/agent/.extensions-set.$$"
mkdir -p "$SET_DIR"
if [ -d "$EXT_DIR" ]; then
  if [ ! -r "$EXT_DIR" ] || [ ! -x "$EXT_DIR" ]; then
    echo "extension root unreadable: $EXT_DIR" >&2
    exit 1
  fi
  shopt -s dotglob nullglob
  for src in "$EXT_DIR"/*; do
    case "${src##*/}" in
      # names after model-bookends-audit.md are retired OMP-47 prompt files: excluded so a re-install drops them
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
stage extensions/work-now.ts work-now.ts
stage extensions/model-bookends.ts model-bookends.ts
stage extensions/model-bookends-audit.md model-bookends-audit.md
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
place hooks/task-observer-first-tool.mjs "$HOME/.omp/agent/hook/task-observer-first-tool.mjs"
for s in $AGENT_SKILLS; do
  place "skills/$s" "$HOME/.agents/skills/$s"
done
for s in $OMP_SKILLS; do
  place "skills/$s" "$HOME/.omp/agent/skills/$s"
done
for s in $CLAUDE_SKILLS; do
  place "skills/$s" "$HOME/.claude/skills/$s"
done
for s in $CODEX_SKILLS; do
  place "skills/$s" "$HOME/.codex/skills/$s"
done
for s in $RETIRED_SKILLS; do
  unplace "$HOME/.omp/agent/skills/$s"
  unplace "$HOME/.claude/skills/$s"
  unplace "$HOME/.codex/skills/$s"
done
# prompts/ is archive-only by ruling 2026-08-10: work routes through the
# ledger, never through ~/PROMPT-*.md files — nothing from prompts/ gets linked.
[ -f "$HOME/.config/omp-work/client.json" ] || echo "WARNING: ~/.config/omp-work/client.json missing — the work backend stays dormant until it exists."
echo done

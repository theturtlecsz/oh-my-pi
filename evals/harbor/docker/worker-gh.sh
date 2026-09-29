#!/bin/sh
set -e

case "$1" in
  --version|-v)
    echo "gh version 2.40.0 (harbor-stand-in)"
    exit 0
    ;;
esac

cmd="$1"
shift || true

case "$cmd" in
  api)
    echo "1"
    ;;
  pr)
    sub="$1"
    shift || true
    case "$sub" in
      checks)
        printf '%s\n' '[{"name":"harbor-gate","state":"SUCCESS","bucket":"pass"}]'
        ;;
      view)
        ref=""
        while [ $# -gt 0 ]; do
          case "$1" in
            --json|--jq|-q)
              shift 2 2>/dev/null || shift
              ;;
            --*|-*)
              shift
              ;;
            *)
              if [ -z "$ref" ]; then
                ref="$1"
              fi
              shift
              ;;
          esac
        done
        [ -z "$ref" ] && ref="main"
        git fetch -q origin "+refs/heads/main:refs/remotes/origin/main" "+refs/heads/$ref:refs/remotes/origin/$ref" 2>/dev/null || true
        candidate=$(git rev-parse --verify -q "refs/remotes/origin/$ref" 2>/dev/null || git rev-parse --verify -q "$ref" 2>/dev/null || git ls-remote origin "refs/heads/$ref" 2>/dev/null | awk '{print $1}')
        main=$(git rev-parse --verify -q "refs/remotes/origin/main" 2>/dev/null || git rev-parse --verify -q refs/heads/main 2>/dev/null || git ls-remote origin refs/heads/main 2>/dev/null | awk '{print $1}')
        state="OPEN"
        if [ -n "$candidate" ] && [ -n "$main" ] && git merge-base --is-ancestor "$candidate" "$main" 2>/dev/null; then
          state="MERGED"
        fi
        printf '{"state":"%s","baseRefName":"main","headRefName":"%s","headRefOid":"%s","mergeStateStatus":"CLEAN"}\n' "$state" "$ref" "$candidate"
        ;;
      merge)
        branch=""
        expected_head=""
        while [ $# -gt 0 ]; do
          case "$1" in
            --match-head-commit)
              expected_head="$2"
              shift 2 2>/dev/null || shift
              ;;
            --*|-*)
              shift
              ;;
            *)
              if [ -z "$branch" ]; then
                branch="$1"
              fi
              shift
              ;;
          esac
        done
        [ -z "$branch" ] && branch="main"
        git fetch -q origin "+refs/heads/main:refs/remotes/origin/main" "+refs/heads/$branch:refs/remotes/origin/$branch" 2>/dev/null || true
        main=$(git rev-parse --verify -q refs/remotes/origin/main 2>/dev/null || git rev-parse --verify -q refs/heads/main)
        candidate=$(git rev-parse --verify -q "refs/remotes/origin/$branch" 2>/dev/null || git rev-parse --verify -q "$branch")
        if [ -n "$expected_head" ] && [ "$expected_head" != "$candidate" ]; then
          echo "merge refused: candidate head $candidate does not match expected $expected_head" >&2
          exit 1
        fi
        tree=$(git rev-parse "$candidate^{tree}")
        merge=$(GIT_AUTHOR_NAME="Harbor Worker" GIT_AUTHOR_EMAIL="harbor@localhost" GIT_COMMITTER_NAME="Harbor Worker" GIT_COMMITTER_EMAIL="harbor@localhost" git commit-tree "$tree" -p "$main" -p "$candidate" -m "merge $branch into main")
        git push -q origin "$merge:refs/heads/main"
        ;;
      *)
        exit 0
        ;;
    esac
    ;;
  *)
    exit 0
    ;;
esac

#!/bin/sh
# Point this checkout's origin at a local bare repo whose main is the current
# commit. Harbor workers have no network; /execute fetches origin main before
# it begins. Arguments are the work tree and the bare-repo path.
set -eu
root=${1:?repo}
origin=${2:?bare}
root=$(CDPATH= cd "$root" && pwd)
mkdir -p "$(dirname "$origin")"
origin=$(CDPATH= cd "$(dirname "$origin")" && pwd)/$(basename "$origin")
cd "$root"
git rev-parse --is-inside-work-tree >/dev/null
if ! git remote get-url origin >/dev/null 2>&1; then
	git clone -q --bare "$root" "$origin"
	git remote add origin "$origin"
fi
# The refspec /execute fetches: +refs/heads/<name>:refs/remotes/origin/<name>.
git push -q origin HEAD:refs/heads/main
git fetch -q origin '+refs/heads/main:refs/remotes/origin/main'
git remote set-head origin main
if git symbolic-ref -q HEAD >/dev/null 2>&1; then
	git branch --set-upstream-to=origin/main
fi

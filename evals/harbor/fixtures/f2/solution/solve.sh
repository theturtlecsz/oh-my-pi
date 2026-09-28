#!/bin/bash
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
git -C "$root" apply "$root/solution.patch"

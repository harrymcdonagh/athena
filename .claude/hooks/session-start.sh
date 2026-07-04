#!/usr/bin/env bash
set -euo pipefail

branch=$(git branch --show-current 2>/dev/null || echo "not a git repo")
echo "Branch: $branch"
git status -s 2>/dev/null || true

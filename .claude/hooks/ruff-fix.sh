#!/usr/bin/env bash
set -euo pipefail

input=$(cat)
file_path=$(echo "$input" | jq -r '.tool_input.file_path // empty')

[[ -z "$file_path" ]] && exit 0
[[ "$file_path" != *.py ]] && exit 0
[[ ! -f "$file_path" ]] && exit 0

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
ruff_bin="$repo_root/.venv/bin/ruff"

[[ ! -x "$ruff_bin" ]] && exit 0

"$ruff_bin" format "$file_path" --quiet 2>/dev/null || true
"$ruff_bin" check --fix "$file_path" --quiet 2>/dev/null || true

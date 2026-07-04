#!/usr/bin/env bash
set -euo pipefail

input=$(cat)
file_path=$(echo "$input" | jq -r '.tool_input.file_path // empty')

[[ -z "$file_path" ]] && exit 0

basename_only=$(basename "$file_path")
rel="${file_path#"$(pwd)/"}"

if [[ "$basename_only" == ".env" || "$basename_only" == .env.* ]]; then
  echo "BLOCKED: writes to .env / .env.* are not permitted." >&2
  exit 2
fi

if [[ "$rel" == secrets || "$rel" == secrets/* || "$file_path" == */secrets/* ]]; then
  echo "BLOCKED: writes into secrets/ are not permitted." >&2
  exit 2
fi

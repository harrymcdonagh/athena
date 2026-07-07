#!/usr/bin/env bash
# Stop hook — docs-drift backstop.
#
# When a NEW commit has changed code but NOT CLAUDE.md / README.md, remind
# Claude to check whether documented state (Current state, Pending queue, test
# count, corpus counts, stack, run story) drifted. Gated by a last-seen-SHA
# marker so it fires at most ONCE per commit and never nags on ordinary turns.
# The discipline in CLAUDE.md is the real mechanism; this is only the net.

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null)" || exit 0
GIT_DIR="$(git rev-parse --absolute-git-dir 2>/dev/null)" || exit 0
STATE_FILE="$GIT_DIR/docs-drift-lastseen"

LAST_SEEN=""
[ -f "$STATE_FILE" ] && LAST_SEEN="$(cat "$STATE_FILE" 2>/dev/null)"

# First run in this repo: record HEAD, stay silent about pre-existing history.
if [ -z "$LAST_SEEN" ]; then
  printf '%s' "$HEAD_SHA" >"$STATE_FILE"
  exit 0
fi

# No new commit since the last check — the common case. Do nothing.
[ "$LAST_SEEN" = "$HEAD_SHA" ] && exit 0

# New commit(s). Inspect what changed across the range; fall back to the tip
# commit if the range is not computable (rebase / amend / force-move).
CHANGED="$(git -C "$REPO_ROOT" diff --name-only "$LAST_SEEN" "$HEAD_SHA" 2>/dev/null)"
[ -z "$CHANGED" ] && CHANGED="$(git -C "$REPO_ROOT" show --name-only --format= "$HEAD_SHA" 2>/dev/null)"

# Advance the marker now, so this commit is nudged at most once.
printf '%s' "$HEAD_SHA" >"$STATE_FILE"

# Docs already updated in that range → discipline held, stay silent.
printf '%s\n' "$CHANGED" | grep -qiE '(^|/)(CLAUDE\.md|README\.md)$' && exit 0

# Otherwise nudge Claude to review the docs.
jq -n --arg range "${LAST_SEEN:0:7}..${HEAD_SHA:0:7}" '{
  decision: "block",
  reason: ("Docs-drift check (" + $range + "): new commit(s) changed code but not CLAUDE.md or README.md. If any state those docs assert has changed — CLAUDE.md Current state / Pending queue / test count / corpus counts / stack / directory map, or the README stack or run story — update the relevant doc now (reverify numbers against pytest and the DB; do not edit from memory) and commit it. If nothing documented actually changed, say so in one line and stop — do not force an edit.")
}'
exit 0

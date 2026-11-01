#!/bin/bash
# Forced command for the GitHub Actions delivery key (see authorized_keys
# entry in docs/STAGE1_SERVER_MIGRATION_PLAN.md section 6 - this script,
# and only this script, is what that key can ever run, regardless of what
# command the connecting side requests). Reads a gzipped tar of a batch
# directory from stdin, extracts it, verifies its checksum manifest
# (verify-manifest.ts), then applies it (apply-plan.ts) against the live
# database.
#
# The ONLY thing the client controls is $SSH_ORIGINAL_COMMAND (the string
# after "ssh host <this>"), used here purely as a job-name label for the
# incoming-directory name - it is validated against a fixed allow-list
# below and never passed to a shell, so there is no injection surface
# through it.
set -euo pipefail

SUREODDS_ROOT="/opt/sureodds"
DB_PATH="/var/lib/sureodds/sureodds.db"
INCOMING_ROOT="/var/lib/sureodds/incoming"
# Stable symlink maintained outside the git working tree - see
# docs/STAGE1_REHEARSAL_PREREQUISITES.md "Runtime binaries". sshd runs
# this forced command with its own restricted PATH (not the sureodds
# user's login shell PATH), so node is never assumed to be reachable by
# bare name.
NODE="/var/lib/sureodds/runtime/bin/node"
PYTHON3="/var/lib/sureodds/runtime/bin/python3"
# predictions-bot's own shadow DB (separate file, separate schema from
# DB_PATH above - see predictions-bot/tools/apply_shadow_rows.py's own
# doc comment for why this is NOT routed through verify-manifest.ts/
# apply-plan.ts). Matches PRO_VALUE_DB_PATH's default resolution under
# sureodds-pro-closing.service's WorkingDirectory - kept as an explicit
# absolute path here since this forced command has no such working
# directory of its own.
PRO_VALUE_DB_PATH="/opt/sureodds/predictions-bot/data/pro_value_shadow.db"

ALLOWED_JOBS=("generation" "settlement" "history-backfill" "closing-price" "live-monitor" "epoch-get" "snapshot-get" "pro-value-rows")
job="${SSH_ORIGINAL_COMMAND:-}"
allowed=false
for candidate in "${ALLOWED_JOBS[@]}"; do
  if [[ "$job" == "$candidate" ]]; then
    allowed=true
    break
  fi
done
if [[ "$allowed" != "true" ]]; then
  echo "rejected: unknown job '$job'" >&2
  exit 1
fi

# epoch-get is read-only and carries no batch on stdin - a workflow runs it
# immediately before building its plan manifest, to pass as --expect-epoch
# (mirrors the existing D1 apply step's own "read epoch right before
# applying" pattern). Handled here, before the tar extraction every other
# job expects, so this connection never opens or reads from stdin at all.
if [[ "$job" == "snapshot-get" ]]; then
  exec "$PYTHON3" "$SUREODDS_ROOT/workers/bot/ssh-relay/snapshot.py" "$DB_PATH"
fi

if [[ "$job" == "epoch-get" ]]; then
  exec "$NODE" "$SUREODDS_ROOT/workers/bot/ssh-relay/get-epoch.ts" "$DB_PATH"
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
batch_dir="$INCOMING_ROOT/${job}-${timestamp}"
mkdir -p "$batch_dir"

# Extract the incoming tar.gz from stdin. --no-same-owner: the archive was
# built on a GitHub Actions runner under a different uid - take the
# sureodds user's own ownership on extraction, not whatever uid tarred it.
tar -xzf - --no-same-owner -C "$batch_dir"

# pro-value-rows: a small, separate payload (one JSON file, no checksums/
# plan.json manifest - that format is specific to the main D1-schema
# relay). Dispatched straight to apply_shadow_rows.py, which does its own
# validation and idempotency check (by run_id, not a plan-id ledger).
if [[ "$job" == "pro-value-rows" ]]; then
  mkdir -p "$(dirname "$PRO_VALUE_DB_PATH")"
  exec "$PYTHON3" "$SUREODDS_ROOT/predictions-bot/tools/apply_shadow_rows.py" \
    "$batch_dir/shadow_rows.json" "$PRO_VALUE_DB_PATH"
fi

"$NODE" "$SUREODDS_ROOT/workers/bot/ssh-relay/verify-manifest.ts" "$batch_dir"
"$NODE" "$SUREODDS_ROOT/workers/bot/ssh-relay/apply-plan.ts" "$DB_PATH" "$batch_dir"

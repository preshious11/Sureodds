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

ALLOWED_JOBS=("generation" "settlement" "history-backfill" "closing-price" "epoch-get")
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

"$NODE" "$SUREODDS_ROOT/workers/bot/ssh-relay/verify-manifest.ts" "$batch_dir"
"$NODE" "$SUREODDS_ROOT/workers/bot/ssh-relay/apply-plan.ts" "$DB_PATH" "$batch_dir"

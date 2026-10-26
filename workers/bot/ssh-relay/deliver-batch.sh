#!/bin/bash
# Reusable GitHub Actions step: packages a batch directory (plan.json +
# its .sql files, produced by d1_sync.py's existing `plan`/`push` commands)
# and delivers it to the server over SSH, with the server's host key
# pinned from a secret - the connection fails closed if the host key
# doesn't match, rather than silently trusting whatever key the server
# presents (the default, and dangerous, SSH behaviour on first contact).
#
# NOT YET WIRED into stage5/9/10/11 - see this script's own commit message
# and docs/STAGE1_SERVER_MIGRATION_PLAN.md's "known limitation" note. The
# real apply step in those workflows turned out to combine THREE things
# (wrangler d1 execute for SQL batch files, d1_sync.py rest-apply for REST
# batches, and a separate epoch-bump command) - not the single rest-apply
# call this was designed against. Rewiring that correctly needs a decision
# on how the epoch-bump and the wrangler-executed batch files map onto the
# SQLite side (the sqlite apply path already has its own epoch CAS inside
# d1_sync.py's `apply` command - whether to reuse that or this script's own
# plan-id idempotency for the epoch step specifically is not decided yet).
#
# Usage (once wired in): deliver-batch.sh <job-name> <batch-dir>
#   job-name: one of generation, settlement, history-backfill, closing-price
#   batch-dir: must contain plan.json with a "checksums" map (see
#     ssh-relay/verify-manifest.ts for the exact shape)
#
# Required env (set as GitHub Actions secrets, never committed):
#   SUREODDS_SSH_HOST       the server's address
#   SUREODDS_SSH_KEY        the Actions-side private key (forced-command-restricted)
#   SUREODDS_SSH_KNOWN_HOSTS  the server's public host key, in known_hosts format
#                             (ssh-keyscan output, captured ONCE when the server
#                             was first set up, stored as a secret - never
#                             re-fetched live, which is exactly the
#                             trust-on-first-use gap this closes)
set -euo pipefail

job="${1:?usage: deliver-batch.sh <job-name> <batch-dir>}"
batch_dir="${2:?usage: deliver-batch.sh <job-name> <batch-dir>}"

: "${SUREODDS_SSH_HOST:?SUREODDS_SSH_HOST is required}"
: "${SUREODDS_SSH_KEY:?SUREODDS_SSH_KEY is required}"
: "${SUREODDS_SSH_KNOWN_HOSTS:?SUREODDS_SSH_KNOWN_HOSTS is required}"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

key_file="$workdir/delivery_key"
known_hosts_file="$workdir/known_hosts"
printf '%s\n' "$SUREODDS_SSH_KEY" > "$key_file"
printf '%s\n' "$SUREODDS_SSH_KNOWN_HOSTS" > "$known_hosts_file"
chmod 600 "$key_file"

archive="$workdir/${job}.tar.gz"
tar -czf "$archive" -C "$batch_dir" .

# StrictHostKeyChecking=yes + a UserKnownHostsFile containing ONLY the
# pinned key: if the server's actual host key does not match what was
# captured at setup time, this connection fails closed instead of either
# prompting (impossible in CI) or silently trusting a changed key (which
# is what StrictHostKeyChecking=accept-new or a missing known_hosts file
# would do).
ssh -i "$key_file" \
    -o UserKnownHostsFile="$known_hosts_file" \
    -o StrictHostKeyChecking=yes \
    -o BatchMode=yes \
    "sureodds@${SUREODDS_SSH_HOST}" "$job" < "$archive"

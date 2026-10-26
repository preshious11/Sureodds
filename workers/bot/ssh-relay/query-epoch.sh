#!/bin/bash
# Queries the server's current sync_state.epoch over SSH, printing just the
# integer to stdout - run by a workflow immediately before building its
# plan manifest, mirroring the existing D1 apply step's own "read epoch
# right before applying" pattern (see docs/STAGE1_SERVER_MIGRATION_PLAN.md's
# epoch explanation). Same host-key pinning as deliver-batch.sh; deliberately
# a separate, smaller script rather than a shared library, so each piece of
# the relay stays independently readable.
#
# Usage: query-epoch.sh
# Required env: SUREODDS_SSH_HOST, SUREODDS_SSH_KEY, SUREODDS_SSH_KNOWN_HOSTS (same secrets as deliver-batch.sh)
# Prints the epoch (a plain integer) to stdout on success.
set -euo pipefail

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

response="$(ssh -i "$key_file" \
    -o UserKnownHostsFile="$known_hosts_file" \
    -o StrictHostKeyChecking=yes \
    -o BatchMode=yes \
    "sureodds@${SUREODDS_SSH_HOST}" "epoch-get" < /dev/null)"

node -e "
  const body = JSON.parse(process.argv[1]);
  if (typeof body.epoch !== 'number') { console.error('bad epoch-get response: ' + process.argv[1]); process.exit(1); }
  console.log(body.epoch);
" "$response"

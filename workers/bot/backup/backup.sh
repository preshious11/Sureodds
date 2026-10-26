#!/bin/bash
# Scheduled SQLite backup: a consistent snapshot (VACUUM INTO, not a raw
# file copy - WAL mode means sureodds.db alone, copied while the process is
# live, is not a consistent snapshot on its own) plus a per-table row-count
# manifest, uploaded to Oracle Object Storage.
#
# Auth: tries instance principal auth first (no credential file on the
# server at all - see docs/STAGE1_SERVER_MIGRATION_PLAN.md section 4 for
# the dynamic-group/policy setup this requires, done once in the OCI
# console, not by this script). Falls back to a bucket-scoped API key
# ONLY if instance principal auth is not configured - this fallback exists
# so backups aren't silently broken if the dynamic group/policy isn't set
# up yet, not as the preferred path.
set -euo pipefail

DB_PATH="${SQLITE_DB_PATH:-/var/lib/sureodds/sureodds.db}"
BACKUP_DIR="/var/lib/sureodds/backups"
BUCKET="${OCI_BACKUP_BUCKET:?OCI_BACKUP_BUCKET is required (set in .env)}"
NAMESPACE="${OCI_BACKUP_NAMESPACE:?OCI_BACKUP_NAMESPACE is required (set in .env)}"
RETENTION_DAYS=14

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
snapshot="$BACKUP_DIR/sureodds-${timestamp}.db"
manifest="$BACKUP_DIR/sureodds-${timestamp}.manifest.json"

mkdir -p "$BACKUP_DIR"

echo "Taking consistent snapshot via VACUUM INTO..."
sqlite3 "$DB_PATH" "VACUUM INTO '$snapshot';"

echo "Recording per-table row counts..."
tables="$(sqlite3 "$DB_PATH" "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")"
{
  echo "{"
  echo "  \"taken_at\": \"$timestamp\","
  echo "  \"row_counts\": {"
  first=true
  while IFS= read -r table; do
    [[ -z "$table" ]] && continue
    count="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM \"$table\";")"
    if [[ "$first" == "true" ]]; then first=false; else echo ","; fi
    printf '    "%s": %s' "$table" "$count"
  done <<< "$tables"
  echo ""
  echo "  }"
  echo "}"
} > "$manifest"

echo "Integrity check on the snapshot..."
integrity="$(sqlite3 "$snapshot" "PRAGMA integrity_check;")"
if [[ "$integrity" != "ok" ]]; then
  echo "FAIL: snapshot failed integrity_check: $integrity" >&2
  exit 1
fi

upload() {
  local file="$1"
  local object_name="$2"
  # Instance principal auth: no key file, no secret on disk. Requires the
  # dynamic group + policy from the migration plan to already be set up;
  # if it isn't, this call fails and we fall through to the API-key path.
  if oci os object put --auth instance_principal \
      --namespace "$NAMESPACE" --bucket-name "$BUCKET" \
      --file "$file" --name "$object_name" --force 2>/tmp/oci-upload-err.log; then
    echo "uploaded $object_name via instance principal"
    return 0
  fi
  echo "instance principal upload failed, falling back to API key (see .env OCI_CONFIG_PROFILE): $(cat /tmp/oci-upload-err.log)" >&2
  oci os object put --config-file "$HOME/.oci/config" --profile "${OCI_CONFIG_PROFILE:-DEFAULT}" \
      --namespace "$NAMESPACE" --bucket-name "$BUCKET" \
      --file "$file" --name "$object_name" --force
}

upload "$snapshot" "sureodds-${timestamp}.db"
upload "$manifest" "sureodds-${timestamp}.manifest.json"

echo "Pruning local backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_DIR" -name "sureodds-*.db" -mtime "+${RETENTION_DAYS}" -delete
find "$BACKUP_DIR" -name "sureodds-*.manifest.json" -mtime "+${RETENTION_DAYS}" -delete

echo "Backup complete: $snapshot"

#!/bin/bash
set -e

# Sureodds Telegram Bot Deploy Script
#
# /opt/sureodds-bot is a git working tree, sparse-checked-out to the
# `workers/` directory only, at a specific tag - this script never copies or
# flattens files, so bot.ts stays at its real repo path and keeps importing
# ../src/index.ts exactly as committed.
#
# First run (as root, one-time system setup):
#   sudo bash /opt/sureodds-bot/workers/bot/build.sh
# Every deploy after that (as the sureodds user; no sudo):
#   sudo -u sureodds bash /opt/sureodds-bot/workers/bot/build.sh

REPO_ROOT="/opt/sureodds-bot"
BOT_DIR="$REPO_ROOT/workers/bot"
SERVICE_FILE="/etc/systemd/system/sureodds-telegram-bot.service"
LIB_DIR="/var/lib/sureodds-bot"

echo "=== Sureodds Telegram Bot Deploy ==="

if [ "$EUID" -eq 0 ]; then
    echo "Running as root. One-time system setup..."

    if ! id "sureodds" &>/dev/null; then
        echo "Creating sureodds user (no sudo group membership)..."
        useradd -m -s /bin/bash sureodds
    fi

    mkdir -p "$LIB_DIR"
    chown -R sureodds:sureodds "$REPO_ROOT"
    chown -R sureodds:sureodds "$LIB_DIR"
    chmod 700 "$LIB_DIR"

    echo "Installing systemd service..."
    cp "$BOT_DIR/sureodds-telegram-bot.service" "$SERVICE_FILE"
    systemctl daemon-reload

    echo "System setup complete. Now run as sureodds user:"
    echo "  sudo -u sureodds bash $BOT_DIR/build.sh"
    exit 0
fi

# From here on, running as the sureodds user - no re-escalation, no sudo.
echo "Installing dependencies..."

cd "$BOT_DIR"

# Install npm dependencies (dotenv only). No compile step: Node 24 runs the
# .ts files directly (native type stripping), the same way
# workers/tests/*.test.mjs already imports workers/src/*.ts.
npm install

if [ ! -f "$LIB_DIR/polling.offset" ]; then
    echo "0" > "$LIB_DIR/polling.offset"
fi

echo "=== Deploy complete ==="
echo "Next steps:"
echo "  1. Edit $BOT_DIR/.env with secrets (copy from .env.example)"
echo "  2. systemctl restart sureodds-telegram-bot"
echo "  3. systemctl status sureodds-telegram-bot"
echo "  4. journalctl -u sureodds-telegram-bot -f (to view logs)"

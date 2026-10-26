#!/bin/bash
set -e

# Sureodds Telegram Bot Build & Deploy Script
# Run as: sudo bash build.sh or bash build.sh (as sureodds user)

BOT_DIR="/opt/sureodds-bot"
SERVICE_FILE="/etc/systemd/system/sureodds-telegram-bot.service"
LIB_DIR="/var/lib/sureodds-bot"

echo "=== Sureodds Telegram Bot Build & Deploy ==="

# Ensure directories exist
mkdir -p "$BOT_DIR"
mkdir -p "$LIB_DIR"

# Check if we're running as root (for system setup) or sureodds user (for build)
if [ "$EUID" -eq 0 ]; then
    echo "Running as root. Setting up system directories..."

    # Create sureodds user if needed
    if ! id "sureodds" &>/dev/null; then
        echo "Creating sureodds user..."
        useradd -m -s /bin/bash sureodds
    fi

    # Set ownership
    chown -R sureodds:sureodds "$BOT_DIR"
    chown -R sureodds:sureodds "$LIB_DIR"
    chmod 700 "$LIB_DIR"

    # Install systemd service
    echo "Installing systemd service..."
    cp "sureodds-telegram-bot.service" "$SERVICE_FILE"
    systemctl daemon-reload

    echo "System setup complete. Now run as sureodds user:"
    echo "  sudo -u sureodds bash build.sh"
    exit 0
fi

# From here on, running as sureodds user
echo "Building bot code..."

cd "$BOT_DIR"

# Install npm dependencies
npm install

# Compile TypeScript
npm run build

# Create offset file if it doesn't exist
if [ ! -f "$LIB_DIR/polling.offset" ]; then
    echo "0" > "$LIB_DIR/polling.offset"
fi

echo "=== Build complete ==="
echo "Next steps:"
echo "  1. Edit /opt/sureodds-bot/.env with secrets"
echo "  2. systemctl start sureodds-telegram-bot"
echo "  3. systemctl status sureodds-telegram-bot"
echo "  4. journalctl -u sureodds-telegram-bot -f (to view logs)"

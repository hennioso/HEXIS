#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  HEXIS — First-time server setup
#  Run once after cloning:  bash setup.sh
# ─────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/hexis.service"
SERVICE_DEST="/etc/systemd/system/hexis.service"
WHOAMI="$(whoami)"

echo ""
echo "  ██╗  ██╗███████╗██╗  ██╗██╗███████╗"
echo "  ██║  ██║██╔════╝╚██╗██╔╝██║██╔════╝"
echo "  ███████║█████╗   ╚███╔╝ ██║███████╗"
echo "  ██╔══██║██╔══╝   ██╔██╗ ██║╚════██║"
echo "  ██║  ██║███████╗██╔╝ ██╗██║███████║"
echo "  ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝╚══════╝"
echo "  Autonomous Crypto Trading Agent — Setup"
echo ""

# ── 1. Python dependencies ───────────────────────────────────────
echo "▶  Installing Python dependencies..."
pip3 install -r "$SCRIPT_DIR/requirements.txt" --quiet
echo "   ✓ Dependencies installed"

# ── 2. .env file ─────────────────────────────────────────────────
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    if [ -f "$SCRIPT_DIR/.env.example" ]; then
        cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
        echo "   ✓ .env created from .env.example — please fill in your keys"
    else
        touch "$SCRIPT_DIR/.env"
        echo "   ✓ Empty .env created — please add your API keys"
    fi
else
    echo "   ✓ .env already exists"
fi

# ── 3. Systemd service ────────────────────────────────────────────
if command -v systemctl &>/dev/null; then
    # Patch the service file to use the actual user and path
    sed -e "s|User=henning|User=$WHOAMI|g" \
        -e "s|/home/henning/HEXIS|$SCRIPT_DIR|g" \
        "$SERVICE_FILE" | sudo tee "$SERVICE_DEST" > /dev/null

    sudo systemctl daemon-reload
    sudo systemctl enable hexis.service
    echo "   ✓ systemd service installed and enabled (auto-starts on boot)"
    echo ""
    echo "  ─────────────────────────────────────────────"
    echo "  Commands:"
    echo "    sudo systemctl start   hexis   → start bot"
    echo "    sudo systemctl stop    hexis   → stop bot"
    echo "    sudo systemctl restart hexis   → restart"
    echo "    sudo systemctl status  hexis   → status"
    echo "    tail -f $SCRIPT_DIR/bot.log   → live logs"
    echo "  ─────────────────────────────────────────────"
else
    echo "   ⚠  systemctl not found — skipping service install"
    echo "   Start manually with: python3 $SCRIPT_DIR/run.py"
fi

echo ""
echo "  ✅  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit .env and add your API keys"
echo "  2. sudo systemctl start hexis"
echo "  3. Open http://$(hostname -I | awk '{print $1}'):5000"
echo "  4. Register your admin account"
echo ""

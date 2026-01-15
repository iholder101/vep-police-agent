#!/bin/bash

# Script to create a systemd unit for the VEP Police Agent
# The unit will run the agent continuously using run-latest-agent.sh
# Logs are automatically handled by systemd (view with: journalctl -u vep-police-agent)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_NAME="vep-police-agent"
UNIT_FILE="/etc/systemd/system/${UNIT_NAME}.service"

# Check if unit already exists
if [ -f "$UNIT_FILE" ]; then
    echo "Systemd unit already exists: $UNIT_FILE"
    echo "To view status: systemctl status $UNIT_NAME"
    echo "To view logs: journalctl -u $UNIT_NAME -f"
    echo "To restart: systemctl restart $UNIT_NAME"
    exit 0
fi

# Check if running as root (needed to create systemd unit)
if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo) to create systemd unit files."
    echo "Run: sudo $0"
    exit 1
fi

# Determine the user to run the service as
# If run via sudo, use SUDO_USER; otherwise use the actual user (should be root)
SERVICE_USER="${SUDO_USER:-$(whoami)}"

# Create systemd unit file
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=VEP Police Agent - AI-powered governance agent for KubeVirt VEPs
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PROJECT_ROOT/scripts/run-latest-agent.sh
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Environment variables (if needed, uncomment and set)
# Environment="API_KEY=/path/to/API_KEY"
# Environment="GOOGLE_TOKEN=/path/to/GOOGLE_TOKEN"
# Environment="GITHUB_TOKEN=/path/to/GITHUB_TOKEN"
# Environment="RESEND_API_KEY=/path/to/RESEND_API_KEY"

[Install]
WantedBy=multi-user.target
EOF

# Set proper permissions
chmod 644 "$UNIT_FILE"

# Reload systemd to recognize new unit
systemctl daemon-reload

echo "Systemd unit created successfully: $UNIT_FILE"
echo ""
echo "To start the service:"
echo "  sudo systemctl start $UNIT_NAME"
echo ""
echo "To enable auto-start on boot:"
echo "  sudo systemctl enable $UNIT_NAME"
echo ""
echo "To view status:"
echo "  systemctl status $UNIT_NAME"
echo ""
echo "To view logs (follow mode):"
echo "  journalctl -u $UNIT_NAME -f"
echo ""
echo "To view recent logs:"
echo "  journalctl -u $UNIT_NAME -n 100"
echo ""
echo "To stop the service:"
echo "  sudo systemctl stop $UNIT_NAME"
echo ""
echo "Note: Logs are automatically saved by systemd and can be viewed with journalctl."
echo "      Logs persist across reboots (configurable in journald.conf)."

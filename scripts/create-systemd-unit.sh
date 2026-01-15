#!/bin/bash

# Script to create a systemd unit for the VEP Police Agent
# The unit will run the agent continuously using run-latest-agent.sh
# Logs are automatically handled by systemd (view with: journalctl -u vep-police-agent)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_NAME="vep-police-agent"
UNIT_FILE="/etc/systemd/system/${UNIT_NAME}.service"

# Show help if requested
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    cat <<EOF
Usage: $0 [--help]

Creates a systemd unit for the VEP Police Agent that runs continuously.

The script will:
- Create a systemd unit at /etc/systemd/system/${UNIT_NAME}.service
- Use the run-latest-agent.sh script to run the agent
- Configure automatic restart on failure
- Set up logging via systemd journald

Prerequisites:
- Must be run as root (sudo)
- The run-latest-agent.sh script must exist in the scripts directory

After creating the unit:

# Start the service
sudo systemctl start ${UNIT_NAME}

# Enable auto-start on boot
sudo systemctl enable ${UNIT_NAME}

# View status
systemctl status ${UNIT_NAME}

# View logs (follow mode)
journalctl -u ${UNIT_NAME} -f

# View recent logs
journalctl -u ${UNIT_NAME} -n 100

# Stop the service
sudo systemctl stop ${UNIT_NAME}

# Restart the service
sudo systemctl restart ${UNIT_NAME}

# Disable auto-start on boot
sudo systemctl disable ${UNIT_NAME}

Note: Logs are automatically saved by systemd and can be viewed with journalctl.
      The script enables persistent journal by default - logs persist across reboots
      and can be viewed even after the service is stopped.
      
      Retention period is configurable in /etc/systemd/journald.conf
      (default: usually 1 month or until disk space limit).
      
      To view logs from a stopped service:
      journalctl -u ${UNIT_NAME} --since "2024-01-15" --until "2024-01-16"
EOF
    exit 0
fi

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

# Ensure persistent journal is enabled (logs persist across reboots)
if [ ! -d "/var/log/journal" ]; then
    echo "Enabling persistent journal for log persistence..."
    mkdir -p /var/log/journal
    systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true
    systemctl restart systemd-journald 2>/dev/null || true
    echo "✓ Persistent journal enabled"
else
    echo "✓ Persistent journal already enabled"
fi

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
echo "      Persistent journal is enabled - logs will persist across reboots and after service stops."
echo ""
echo "To view logs from a stopped service (e.g., yesterday):"
echo "  journalctl -u ${UNIT_NAME} --since yesterday"
echo "  journalctl -u ${UNIT_NAME} --since '2024-01-15' --until '2024-01-16'"

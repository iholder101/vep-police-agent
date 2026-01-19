#!/bin/bash

# Script to create a systemd unit for the VEP Police Agent
# The unit will run the agent once using run-latest-agent.sh
# Logs are automatically handled by systemd (view with: journalctl -u vep-police-agent)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_NAME="vep-police-agent"
UNIT_FILE="/etc/systemd/system/${UNIT_NAME}.service"

# Check if --delete flag is present anywhere in args
DELETE_REQUESTED=false
for arg in "$@"; do
    if [ "$arg" = "--delete" ]; then
        DELETE_REQUESTED=true
        break
    fi
done

# Handle --delete flag
if [ "$DELETE_REQUESTED" = true ]; then
    # Check if running as root (needed to delete systemd unit)
    if [ "$EUID" -ne 0 ]; then
        echo "Error: This script must be run as root (sudo) to delete systemd unit files."
        echo "Run: sudo $0 --delete"
        exit 1
    fi
    
    # Check if unit exists
    if [ ! -f "$UNIT_FILE" ]; then
        echo "Systemd unit does not exist: $UNIT_FILE"
        echo "Nothing to delete."
        exit 0
    fi
    
    # Stop the service if it's running
    if systemctl is-active --quiet "$UNIT_NAME" 2>/dev/null; then
        echo "Stopping service..."
        systemctl stop "$UNIT_NAME" --no-block 2>/dev/null || systemctl stop "$UNIT_NAME" 2>/dev/null || true
    else
        echo "Service is not running, skipping stop"
    fi
    
    if systemctl is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then
        echo "Disabling service..."
        systemctl disable "$UNIT_NAME"
    fi
    
    # Remove the unit file
    echo "Removing systemd unit file: $UNIT_FILE"
    rm -f "$UNIT_FILE"
    
    # Reload systemd to recognize the removal
    systemctl daemon-reload

    # Delete all logs for this unit
    echo "Deleting all logs for ${UNIT_NAME}..."
    journalctl --rotate
    journalctl --vacuum-time=1s --unit="${UNIT_NAME}" 2>/dev/null || true

    echo ""
    echo "✓ Systemd unit and logs removed successfully"
    exit 0
fi

# Collect extra flags to pass to the agent (any args that aren't script-specific)
EXTRA_FLAGS=()
SHOW_HELP=false
START_NOW=false
for arg in "$@"; do
    case "$arg" in
        --help|-h)
            SHOW_HELP=true
            ;;
        --delete)
            # Already handled above
            ;;
        --start)
            START_NOW=true
            ;;
        *)
            EXTRA_FLAGS+=("$arg")
            ;;
    esac
done

# Show help if requested
if [ "$SHOW_HELP" = true ]; then
    cat <<EOF
Usage: $0 [--help|--delete|--start] [AGENT_FLAGS...]

Creates or deletes a systemd unit for the VEP Police Agent.

Options:
  --help, -h    Show this help message
  --delete      Remove the systemd unit and clean up (stops and disables service)
  --start       Start the service immediately after creating the unit

Agent Flags (passed to run-latest-agent.sh):
  Any additional flags are embedded in the systemd unit and passed to the agent.
  Example: $0 --use-state-cache --skip-send-email

Creates a systemd unit for the VEP Police Agent that runs once.

The script will:
- Create a systemd unit at /etc/systemd/system/${UNIT_NAME}.service
- Use the run-latest-agent.sh script to run the agent
- Run once without automatic restart
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

# Delete the systemd unit
sudo $0 --delete

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

# Build ExecStart command with extra flags
EXEC_START="$PROJECT_ROOT/scripts/run-latest-agent.sh --immediate-start"
if [ ${#EXTRA_FLAGS[@]} -gt 0 ]; then
    EXEC_START="$EXEC_START ${EXTRA_FLAGS[*]}"
    echo "Including extra flags in systemd unit: ${EXTRA_FLAGS[*]}"
fi

# Create systemd unit file
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=VEP Police Agent - AI-powered governance agent for KubeVirt VEPs
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$PROJECT_ROOT
ExecStart=$EXEC_START
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

# Start the service if --start flag was provided
if [ "$START_NOW" = true ]; then
    echo "Starting service..."
    systemctl start "$UNIT_NAME"
    echo "✓ Service started"
    echo ""
    echo "View logs with:"
    echo "  journalctl -u $UNIT_NAME -f -o cat"
else
    echo "To start the service:"
    echo "  sudo systemctl start $UNIT_NAME"
    echo ""
    echo "To view logs (follow mode):"
    echo "  journalctl -u $UNIT_NAME -f -o cat"
fi

echo ""
echo "To enable auto-start on boot:"
echo "  sudo systemctl enable $UNIT_NAME"
echo ""
echo "To view status:"
echo "  systemctl status $UNIT_NAME"
echo ""
echo "To stop the service:"
echo "  sudo systemctl stop $UNIT_NAME"

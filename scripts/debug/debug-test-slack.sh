#!/bin/bash

# Debug script to test Slack alert functionality
# Skips monitoring and sheets to focus on Slack alerts only
# Also skips email to test Slack in isolation

# Get absolute path to current directory for mounting
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Build podman command - mount files and pass paths to avoid JSON parsing issues
CMD_ARGS=(
    --api-key /workspace/API_KEY
    --google-token /workspace/GOOGLE_TOKEN
    --skip-monitoring
    --skip-sheets
    --skip-send-email
    --one-cycle
    --fastest-model
    --mock-veps
    --mock-analyzed-combined
    --mock-alert-summary
    --immediate-start
)

# Add GitHub token if file exists
if [ -f "$PROJECT_ROOT/GITHUB_TOKEN" ]; then
    CMD_ARGS+=(--github-token /workspace/GITHUB_TOKEN)
fi

# Add Resend API key if file exists (for consistency, even though email is skipped)
if [ -f "$PROJECT_ROOT/RESEND_API_KEY" ]; then
    CMD_ARGS+=(--resend-api-key /workspace/RESEND_API_KEY)
fi

# Add Slack webhook URL if file exists
if [ -f "$PROJECT_ROOT/SLACK_WEBHOOK_URL" ]; then
    CMD_ARGS+=(--slack-webhook-url /workspace/SLACK_WEBHOOK_URL)
else
    echo "WARNING: SLACK_WEBHOOK_URL file not found at $PROJECT_ROOT/SLACK_WEBHOOK_URL"
    echo "Create it with: echo 'https://hooks.slack.com/services/...' > SLACK_WEBHOOK_URL"
    echo ""
fi

# Use state cache for fast debug cycles
CMD_ARGS+=(--use-state-cache)

# Pass through any additional arguments/flags (e.g., --no-index-cache)
if [ $# -gt 0 ]; then
    CMD_ARGS+=("$@")
fi

# Run podman
# Set PYTHONUNBUFFERED=1 to ensure logs are flushed immediately (important for real-time log viewing)
podman run --rm --pull=newer \
    -e PYTHONUNBUFFERED=1 \
    -v "$PROJECT_ROOT:/workspace:ro" \
    -w /workspace \
    quay.io/mabekitzur/vep-police-agent:latest \
    "${CMD_ARGS[@]}"

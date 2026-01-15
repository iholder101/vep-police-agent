#!/bin/bash

# Debug script to test email alert functionality
# Skips monitoring and sheets to focus on email alerts only
# Uses EMAIL_RECIPIENTS from config.py (default: iholder@redhat.com)
# Can override by setting EMAIL_RECIPIENTS environment variable (comma-separated emails)

# Get absolute path to current directory for mounting
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Build podman command - mount files and pass paths to avoid JSON parsing issues
CMD_ARGS=(
    --api-key /workspace/API_KEY
    --google-token /workspace/GOOGLE_TOKEN
    --skip-monitoring
    --skip-sheets
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

# Add Resend API key if file exists
if [ -f "$PROJECT_ROOT/RESEND_API_KEY" ]; then
    CMD_ARGS+=(--resend-api-key /workspace/RESEND_API_KEY)
fi

# Pass through any additional arguments/flags (e.g., --no-index-cache)
if [ $# -gt 0 ]; then
    CMD_ARGS+=("$@")
fi

# Build podman environment variables
PODMAN_ENV=()
# Only pass EMAIL_RECIPIENTS if it's set (to override config.py default)
# If not set, config.py default (iholder@redhat.com) will be used
if [ -n "$EMAIL_RECIPIENTS" ]; then
    PODMAN_ENV+=(-e "EMAIL_RECIPIENTS=$EMAIL_RECIPIENTS")
fi

# Run podman with environment variables
podman run --rm --pull=newer \
    -v "$PROJECT_ROOT:/workspace:ro" \
    -w /workspace \
    "${PODMAN_ENV[@]}" \
    quay.io/mabekitzur/vep-police-agent:latest \
    "${CMD_ARGS[@]}"

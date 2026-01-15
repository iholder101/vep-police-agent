#!/bin/bash

# Debug script to test email alert functionality
# Skips monitoring and sheets to focus on email alerts only
# Requires EMAIL_RECIPIENTS environment variable (comma-separated emails)

# Get absolute path to current directory for mounting
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Check if EMAIL_RECIPIENTS is set
if [ -z "$EMAIL_RECIPIENTS" ]; then
    echo "ERROR: EMAIL_RECIPIENTS environment variable is not set."
    echo "Please set it to a comma-separated list of email addresses:"
    echo "  export EMAIL_RECIPIENTS='user1@example.com,user2@example.com'"
    exit 1
fi

# Build podman command - mount files and pass paths to avoid JSON parsing issues
CMD_ARGS=(
    --api-key /workspace/API_KEY
    --google-token /workspace/GOOGLE_TOKEN
    --skip-monitoring
    --skip-sheets
    --one-cycle
)

# Add GitHub token if file exists
if [ -f "$PROJECT_ROOT/GITHUB_TOKEN" ]; then
    CMD_ARGS+=(--github-token /workspace/GITHUB_TOKEN)
fi

# Pass through any additional arguments/flags (e.g., --fastest-model, --no-index-cache)
if [ $# -gt 0 ]; then
    CMD_ARGS+=("$@")
fi

# Export EMAIL_RECIPIENTS to the container
podman run --rm --pull=newer \
    -v "$PROJECT_ROOT:/workspace:ro" \
    -w /workspace \
    -e EMAIL_RECIPIENTS="$EMAIL_RECIPIENTS" \
    quay.io/mabekitzur/vep-police-agent:latest \
    "${CMD_ARGS[@]}"

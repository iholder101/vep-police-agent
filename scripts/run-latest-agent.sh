#!/bin/bash

# Get absolute path to current directory for mounting
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Default sheet ID (can be overridden with --sheet-id flag or SHEET_ID env var)
DEFAULT_SHEET_ID="${SHEET_ID:-12evICwzi3Hpkbc3vWLp6pKEQNz7G3yFblWP2b764et4}"

# Build podman command - mount files and pass paths to avoid JSON parsing issues
CMD_ARGS=(
    --api-key /workspace/API_KEY
    --google-token /workspace/GOOGLE_TOKEN
)

# Add GitHub token if file exists
if [ -f "$PROJECT_ROOT/GITHUB_TOKEN" ]; then
    CMD_ARGS+=(--github-token /workspace/GITHUB_TOKEN)
fi

# Add Resend API key if file exists
if [ -f "$PROJECT_ROOT/RESEND_API_KEY" ]; then
    CMD_ARGS+=(--resend-api-key /workspace/RESEND_API_KEY)
fi

# Add Slack webhook URL if file exists
if [ -f "$PROJECT_ROOT/SLACK_WEBHOOK_URL" ]; then
    CMD_ARGS+=(--slack-webhook-url /workspace/SLACK_WEBHOOK_URL)
fi

# Parse deploy-specific flags (--detach, --container-name) out of the
# argument list before the rest are forwarded to the agent inside the container
DETACH_MODE=false
CONTAINER_NAME=""
FORWARD_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --detach)
            DETACH_MODE=true
            shift
            ;;
        --container-name)
            CONTAINER_NAME="$2"
            shift 2
            ;;
        *)
            FORWARD_ARGS+=("$1")
            shift
            ;;
    esac
done

# Check if --sheet-id is already in arguments (user override)
SHEET_ID_IN_ARGS=false
for arg in "${FORWARD_ARGS[@]}"; do
    if [[ "$arg" == --sheet-id* ]]; then
        SHEET_ID_IN_ARGS=true
        break
    fi
done

# Add default sheet ID if not overridden
if [ "$SHEET_ID_IN_ARGS" = false ]; then
    CMD_ARGS+=(--sheet-id "$DEFAULT_SHEET_ID")
fi

# Pass through any remaining arguments/flags
CMD_ARGS+=("${FORWARD_ARGS[@]}")

# Build podman run flags - detached/named for production deploys, foreground otherwise
PODMAN_FLAGS=(--rm --pull=newer)
if [ "$DETACH_MODE" = true ]; then
    PODMAN_FLAGS+=(--detach --log-driver=journald)
fi
if [ -n "$CONTAINER_NAME" ]; then
    PODMAN_FLAGS+=(--name "$CONTAINER_NAME")
fi

# Execute the command
# Set PYTHONUNBUFFERED=1 to ensure logs are flushed immediately (important for real-time log viewing)
# Mount workspace as read-only, but cache directory as read-write for persistence
podman run "${PODMAN_FLAGS[@]}" \
    -e PYTHONUNBUFFERED=1 \
    --network=host \
    -v "$PROJECT_ROOT:/workspace:ro" \
    -v "$PROJECT_ROOT/cache:/workspace/cache:rw" \
    -v "$PROJECT_ROOT/output:/workspace/output:rw" \
    -w /workspace \
    quay.io/mabekitzur/vep-police-agent:latest \
    "${CMD_ARGS[@]}"



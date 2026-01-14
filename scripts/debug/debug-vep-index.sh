#!/bin/bash

# Debug script to run the agent with --debug=discover-veps flag
# This will index all VEP data and print it, then exit

# Get absolute path to current directory for mounting
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

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

# Check if --sheet-id is already in arguments (user override)
SHEET_ID_IN_ARGS=false
for arg in "$@"; do
    if [[ "$arg" == --sheet-id* ]]; then
        SHEET_ID_IN_ARGS=true
        break
    fi
done

# Add default sheet ID if not overridden
if [ "$SHEET_ID_IN_ARGS" = false ]; then
    CMD_ARGS+=(--sheet-id "$DEFAULT_SHEET_ID")
fi

# Pass through any additional arguments/flags (including --no-index-cache, --index-cache-minutes, etc.)
# Note: --debug discover-veps is added last, so it will override any --debug flag passed by user
if [ $# -gt 0 ]; then
    for arg in "$@"; do
        # Skip --debug if user passed it, since we'll add our own
        if [[ "$arg" == --debug* ]]; then
            continue
        fi
        CMD_ARGS+=("$arg")
    done
fi

CMD_ARGS+=(--debug discover-veps)

podman run --rm --pull=newer \
    -v "$PROJECT_ROOT:/workspace:ro" \
    -w /workspace \
    quay.io/mabekitzur/vep-police-agent:latest \
    "${CMD_ARGS[@]}"

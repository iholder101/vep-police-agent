#!/bin/bash

# Debug script to test Google Sheets integration with limited LLM iterations
# This limits LLM iterations to 1 to quickly test the sheets functionality

# Get absolute path to current directory for mounting
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Build podman command - mount files and pass paths to avoid JSON parsing issues
CMD_ARGS=(
    --api-key /workspace/API_KEY
    --google-token /workspace/GOOGLE_TOKEN
)

# Add GitHub token if file exists
if [ -f "$PROJECT_ROOT/GITHUB_TOKEN" ]; then
    CMD_ARGS+=(--github-token /workspace/GITHUB_TOKEN)
fi

CMD_ARGS+=(--debug test-sheets)

podman run --rm --pull=newer \
    -v "$PROJECT_ROOT:/workspace:ro" \
    -w /workspace \
    quay.io/mabekitzur/vep-police-agent:latest \
    "${CMD_ARGS[@]}"

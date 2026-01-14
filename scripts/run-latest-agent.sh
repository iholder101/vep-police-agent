#!/bin/bash

# Build podman command
CMD="podman run --rm --pull=newer quay.io/mabekitzur/vep-police-agent:latest"
CMD="$CMD --api-key \"$(cat API_KEY)\""
CMD="$CMD --google-token \"$(cat GOOGLE_TOKEN)\""

# Add GitHub token if file exists
if [ -f "GITHUB_TOKEN" ]; then
    CMD="$CMD --github-token \"$(cat GITHUB_TOKEN)\""
fi

# Default sheet ID (can be overridden with --sheet-id flag or SHEET_ID env var)
DEFAULT_SHEET_ID="${SHEET_ID:-12evICwzi3Hpkbc3vWLp6pKEQNz7G3yFblWP2b764et4}"

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
    CMD="$CMD --sheet-id \"$DEFAULT_SHEET_ID\""
fi

# Pass through any additional arguments/flags
if [ $# -gt 0 ]; then
    for arg in "$@"; do
        CMD="$CMD \"$arg\""
    done
fi

# Execute the command
eval $CMD



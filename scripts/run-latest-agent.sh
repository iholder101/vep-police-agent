#!/bin/bash

# Build podman command
CMD="podman run --rm --pull=newer quay.io/mabekitzur/vep-police-agent:latest"
CMD="$CMD --api-key \"$(cat API_KEY)\""
CMD="$CMD --google-token \"$(cat GOOGLE_TOKEN)\""

# Add GitHub token if file exists
if [ -f "GITHUB_TOKEN" ]; then
    CMD="$CMD --github-token \"$(cat GITHUB_TOKEN)\""
fi

# Pass through any additional arguments/flags
if [ $# -gt 0 ]; then
    for arg in "$@"; do
        CMD="$CMD \"$arg\""
    done
fi

# Execute the command
eval $CMD



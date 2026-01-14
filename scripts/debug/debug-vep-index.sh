#!/bin/bash

# Debug script to run the agent with --debug=discover-veps flag
# This will index all VEP data and print it, then exit

# Build podman command
CMD="podman run --rm --pull=newer quay.io/mabekitzur/vep-police-agent:latest"
CMD="$CMD --api-key \"$(cat API_KEY)\""
CMD="$CMD --google-token \"$(cat GOOGLE_TOKEN)\""

# Add GitHub token if file exists
if [ -f "GITHUB_TOKEN" ]; then
    CMD="$CMD --github-token \"$(cat GITHUB_TOKEN)\""
fi

# Add debug flag
CMD="$CMD --debug discover-veps"

# Execute the command
eval $CMD

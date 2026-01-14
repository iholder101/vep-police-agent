#!/bin/bash

# Debug script to test Google Sheets integration with limited LLM iterations
# This limits LLM iterations to 1 to quickly test the sheets functionality

# Build podman command
CMD="podman run --rm --pull=newer quay.io/mabekitzur/vep-police-agent:latest"
CMD="$CMD --api-key \"$(cat API_KEY)\""
CMD="$CMD --google-token \"$(cat GOOGLE_TOKEN)\""

# Add GitHub token if file exists
if [ -f "GITHUB_TOKEN" ]; then
    CMD="$CMD --github-token \"$(cat GITHUB_TOKEN)\""
fi

# Add debug flag
CMD="$CMD --debug test-sheets"

# Execute the command
eval $CMD

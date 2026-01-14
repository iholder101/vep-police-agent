#!/bin/bash
# Opinionated build and push script with default settings
# Sets default tag to current date in DD_MM_YYYY format
# Automatically increments suffix if tag already exists (DD_MM_YYYY-1, DD_MM_YYYY-2, etc.)

set -e

# Get script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Set default values
export ADD_LATEST_TAG="${ADD_LATEST_TAG:-true}"
export QUAY_USERNAME="${QUAY_USERNAME:-mabekitzur}"
export IMAGE_NAME="${IMAGE_NAME:-vep-police-agent}"

# Function to check if a tag exists in quay.io
tag_exists() {
    local tag=$1
    local image_name="quay.io/${QUAY_USERNAME}/${IMAGE_NAME}:${tag}"
    
    # Use skopeo if available (most reliable for remote registries)
    if command -v skopeo &>/dev/null; then
        if skopeo inspect "docker://${image_name}" &>/dev/null 2>&1; then
            return 0  # Tag exists
        else
            return 1  # Tag does not exist
        fi
    fi
    
    # Fallback: Use podman pull --quiet (reliable but slower)
    # This will actually pull the image, but --quiet suppresses output
    # We check if it succeeds - if tag doesn't exist, pull will fail
    if podman pull --quiet "${image_name}" &>/dev/null 2>&1; then
        return 0  # Tag exists
    else
        return 1  # Tag does not exist
    fi
}

# Generate base tag from current date (DD_MM_YYYY format)
BASE_TAG=$(date +"%d_%m_%Y")

# If IMAGE_TAG is explicitly set, use it; otherwise find next available tag
if [ -n "${IMAGE_TAG}" ] && [ "${IMAGE_TAG}" != "${BASE_TAG}" ]; then
    # User explicitly set IMAGE_TAG, use it as-is
    FINAL_TAG="${IMAGE_TAG}"
    echo "Using explicitly set tag: ${FINAL_TAG}"
else
    # Find next available tag starting from base tag
    FINAL_TAG="${BASE_TAG}"
    suffix=0
    
    echo "Checking for existing tags starting from: ${BASE_TAG}"
    
    while tag_exists "${FINAL_TAG}"; do
        echo "  Tag ${FINAL_TAG} exists, trying next..."
        if [ $suffix -eq 0 ]; then
            # First increment: add -1
            FINAL_TAG="${BASE_TAG}-1"
            suffix=1
        else
            # Subsequent increments: increment the number
            suffix=$((suffix + 1))
            FINAL_TAG="${BASE_TAG}-${suffix}"
        fi
    done
    
    echo "✓ Using tag: ${FINAL_TAG}"
fi

# Export the final tag
export IMAGE_TAG="${FINAL_TAG}"

# Run the build script from project root
cd "${PROJECT_ROOT}"
exec "${SCRIPT_DIR}/build-and-push.sh"


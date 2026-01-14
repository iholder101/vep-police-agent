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

# Function to check if a tag exists in quay.io (used as fallback)
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
    if podman pull --quiet "${image_name}" &>/dev/null 2>&1; then
        return 0  # Tag exists
    else
        return 1  # Tag does not exist
    fi
}

# Function to fetch all tags for the image from quay.io
fetch_all_tags() {
    local image_name="quay.io/${QUAY_USERNAME}/${IMAGE_NAME}"
    
    # Use skopeo if available (fastest for listing tags)
    if command -v skopeo &>/dev/null; then
        # List all tags using skopeo (returns JSON)
        local json_output=$(skopeo list-tags "docker://${image_name}" 2>/dev/null || echo "")
        
        if [ -n "$json_output" ]; then
            # Parse JSON - extract tags array
            # skopeo list-tags returns: {"Repository":"...","Tags":["tag1","tag2",...]}
            if command -v jq &>/dev/null; then
                # Use jq to parse JSON properly (best method)
                jq -r '.Tags[]? // empty' <<< "$json_output" 2>/dev/null
                return
            else
                # Fallback: parse JSON with grep/sed (works for simple cases)
                # Extract content between "Tags":[" and "]"
                echo "$json_output" | sed -n 's/.*"Tags":\[\([^]]*\)\].*/\1/p' | \
                    sed 's/"//g' | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | \
                    grep -v '^$'
                return
            fi
        fi
    fi
    
    # Return empty if we couldn't fetch tags
    return 1
}

# Function to check if a tag exists in a list of tags
tag_exists_in_list() {
    local tag=$1
    shift
    local tags=("$@")
    
    for existing_tag in "${tags[@]}"; do
        if [ "$existing_tag" = "$tag" ]; then
            return 0  # Tag exists
        fi
    done
    return 1  # Tag does not exist
}

# Generate base tag from current date (DD_MM_YYYY format)
BASE_TAG=$(date +"%d_%m_%Y")

# If IMAGE_TAG is explicitly set, use it; otherwise find next available tag
if [ -n "${IMAGE_TAG}" ] && [ "${IMAGE_TAG}" != "${BASE_TAG}" ]; then
    # User explicitly set IMAGE_TAG, use it as-is
    FINAL_TAG="${IMAGE_TAG}"
    echo "Using explicitly set tag: ${FINAL_TAG}"
else
    # Fetch all existing tags once
    echo "Fetching existing tags from quay.io..."
    mapfile -t existing_tags < <(fetch_all_tags)
    
    if [ ${#existing_tags[@]} -eq 0 ]; then
        # If we couldn't fetch tags (e.g., no skopeo), fall back to checking one by one
        echo "Could not fetch tag list, checking tags individually..."
        FINAL_TAG="${BASE_TAG}"
        suffix=0
        
        echo "Checking for existing tags starting from: ${BASE_TAG}"
        
        # Fallback: check tags one by one (old method)
        while tag_exists "${FINAL_TAG}"; do
            echo "  Tag ${FINAL_TAG} exists, trying next..."
            if [ $suffix -eq 0 ]; then
                FINAL_TAG="${BASE_TAG}-1"
                suffix=1
            else
                suffix=$((suffix + 1))
                FINAL_TAG="${BASE_TAG}-${suffix}"
            fi
        done
    else
        # Use the fetched tag list
        echo "Found ${#existing_tags[@]} existing tag(s)"
        FINAL_TAG="${BASE_TAG}"
        suffix=0
        
        echo "Checking for existing tags starting from: ${BASE_TAG}"
        
        while tag_exists_in_list "${FINAL_TAG}" "${existing_tags[@]}"; do
            echo "  Tag ${FINAL_TAG} exists, trying next..."
            if [ $suffix -eq 0 ]; then
                FINAL_TAG="${BASE_TAG}-1"
                suffix=1
            else
                suffix=$((suffix + 1))
                FINAL_TAG="${BASE_TAG}-${suffix}"
            fi
        done
    fi
    
    echo "✓ Using tag: ${FINAL_TAG}"
fi

# Export the final tag
export IMAGE_TAG="${FINAL_TAG}"

# Run the build script from project root
cd "${PROJECT_ROOT}"
exec "${SCRIPT_DIR}/build-and-push.sh"


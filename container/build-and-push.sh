#!/bin/bash
# Build and push VEP Police Agent container image to quay.io

set -e

# Default values
# Try to get quay.io username from login, fallback to whoami
LOGGED_IN_QUAY_USER=$(podman login --get-login quay.io 2>/dev/null || echo "")
if [ -n "$LOGGED_IN_QUAY_USER" ]; then
    DEFAULT_QUAY_USERNAME="$LOGGED_IN_QUAY_USER"
else
    DEFAULT_QUAY_USERNAME="$(whoami)"
fi
QUAY_USERNAME="${QUAY_USERNAME:-$DEFAULT_QUAY_USERNAME}"
IMAGE_NAME="${IMAGE_NAME:-vep-police-agent}"

# Function to fetch all tags for the image from quay.io
fetch_all_tags() {
    local image_name="quay.io/${QUAY_USERNAME}/${IMAGE_NAME}"
    
    # Use skopeo if available (fastest for listing tags)
    if command -v skopeo &>/dev/null; then
        # List all tags using skopeo (returns JSON)
        local json_output=$(timeout 30 skopeo list-tags "docker://${image_name}" 2>/dev/null || echo "")
        
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

# Function to check if a tag exists in quay.io (used as fallback)
tag_exists() {
    local tag=$1
    local image_name="quay.io/${QUAY_USERNAME}/${IMAGE_NAME}:${tag}"
    
    # Use skopeo if available (most reliable for remote registries)
    if command -v skopeo &>/dev/null; then
        # Add timeout to prevent hanging
        if timeout 10 skopeo inspect "docker://${image_name}" &>/dev/null 2>&1; then
            return 0  # Tag exists
        else
            return 1  # Tag does not exist
        fi
    fi
    
    # Fallback: Use podman pull --quiet (reliable but slower)
    # Add timeout to prevent hanging
    if timeout 10 podman pull --quiet "${image_name}" &>/dev/null 2>&1; then
        return 0  # Tag exists
    else
        return 1  # Tag does not exist
    fi
}

# Generate base tag from current date (DD_MM_YYYY format)
BASE_TAG=$(date +"%d_%m_%Y")

# If IMAGE_TAG is explicitly set, use it; otherwise use date-based tag with auto-increment
if [ -n "${IMAGE_TAG}" ]; then
    # User explicitly set IMAGE_TAG, use it as-is
    FINAL_TAG="${IMAGE_TAG}"
else
    # Use date-based tag and check if it exists, increment if needed
    FINAL_TAG="${BASE_TAG}"
    suffix=0
    
    # Fetch all existing tags once (much faster than checking one by one)
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
            # Safety limit to prevent infinite loops
            if [ $suffix -gt 100 ]; then
                echo "WARNING: Reached maximum suffix limit (100), using tag: ${FINAL_TAG}"
                break
            fi
        done
    else
        # Use the fetched tag list (much faster)
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
            # Safety limit to prevent infinite loops
            if [ $suffix -gt 100 ]; then
                echo "WARNING: Reached maximum suffix limit (100), using tag: ${FINAL_TAG}"
                break
            fi
        done
    fi
    
    echo "✓ Using tag: ${FINAL_TAG}"
fi

IMAGE_TAG="${FINAL_TAG}"

# Option to also tag as "latest" (default to true for date-based tags)
ADD_LATEST_TAG="${ADD_LATEST_TAG:-true}"

# Full image name
FULL_IMAGE_NAME="quay.io/${QUAY_USERNAME}/${IMAGE_NAME}:${IMAGE_TAG}"
LATEST_IMAGE_NAME="quay.io/${QUAY_USERNAME}/${IMAGE_NAME}:latest"

echo "=========================================="
echo "VEP Police Agent - Build and Push Script"
echo "=========================================="
echo ""
echo "Configuration:"
echo "  Quay.io Username: ${QUAY_USERNAME}"
echo "  Image Name:      ${IMAGE_NAME}"
echo "  Image Tag:       ${IMAGE_TAG}"
echo "  Add Latest Tag:  ${ADD_LATEST_TAG}"
echo "  Full Image:      ${FULL_IMAGE_NAME}"
if [ "${ADD_LATEST_TAG}" = "true" ] || [ "${ADD_LATEST_TAG}" = "1" ]; then
    echo "  Latest Image:    ${LATEST_IMAGE_NAME}"
fi
echo ""

# Check if podman is available
if ! command -v podman &> /dev/null; then
    echo "ERROR: podman is not installed or not in PATH"
    exit 1
fi

# Check if already logged in to quay.io
if ! podman login --get-login quay.io &> /dev/null; then
    echo "Not logged in to quay.io. Please provide credentials:"
    echo ""
    
    # Prompt for username if not set
    if [ -z "$QUAY_USERNAME" ] || [ "$QUAY_USERNAME" = "$(whoami)" ]; then
        read -p "Quay.io Username [${QUAY_USERNAME}]: " input_username
        if [ -n "$input_username" ]; then
            QUAY_USERNAME="$input_username"
            FULL_IMAGE_NAME="quay.io/${QUAY_USERNAME}/${IMAGE_NAME}:${IMAGE_TAG}"
        fi
    fi
    
    # Prompt for password/token
    echo ""
    read -sp "Quay.io Password/Token: " quay_password
    echo ""
    
    if [ -z "$quay_password" ]; then
        echo "ERROR: Password/Token is required"
        exit 1
    fi
    
    # Login to quay.io
    echo "Logging in to quay.io..."
    echo "$quay_password" | podman login quay.io --username "$QUAY_USERNAME" --password-stdin
    
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to login to quay.io"
        exit 1
    fi
    
    echo "✓ Successfully logged in to quay.io"
    echo ""
else
    echo "✓ Already logged in to quay.io"
    echo ""
fi

# Build the image (from parent directory, using Containerfile in container/)
echo "Building container image..."
cd "$(dirname "$0")/.." || exit 1
podman build -f container/Containerfile -t "${FULL_IMAGE_NAME}" .

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to build container image"
    exit 1
fi

echo "✓ Image built successfully"
echo ""

# Tag as "latest" if requested
if [ "${ADD_LATEST_TAG}" = "true" ] || [ "${ADD_LATEST_TAG}" = "1" ]; then
    if [ "${IMAGE_TAG}" != "latest" ]; then
        echo "Tagging image as 'latest'..."
        podman tag "${FULL_IMAGE_NAME}" "${LATEST_IMAGE_NAME}"
        echo "✓ Image tagged as 'latest'"
        echo ""
    fi
fi

# Push the image(s)
echo "Pushing image to quay.io..."
podman push "${FULL_IMAGE_NAME}"

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to push image to quay.io"
    exit 1
fi

echo "✓ Image pushed successfully"

# Push "latest" tag if requested and different from IMAGE_TAG
if [ "${ADD_LATEST_TAG}" = "true" ] || [ "${ADD_LATEST_TAG}" = "1" ]; then
    if [ "${IMAGE_TAG}" != "latest" ]; then
        echo ""
        echo "Pushing 'latest' tag to quay.io..."
        podman push "${LATEST_IMAGE_NAME}"
        
        if [ $? -ne 0 ]; then
            echo "ERROR: Failed to push 'latest' tag to quay.io"
            exit 1
        fi
        
        echo "✓ 'latest' tag pushed successfully"
    fi
fi

echo ""
echo "=========================================="
echo "Success!"
echo "=========================================="
echo ""
echo "Image available at: ${FULL_IMAGE_NAME}"
if [ "${ADD_LATEST_TAG}" = "true" ] || [ "${ADD_LATEST_TAG}" = "1" ]; then
    if [ "${IMAGE_TAG}" != "latest" ]; then
        echo "Also tagged as:     ${LATEST_IMAGE_NAME}"
    fi
fi
echo ""
echo "To run the container:"
if [ "${ADD_LATEST_TAG}" = "true" ] || [ "${ADD_LATEST_TAG}" = "1" ]; then
    echo "  podman run --rm \\"
    echo "    ${LATEST_IMAGE_NAME} \\"
    echo "    --api-key \"your-api-key\" \\"
    echo "    --google-token \"\$(cat GOOGLE_TOKEN)\""
else
    echo "  podman run --rm \\"
    echo "    ${FULL_IMAGE_NAME} \\"
    echo "    --api-key \"your-api-key\" \\"
    echo "    --google-token \"\$(cat GOOGLE_TOKEN)\""
fi
echo ""

#!/bin/bash
# Build and push VEP Police Agent container image to quay.io

set -e

# Default values
QUAY_USERNAME="${QUAY_USERNAME:-$(whoami)}"
IMAGE_NAME="${IMAGE_NAME:-vep-police-agent}"

# Try to get git commit hash for tag, fallback to "latest"
if command -v git &> /dev/null && git rev-parse --git-dir &> /dev/null; then
    GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "")
    if [ -n "$GIT_COMMIT" ]; then
        DEFAULT_TAG="$GIT_COMMIT"
    else
        DEFAULT_TAG="latest"
    fi
else
    DEFAULT_TAG="latest"
fi

IMAGE_TAG="${IMAGE_TAG:-$DEFAULT_TAG}"

# Option to also tag as "latest"
ADD_LATEST_TAG="${ADD_LATEST_TAG:-false}"

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

# Check if already logged in to quay.io and verify username matches
LOGGED_IN_USER=$(podman login --get-login quay.io 2>/dev/null || echo "")
if [ -z "$LOGGED_IN_USER" ]; then
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
elif [ "$LOGGED_IN_USER" != "$QUAY_USERNAME" ]; then
    echo "⚠ Warning: Logged in as '${LOGGED_IN_USER}' but pushing to '${QUAY_USERNAME}' namespace"
    echo "This will likely fail due to permission issues."
    echo ""
    read -p "Do you want to login as '${QUAY_USERNAME}' instead? [y/N]: " relogin
    if [ "$relogin" = "y" ] || [ "$relogin" = "Y" ]; then
        echo ""
        read -sp "Quay.io Password/Token for '${QUAY_USERNAME}': " quay_password
        echo ""
        
        if [ -z "$quay_password" ]; then
            echo "ERROR: Password/Token is required"
            exit 1
        fi
        
        echo "Logging in to quay.io as '${QUAY_USERNAME}'..."
        echo "$quay_password" | podman login quay.io --username "$QUAY_USERNAME" --password-stdin
        
        if [ $? -ne 0 ]; then
            echo "ERROR: Failed to login to quay.io"
            exit 1
        fi
        
        echo "✓ Successfully logged in to quay.io as '${QUAY_USERNAME}'"
        echo ""
    else
        echo "Continuing with current login (may fail)..."
        echo ""
    fi
else
    echo "✓ Already logged in to quay.io as '${QUAY_USERNAME}'"
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

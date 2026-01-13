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

# Full image name
FULL_IMAGE_NAME="quay.io/${QUAY_USERNAME}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "=========================================="
echo "VEP Police Agent - Build and Push Script"
echo "=========================================="
echo ""
echo "Configuration:"
echo "  Quay.io Username: ${QUAY_USERNAME}"
echo "  Image Name:      ${IMAGE_NAME}"
echo "  Image Tag:       ${IMAGE_TAG}"
echo "  Full Image:      ${FULL_IMAGE_NAME}"
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

# Push the image
echo "Pushing image to quay.io..."
podman push "${FULL_IMAGE_NAME}"

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to push image to quay.io"
    exit 1
fi

echo "✓ Image pushed successfully"
echo ""
echo "=========================================="
echo "Success!"
echo "=========================================="
echo ""
echo "Image available at: ${FULL_IMAGE_NAME}"
echo ""
echo "To run the container:"
echo "  podman run --rm \\"
echo "    ${FULL_IMAGE_NAME} \\"
echo "    --api-key \"your-api-key\" \\"
echo "    --google-token \"\$(cat GOOGLE_TOKEN)\""
echo ""

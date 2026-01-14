#!/bin/bash
# Opinionated build and push script with default settings
# Sets default tag to current date in DD_MM_YYYY format

set -e

# Get script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Generate default tag from current date (DD_MM_YYYY format)
DEFAULT_TAG=$(date +"%d_%m_%Y")

# Set default values
export ADD_LATEST_TAG="${ADD_LATEST_TAG:-true}"
export QUAY_USERNAME="${QUAY_USERNAME:-mabekitzur}"
export IMAGE_NAME="${IMAGE_NAME:-vep-police-agent}"
export IMAGE_TAG="${IMAGE_TAG:-${DEFAULT_TAG}}"

# Run the build script from project root
cd "${PROJECT_ROOT}"
exec "${SCRIPT_DIR}/build-and-push.sh"


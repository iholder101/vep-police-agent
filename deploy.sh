Exit code: 0

--- stdout ---
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

CONTAINER_NAME="vagent1"

podman stop "$CONTAINER_NAME" 2>/dev/null || true
podman rm "$CONTAINER_NAME" 2>/dev/null || true
exec ./scripts/run-latest-agent.sh --detach --container-name "$CONTAINER_NAME" --immediate-start "$@"

#!/bin/sh
# Sidecar entrypoint script
# Configures predicate-authorityd based on environment variables

set -e

# Base command
CMD="predicate-authorityd --host 0.0.0.0 --port 8787 --policy-file /app/policy.yaml --log-level ${LOG_LEVEL:-info}"

# Check if control plane URL is provided AND all required fields are present for cloud-connected mode
# Cloud-connected mode requires: CONTROL_PLANE_URL, TENANT_ID, and PROJECT_ID
if [ -n "$CONTROL_PLANE_URL" ] && [ -n "$TENANT_ID" ] && [ -n "$PROJECT_ID" ]; then
    echo "[sidecar] Cloud-connected mode: $CONTROL_PLANE_URL"
    CMD="$CMD --mode cloud_connected"
    CMD="$CMD --control-plane-url $CONTROL_PLANE_URL"
    CMD="$CMD --tenant-id $TENANT_ID"
    CMD="$CMD --project-id $PROJECT_ID"

    # Add API key if provided
    if [ -n "$PREDICATE_API_KEY" ]; then
        CMD="$CMD --predicate-api-key $PREDICATE_API_KEY"
    fi

    # Enable sync if requested
    if [ "$SYNC_ENABLED" = "true" ]; then
        CMD="$CMD --sync-enabled"
    fi

    # Allow local fallback for cloud_connected mode with local identity
    # This is required when not using external IdP for identity verification
    CMD="$CMD --allow-local-fallback"
else
    if [ -n "$CONTROL_PLANE_URL" ]; then
        echo "[sidecar] Warning: CONTROL_PLANE_URL set but missing TENANT_ID or PROJECT_ID"
        echo "[sidecar] Falling back to local-only mode"
    fi
    echo "[sidecar] Local-only mode"
    CMD="$CMD --mode local_only"
fi

# Run the sidecar
echo "[sidecar] Starting: $CMD run"
exec $CMD run

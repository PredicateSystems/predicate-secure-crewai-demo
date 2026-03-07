#!/bin/bash
# Sidecar entrypoint script
# Configures predicate-authorityd based on environment variables

set -e

# Base command
CMD="predicate-authorityd --host 0.0.0.0 --port 8787 --policy-file /app/policy.yaml --log-level ${LOG_LEVEL:-info}"

# Check if control plane URL is provided for cloud-connected mode
if [ -n "$CONTROL_PLANE_URL" ]; then
    echo "[sidecar] Cloud-connected mode: $CONTROL_PLANE_URL"
    CMD="$CMD --mode cloud_connected"
    CMD="$CMD --control-plane-url $CONTROL_PLANE_URL"

    # Add API key if provided
    if [ -n "$PREDICATE_API_KEY" ]; then
        CMD="$CMD --predicate-api-key $PREDICATE_API_KEY"
    fi

    # Add tenant ID if provided
    if [ -n "$TENANT_ID" ]; then
        CMD="$CMD --tenant-id $TENANT_ID"
    fi

    # Add project ID if provided
    if [ -n "$PROJECT_ID" ]; then
        CMD="$CMD --project-id $PROJECT_ID"
    fi

    # Enable sync if requested
    if [ "$SYNC_ENABLED" = "true" ]; then
        CMD="$CMD --sync-enabled"
    fi
else
    echo "[sidecar] Local-only mode"
    CMD="$CMD --mode local_only"
fi

# Run the sidecar
echo "[sidecar] Starting: $CMD run"
exec $CMD run

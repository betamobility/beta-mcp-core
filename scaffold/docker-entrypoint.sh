#!/bin/sh
# Canonical entrypoint for Beta MCP servers on Railway.
#
# Fix ownership of the mounted OAuth-state volume, then drop root.
#
# Railway mounts volumes root-owned; the app runs as appuser. FASTMCP_HOME
# points fastmcp's encrypted OAuth store (client registrations, tokens) at the
# volume so connector authorizations survive deploys. Without the chown the
# server fails closed at boot (PermissionError creating $FASTMCP_HOME).
#
# With FASTMCP_HOME unset this no-ops apart from the privilege drop, so the
# image can ship before the volume exists (rollout stage 1 of 3 — see
# scaffold/README.md).
set -eu

if [ -n "${FASTMCP_HOME:-}" ]; then
    mkdir -p "$FASTMCP_HOME"
    chown -R appuser:appuser "$FASTMCP_HOME"
fi

exec gosu appuser "$@"

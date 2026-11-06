#!/bin/sh
# Container entry point for both services (panel and scheduler).
# - makes the Claude config folder (mode 700) on the /data volume
# - makes sure Claude Code knows the Upwork MCP server (safe to run on every start)
# - never stops the container because of a claude problem; it only logs a warning
# Then it runs the real command ("$@").

CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/data/claude}"
UPWORK_MCP_URL="${UPWORK_MCP_URL:-https://mcp.upwork.com/mcp}"
export CLAUDE_CONFIG_DIR

warn() { echo "entrypoint: warning: $*" >&2; }

if mkdir -p "$CLAUDE_CONFIG_DIR" && chmod 700 "$CLAUDE_CONFIG_DIR"; then
    # Look for an "upwork" server in .claude.json. Exit 0 = found.
    has_server() {
        python3 - "$CLAUDE_CONFIG_DIR/.claude.json" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
except (OSError, ValueError):
    sys.exit(1)
servers = data.get("mcpServers") if isinstance(data, dict) else None
sys.exit(0 if isinstance(servers, dict) and "upwork" in servers else 1)
PY
    }
    if has_server; then
        :
    else
        echo "entrypoint: adding the Upwork MCP server to $CLAUDE_CONFIG_DIR"
        claude mcp add --transport http --scope user upwork "$UPWORK_MCP_URL" \
            </dev/null >/dev/null 2>&1 \
            || warn "could not add the Upwork MCP server (claude mcp add failed)"
    fi
else
    warn "could not create $CLAUDE_CONFIG_DIR"
fi

exec "$@"

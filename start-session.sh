#!/usr/bin/env bash
# Start the Voice Router MCP Server (shared, single instance).
#
# Run once. All Claude Code sessions connect to the same endpoint.
#
# Usage: start-session.sh [PORT] [VOICE]
#   PORT  : MCP port (default: 8766)
#   VOICE : Kokoro voice (default: af_sky)
#
# Add to Claude Code once:
#   claude mcp add --transport http voice-router http://127.0.0.1:8766/mcp

set -e
PORT="${1:-8766}"
VOICE="${2:-af_sky}"

PYTHON=~/.local/share/uv/tools/voice-mode/bin/python
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export VOICE_ROUTER_PORT="$PORT"
export VOICE_ROUTER_VOICE="$VOICE"

echo "Starting Voice Router MCP Server on port $PORT"
echo "Add to Claude Code (once):  claude mcp add --transport http voice-router http://127.0.0.1:$PORT/mcp"
echo ""
exec "$PYTHON" "$DIR/mcp_server.py"

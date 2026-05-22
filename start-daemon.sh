#!/usr/bin/env bash
# Start the Voice Router Daemon
set -e
PYTHON=~/.local/share/uv/tools/voice-mode/bin/python
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PYTHON" "$DIR/daemon.py"

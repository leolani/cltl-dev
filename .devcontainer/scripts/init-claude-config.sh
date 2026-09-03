#!/usr/bin/env bash
set -euo pipefail

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-/home/vscode/.claude}"
SEED_DIR="/home/vscode/.claude-seed"
MARKER="$CLAUDE_DIR/.initialized-from-seed"

mkdir -p "$CLAUDE_DIR"

# The shared credentials file is bind-mounted from the host. If the host path did
# not exist at create time, Docker silently makes a directory here instead, which
# fails in confusing ways later. Check on every start, not just first init.
CREDS="$CLAUDE_DIR/.credentials.json"
if [ -d "$CREDS" ]; then
  echo "WARNING: $CREDS is a directory, not a file." >&2
  echo "  The host mount source is missing. On the host run:" >&2
  echo "    mkdir -p ~/.claude-shared && touch ~/.claude-shared/.credentials.json" >&2
  echo "    chmod 600 ~/.claude-shared/.credentials.json" >&2
  echo "  then rebuild the container." >&2
fi

if [ -e "$MARKER" ]; then
  echo "Claude config already initialized from seed."
  exit 0
fi

if [ -d "$SEED_DIR" ] && [ "$(find "$SEED_DIR" -mindepth 1 -maxdepth 1 | head -n 1)" ]; then
  echo "Copying Claude seed config into private volume..."
  cp -R "$SEED_DIR"/. "$CLAUDE_DIR"/
else
  echo "No Claude seed config found; continuing with empty config."
fi

# chown fails on the bind-mounted credentials file; that is expected and harmless.
sudo chown -R vscode:vscode "$CLAUDE_DIR" || true
touch "$MARKER"

echo "Claude config initialized."

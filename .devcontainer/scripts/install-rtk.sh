#!/usr/bin/env bash
set -euo pipefail

# Install rtk (Rust Token Killer, rtk-ai/rtk) for the vscode user and enable the
# Claude Code hook so commands are transparently proxied through rtk.
#
# Idempotent: safe to re-run. Runs as the remoteUser (vscode) during
# postCreateCommand, so rtk lands in ~/.local/bin (already on PATH).
#
# Docs: https://www.rtk-ai.app/docs/getting-started/installation/
# NOTE: this installs rtk-ai/rtk (Rust Token Killer), NOT the unrelated
# Rust Type Kit that shares the name.

BIN_DIR="${HOME}/.local/bin"

if command -v rtk >/dev/null 2>&1; then
  echo "rtk already installed: $(rtk --version)"
else
  echo "Installing rtk from the official installer..."
  curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/master/install.sh | sh
fi

# Ensure ~/.local/bin is on PATH for the remaining steps of this script.
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) export PATH="${BIN_DIR}:${PATH}" ;;
esac

if ! command -v rtk >/dev/null 2>&1; then
  echo "ERROR: rtk not found on PATH after install." >&2
  exit 1
fi

# Enable the Claude Code hook globally (transparent command rewriting).
# Non-fatal: a hook failure should not break container creation.
echo "Enabling rtk Claude Code hook (rtk init --global)..."
rtk init --global || echo "WARNING: 'rtk init --global' failed; run it manually to enable the hook."

rtk --version
echo "rtk install complete."

#!/usr/bin/env bash
# Remove all test runtime data while preserving the directory skeleton.
#
# Run this before a fresh test session to ensure tests start from a clean state:
#   bash tests/integration/storage/clean.sh
#
# Keeps: .gitkeep, .gitignore, clean.sh, and the subdirectory entries themselves.
# Removes: WAV files, log files, emissor JSON, event logs, RabbitMQ Mnesia data.
set -euo pipefail

STORAGE_DIR="$(cd "$(dirname "$0")" && pwd)"

find "$STORAGE_DIR" \
    -mindepth 1 \
    ! -name '.gitkeep' \
    ! -name '.gitignore' \
    ! -name 'clean.sh' \
    ! -path "$STORAGE_DIR/audio" \
    ! -path "$STORAGE_DIR/audio/.gitkeep" \
    ! -path "$STORAGE_DIR/image" \
    ! -path "$STORAGE_DIR/image/.gitkeep" \
    ! -path "$STORAGE_DIR/rabbitmq" \
    ! -path "$STORAGE_DIR/rabbitmq/.gitkeep" \
    -delete 2>/dev/null || true

echo "Test storage cleaned: $STORAGE_DIR"

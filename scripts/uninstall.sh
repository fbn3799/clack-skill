#!/usr/bin/env bash
# Clack Voice Relay — uninstall
set -euo pipefail

echo "=== Clack Voice Relay — Uninstall ==="

# Stop and remove systemd service
if [[ -f /etc/systemd/system/clack.service ]]; then
  echo "Stopping clack service..."
  systemctl stop clack 2>/dev/null || true
  systemctl disable clack 2>/dev/null || true
  rm -f /etc/systemd/system/clack.service
  systemctl daemon-reload
  echo "  ✓ Service removed"
else
  echo "  No clack service found"
fi

# Resolve skill directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Remove venv
if [[ -d "$SKILL_DIR/venv" ]]; then
  rm -rf "$SKILL_DIR/venv"
  echo "  ✓ Python venv removed"
fi

echo ""
echo "Uninstall complete."
echo "The skill directory ($SKILL_DIR) is still on disk."
echo "To fully remove: rm -rf $SKILL_DIR"
echo ""

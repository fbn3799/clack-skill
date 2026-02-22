#!/usr/bin/env bash
# Clack Voice Relay — one-liner installer
# Usage: curl -fsSL https://raw.githubusercontent.com/fbn3799/clack-skill/master/scripts/install.sh | bash
set -euo pipefail

INSTALL_DIR="${HOME}/.openclaw/workspace/skills/clack"

echo "=== Clack Voice Relay — Install ==="
echo ""

# Check for git
if ! command -v git &>/dev/null; then
  echo "Installing git..."
  sudo apt-get update -qq && sudo apt-get install -y -qq git
fi

# Clone or update
if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "Updating existing installation..."
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "Cloning clack-skill..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone https://github.com/fbn3799/clack-skill.git "$INSTALL_DIR"
fi

echo ""

# Run setup
exec bash "$INSTALL_DIR/scripts/setup.sh" "$@"

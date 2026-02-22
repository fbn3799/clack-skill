#!/usr/bin/env bash
# Clack Voice Relay — one-liner installer
# Usage: curl -fsSL https://raw.githubusercontent.com/fbn3799/clack-skill/master/scripts/install.sh | sudo bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:"
  echo "  curl -fsSL https://raw.githubusercontent.com/fbn3799/clack-skill/master/scripts/install.sh | sudo bash"
  exit 1
fi

# Preserve real user's home for OpenClaw config detection
if [[ -n "${SUDO_USER:-}" ]]; then
  INSTALL_DIR="$(eval echo "~$SUDO_USER")/.openclaw/workspace/skills/clack"
else
  INSTALL_DIR="${HOME}/.openclaw/workspace/skills/clack"
fi

echo "=== Clack Voice Relay — Install ==="
echo ""

# Check for git
if ! command -v git &>/dev/null; then
  echo "Installing git..."
  apt-get update -qq && apt-get install -y -qq git
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

# Run setup (restore terminal stdin for interactive prompts)
exec bash "$INSTALL_DIR/scripts/setup.sh" "$@" </dev/tty

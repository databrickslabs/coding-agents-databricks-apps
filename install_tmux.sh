#!/bin/bash
# Install a statically-linked tmux to ~/.local/bin.
#
# The Omnigent native harnesses (`omnigent claude`, `omnigent codex`) launch
# the agent through a local tmux terminal and REFUSE to start without tmux on
# PATH (onboarding/harness_readiness.py). Databricks Apps containers have no apt
# and no system tmux, so we fetch a fully-static build (no libevent/ncurses
# runtime deps) from mjakob-gh/build-static-tmux — the same fetch-a-binary
# pattern as install_gh.sh / install_databricks_cli.sh.

set -euo pipefail

INSTALL_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR"

if command -v tmux >/dev/null 2>&1; then
  echo "tmux already on PATH: $(command -v tmux)"
  exit 0
fi

TMUX_VERSION="v3.6b"

_ARCH=$(uname -m)
case "$_ARCH" in
  x86_64)        _ARCH="amd64" ;;
  aarch64|arm64) _ARCH="arm64" ;;
  *) echo "unsupported arch $_ARCH for static tmux; skipping" >&2; exit 0 ;;
esac

ASSET="tmux.linux-${_ARCH}.stripped.gz"
URL="https://github.com/mjakob-gh/build-static-tmux/releases/download/${TMUX_VERSION}/${ASSET}"

echo "Installing static tmux ${TMUX_VERSION} (${_ARCH})"
curl -fsSL "$URL" -o /tmp/tmux.gz
gunzip -f /tmp/tmux.gz
mv /tmp/tmux "$INSTALL_DIR/tmux"
chmod +x "$INSTALL_DIR/tmux"

echo "tmux installed to $INSTALL_DIR/tmux ($("$INSTALL_DIR/tmux" -V 2>/dev/null || echo 'version check failed'))"

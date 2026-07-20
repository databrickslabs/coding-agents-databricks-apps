#!/bin/bash
# Install a statically-linked jq to ~/.local/bin.
#
# The Omnigent native harnesses (pi / claude / codex) resolve their Databricks
# gateway bearer via an auth command that ends in `... --output json | jq -r
# '.access_token'` (omnigent.inner.codex_executor._databricks_codex_auth_command).
# Without jq on PATH the pipe yields an empty token and the harness reports
# "Failed to resolve API key". Databricks Apps containers have no apt and no
# system jq, so fetch the official fully-static build from jqlang/jq — the same
# fetch-a-binary pattern as install_tmux.sh / install_gh.sh.

set -euo pipefail

INSTALL_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR"

if command -v jq >/dev/null 2>&1; then
  echo "jq already on PATH: $(command -v jq)"
  exit 0
fi

JQ_VERSION="1.7.1"

_ARCH=$(uname -m)
case "$_ARCH" in
  x86_64)        _ARCH="amd64" ;;
  aarch64|arm64) _ARCH="arm64" ;;
  *) echo "unsupported arch $_ARCH for static jq; skipping" >&2; exit 0 ;;
esac

ASSET="jq-linux-${_ARCH}"
URL="https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/${ASSET}"

echo "Installing static jq ${JQ_VERSION} (${_ARCH})"
curl -fsSL "$URL" -o "$INSTALL_DIR/jq"
chmod +x "$INSTALL_DIR/jq"

echo "jq installed to $INSTALL_DIR/jq ($("$INSTALL_DIR/jq" --version 2>/dev/null || echo 'version check failed'))"

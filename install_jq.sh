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
#
# Enterprise mode: redirect upstream URLs to internal mirrors when configured.
# See docs/enterprise.md for the env-var contract. Without this, a firewalled
# deployment can't reach github.com and the harnesses lose their bearer token.

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

GH_RELEASES="${GITHUB_RELEASE_MIRROR:-https://github.com}"

ASSET="jq-linux-${_ARCH}"
URL="${GH_RELEASES}/jqlang/jq/releases/download/jq-${JQ_VERSION}/${ASSET}"

echo "Installing static jq ${JQ_VERSION} (${_ARCH})"
# Download to a temp path first, then verify it actually runs before installing
# it as `jq`. A truncated or HTML-error-page download would otherwise land on
# PATH as an executable that silently produces an empty token — the exact
# failure mode this script exists to prevent.
_TMP=$(mktemp)
trap 'rm -f "$_TMP"' EXIT

# Best-effort, like the unsupported-arch path above: jq is only needed by the
# Omnigent native-harness auth command, which is off unless the Omnigent
# resources are attached. Failing this step hard would mark the whole app setup
# as errored on any firewalled deploy without a jq mirror configured, which is
# disproportionate — and if jq really is needed and absent, the harness reports
# "Failed to resolve API key", which is a clearer and more local signal.
if ! curl -fsSL "$URL" -o "$_TMP"; then
  echo "could not download jq from $URL — skipping (set GITHUB_RELEASE_MIRROR for firewalled networks)" >&2
  exit 0
fi
chmod +x "$_TMP"
# Verify it actually runs before installing it as `jq`. A truncated download or
# an HTML error page would otherwise land on PATH as an executable that silently
# produces an empty token — the exact failure this script exists to prevent.
if ! "$_TMP" --version >/dev/null 2>&1; then
  echo "downloaded jq is not a working binary (from $URL) — skipping" >&2
  exit 0
fi
mv "$_TMP" "$INSTALL_DIR/jq"
trap - EXIT

echo "jq installed to $INSTALL_DIR/jq ($("$INSTALL_DIR/jq" --version 2>/dev/null || echo 'version check failed'))"

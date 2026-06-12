#!/bin/bash
# Install the latest Databricks CLI to ~/.local/bin.
#
# - Fetches the latest release tag from the GitHub API
# - Downloads and unzips the Linux amd64 binary
# - Prints the installed version

set -euo pipefail

INSTALL_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR"

# Enterprise mode: redirect upstream URLs to internal mirrors when configured.
# See docs/enterprise.md for the env-var contract.
GH_API="${GITHUB_API_BASE:-https://api.github.com}"
GH_RELEASES="${GITHUB_RELEASE_MIRROR:-https://github.com}"

# Fetch latest release tag
DB_CLI_VERSION=$(curl -fsSL "${GH_API}/repos/databricks/cli/releases/latest" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))")

echo "Installing Databricks CLI v${DB_CLI_VERSION}"

curl -fsSL "${GH_RELEASES}/databricks/cli/releases/download/v${DB_CLI_VERSION}/databricks_cli_${DB_CLI_VERSION}_linux_amd64.zip" \
  -o /tmp/dbcli.zip
unzip -o /tmp/dbcli.zip -d /tmp/dbcli
mv /tmp/dbcli/databricks "$INSTALL_DIR/databricks"
rm -rf /tmp/dbcli.zip /tmp/dbcli
chmod +x "$INSTALL_DIR/databricks"

"$INSTALL_DIR/databricks" --version

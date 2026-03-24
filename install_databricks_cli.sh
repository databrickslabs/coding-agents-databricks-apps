#!/bin/bash
# Install the latest Databricks CLI to ~/.local/bin.
#
# - Fetches the latest release tag from the GitHub API
# - Downloads and unzips the Linux amd64 binary
# - Prints the installed version

set -euo pipefail

INSTALL_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR"

# Fetch latest release tag
DB_CLI_VERSION=$(curl -fsSL "https://api.github.com/repos/databricks/cli/releases/latest" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))")

echo "Installing Databricks CLI v${DB_CLI_VERSION}"

curl -fsSL "https://github.com/databricks/cli/releases/download/v${DB_CLI_VERSION}/databricks_cli_${DB_CLI_VERSION}_linux_amd64.zip" \
  -o /tmp/dbcli.zip
unzip -o /tmp/dbcli.zip -d /tmp/dbcli
mv /tmp/dbcli/databricks "$INSTALL_DIR/databricks"
rm -rf /tmp/dbcli.zip /tmp/dbcli
chmod +x "$INSTALL_DIR/databricks"

"$INSTALL_DIR/databricks" --version

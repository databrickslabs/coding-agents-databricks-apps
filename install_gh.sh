#!/bin/bash
# Install GitHub CLI (gh) to ~/.local/bin with an auth-login wrapper.
#
# - Fetches the latest 2.x release from the GitHub API
# - Installs to ~/.local/bin/gh.real
# - Creates a wrapper at ~/.local/bin/gh that intercepts `gh auth login`
#   to skip interactive prompts (arrow-key menus break in xterm.js PTY)

set -euo pipefail

INSTALL_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR"

# Fetch latest release tag
GH_VERSION=$(curl -fsSL "https://api.github.com/repos/cli/cli/releases/latest" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))")

echo "Installing GitHub CLI v${GH_VERSION}"

curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
  -o /tmp/gh.tar.gz
tar -xzf /tmp/gh.tar.gz -C /tmp
mv "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" "$INSTALL_DIR/gh"
rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_amd64"
chmod +x "$INSTALL_DIR/gh"

# Set git protocol to HTTPS
"$INSTALL_DIR/gh" config set git_protocol https 2>/dev/null || true

# Create wrapper that intercepts `gh auth login` to avoid interactive prompts
cat > "$INSTALL_DIR/gh.wrapper" << 'WRAPPER'
#!/bin/bash
if [ "$1" = "auth" ] && [ "$2" = "login" ]; then
    shift 2
    printf "Y\\n" | ~/.local/bin/gh.real auth login -h github.com -p https -w --skip-ssh-key "$@"
    exit 0
fi
exec ~/.local/bin/gh.real "$@"
WRAPPER

mv "$INSTALL_DIR/gh" "$INSTALL_DIR/gh.real"
mv "$INSTALL_DIR/gh.wrapper" "$INSTALL_DIR/gh"
chmod +x "$INSTALL_DIR/gh"

echo "GitHub CLI v${GH_VERSION} installed to $INSTALL_DIR"

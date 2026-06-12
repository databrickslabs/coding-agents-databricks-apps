# CoDA MCP Client Setup

CoDA exposes an MCP endpoint at `/mcp` on the Databricks App. Databricks Apps use OAuth (not PATs) for authentication, so MCP clients need a stdio bridge that injects fresh OAuth tokens.

## How it works

`tools/coda-bridge.py` is a zero-dependency Python script that:

1. Claude Code launches it as a stdio MCP server
2. It reads JSON-RPC messages from stdin
3. Fetches a fresh OAuth token via `databricks auth token`
4. Forwards requests to the App's HTTP endpoint with the token
5. Returns responses on stdout

Tokens are cached for 30 minutes (they expire after 60).

## Setup

### 1. Copy the bridge script

```bash
mkdir -p ~/.claude/mcp-bridges
cp tools/coda-bridge.py ~/.claude/mcp-bridges/
```

### 2. Add to Claude Code settings

Add this to `mcpServers` in `~/.claude/settings.json`:

```json
"coda-mcp": {
    "type": "stdio",
    "command": "python3",
    "args": ["/path/to/.claude/mcp-bridges/coda-bridge.py"],
    "env": {
        "CODA_MCP_URL": "https://<your-app-name>.databricksapps.com/mcp",
        "DATABRICKS_PROFILE": "<your-databricks-cli-profile>"
    }
}
```

### 3. Restart Claude Code

The MCP server will start automatically on next session.

## Configuration

| Environment Variable | Description | Example |
|---------------------|-------------|---------|
| `CODA_MCP_URL` | Full URL to the app's `/mcp` endpoint | `https://mcp-test-coda-747...com/mcp` |
| `DATABRICKS_PROFILE` | Databricks CLI profile name | `9cefok` |

## Prerequisites

- `databricks` CLI installed and authenticated (`databricks auth login -p <profile>`)
- Python 3.8+
- No pip dependencies required (stdlib only)

## Troubleshooting

Bridge logs go to stderr. Check with:

```bash
CODA_MCP_URL="https://your-app.databricksapps.com/mcp" \
DATABRICKS_PROFILE="your-profile" \
echo '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}' | python3 tools/coda-bridge.py
```

If you see `Auth failed (302)`, your Databricks CLI session may have expired. Run:

```bash
databricks auth login -p <profile>
```

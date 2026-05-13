# Enterprise Mode (Proxy / Registry)

CoDA can run inside locked-down enterprise networks where outbound traffic is
restricted to internal proxies, mirrors, and registries. This page documents
the env-var contract that redirects every external reach.

The default behaviour is unchanged when no enterprise vars are set —
non-enterprise deployments continue to use public PyPI, npmjs.org, GitHub,
and `claude.ai/install.sh`.

## Scope

This feature targets the **proxy/registry** lockdown profile:

- Outbound is allowed but only to internal JFrog Artifactory / Nexus /
  GitHub Enterprise.
- Public PyPI, npmjs.org, github.com, claude.ai are unreachable.
- TLS termination uses a corporate root CA.

Fully air-gapped deployments (zero egress except to `DATABRICKS_HOST`) need
binary vendoring, which is a separate follow-up feature.

## Quick start

1. Mirror the upstream binaries into your JFrog generic-local repo:

   ```
   {generic-repo}/cli/cli/releases/download/v2.50.0/gh_2.50.0_linux_amd64.tar.gz
   {generic-repo}/databricks/cli/releases/download/v0.235.0/databricks_cli_0.235.0_linux_amd64.zip
   {generic-repo}/micro-editor/micro/releases/download/v2.0.13/micro-2.0.13-linux64-static.tar.gz
   ```

   Convention: keep the same path tail as `github.com/.../releases/download/...`.
   No path rewriting needed.

2. Configure your Databricks App's `app.yaml` with the env vars you need
   (see the table below). All are optional — set only what your environment
   requires.

3. Run `make enterprise-doctor` on the deployment host to confirm every
   configured target is reachable before deploying.

4. Deploy as normal: `make deploy`.

## Env-var reference

| Variable | Purpose | Default |
|---|---|---|
| `ENTERPRISE_MODE` | Master switch. When `true`, log a startup banner and warn on missing recommended mirrors. Behavioural overrides are still driven by the individual vars below — this flag is for diagnostics. | unset |
| `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` | Corporate egress proxy. Honoured natively by `curl`, `uv`, `npm`, `git`, `requests`. | unset |
| `REQUESTS_CA_BUNDLE` / `NODE_EXTRA_CA_CERTS` / `SSL_CERT_FILE` | Corporate root CA bundle path (PEM). | unset |
| `UV_DEFAULT_INDEX` | Internal PyPI proxy URL, e.g. `https://jfrog/api/pypi/pypi-virtual/simple/`. | public PyPI |
| `UV_HTTP_TIMEOUT` | Larger timeout for slow proxies. | uv default |
| `UV_INDEX_<name>_USERNAME` / `UV_INDEX_<name>_PASSWORD` | uv-native auth for named indexes. | unset |
| `NPM_REGISTRY` | Internal npm registry URL. Written to `~/.npmrc`. | npmjs.org |
| `NPM_TOKEN` | Bearer token for `NPM_REGISTRY`. Written to `~/.npmrc` as `//host/:_authToken`. | unset |
| `GITHUB_API_BASE` | Replacement for `https://api.github.com` (GitHub Enterprise or JFrog API mirror). | `api.github.com` |
| `GITHUB_RELEASE_MIRROR` | Replacement for `https://github.com` for release downloads. Path tail preserved — point at a JFrog generic-repo. | `github.com` |
| `CLAUDE_INSTALLER_URL` | Override `https://claude.ai/install.sh`. | upstream |
| `HERMES_PIP_URL` | Override `git+https://github.com/NousResearch/hermes-agent.git`. Can be a mirrored git URL or an internal-index package spec. | upstream git URL |
| `DEEPWIKI_MCP_URL` / `EXA_MCP_URL` | Override or set empty to omit these MCP servers entirely. | upstream URLs |

## Mirror conventions

### GitHub releases

`GITHUB_RELEASE_MIRROR` must serve assets at the **same path tail** as `github.com`:

```
{mirror}/{owner}/{repo}/releases/download/{tag}/{asset}
```

So `https://github.com/cli/cli/releases/download/v2.50.0/gh.tar.gz` becomes
`{mirror}/cli/cli/releases/download/v2.50.0/gh.tar.gz`.

In JFrog Artifactory, a *Generic* repository or a *Generic Remote* repo proxying
github.com works without configuration.

### GitHub API

`GITHUB_API_BASE` replaces the hostname *and* prefix:

```
https://api.github.com/repos/cli/cli/releases/latest
                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                       path preserved
```

For GitHub Enterprise, the base path is typically `/api/v3`:
`https://ghe.example.com/api/v3` — install scripts will hit
`https://ghe.example.com/api/v3/repos/cli/cli/releases/latest`.

### npm

`~/.npmrc` is written automatically at app startup when `NPM_REGISTRY` is set.
Format:

```
registry=https://jfrog.example.com/api/npm/npm-virtual/
//jfrog.example.com/:_authToken=<NPM_TOKEN>
always-auth=true
```

### PyPI

uv reads `UV_DEFAULT_INDEX` from the environment. No file written; configuration
is process-environment only.

For named-index auth (when your proxy needs HTTP basic auth):

```yaml
- name: UV_DEFAULT_INDEX
  value: https://jfrog.example.com/api/pypi/pypi-virtual/simple/
- name: UV_INDEX_INTERNAL_USERNAME
  value: svc-coda
- name: UV_INDEX_INTERNAL_PASSWORD
  value: <secret>
```

## Sample `app.yaml` snippet

```yaml
env:
  # Master switch — enables startup banner and missing-mirror warnings.
  - name: ENTERPRISE_MODE
    value: "true"

  # Corporate egress proxy.
  - name: HTTPS_PROXY
    value: http://proxy.corp.example.com:3128
  - name: NO_PROXY
    value: localhost,127.0.0.1,.corp.example.com

  # Corporate root CA.
  - name: REQUESTS_CA_BUNDLE
    value: /etc/ssl/certs/corp-root.pem
  - name: NODE_EXTRA_CA_CERTS
    value: /etc/ssl/certs/corp-root.pem

  # Internal PyPI proxy.
  - name: UV_DEFAULT_INDEX
    value: https://jfrog.example.com/api/pypi/pypi-virtual/simple/

  # Internal npm registry.
  - name: NPM_REGISTRY
    value: https://jfrog.example.com/api/npm/npm-virtual/
  - name: NPM_TOKEN
    value: <use a Databricks secret reference here>

  # GitHub mirror (releases + API).
  - name: GITHUB_RELEASE_MIRROR
    value: https://jfrog.example.com/artifactory/github-mirror
  - name: GITHUB_API_BASE
    value: https://ghe.example.com/api/v3

  # Drop the public MCP servers from agent configs.
  - name: DEEPWIKI_MCP_URL
    value: ""
  - name: EXA_MCP_URL
    value: ""
```

## Troubleshooting

### `HTTP 407 Proxy Authentication Required`

Your proxy needs credentials. Include them in the URL:
`HTTPS_PROXY=http://user:pass@proxy.corp.example.com:3128`. The startup banner
masks the password in logs.

### `SSL: CERTIFICATE_VERIFY_FAILED`

The corporate root CA isn't in the trust store. Set
`REQUESTS_CA_BUNDLE` (Python), `NODE_EXTRA_CA_CERTS` (npm/node), and
`SSL_CERT_FILE` (catch-all) to a PEM bundle that includes your corp root.

### `npm ERR! 401 Unauthorized`

`NPM_TOKEN` is missing, wrong, or expired. Confirm with:
```bash
curl -H "Authorization: Bearer $NPM_TOKEN" "$NPM_REGISTRY"
```

### `uv: error: Could not connect to index`

Check that `UV_DEFAULT_INDEX` ends in `/simple/` (the PEP 503 path). JFrog
typically exposes PyPI as `/api/pypi/<repo>/simple/`.

### `gh: failed to fetch latest release tag`

`GITHUB_API_BASE` is unreachable or doesn't proxy the GitHub API. For
JFrog-style mirrors, you may need to set the base to point at a service
that proxies `api.github.com`, or pin the version manually in the install
script.

## Known gotcha: `requests` from GitHub in `pyproject.toml`

`pyproject.toml` currently pins `requests` to a direct GitHub source:

```toml
[tool.uv.sources]
requests = { git = "https://github.com/psf/requests", rev = "v2.33.0" }
```

This bypasses the PyPI proxy and breaks in enterprise environments. Two
workarounds for customers:

1. **Mirror `psf/requests` in your internal git** and override the URL with
   a `pyproject.toml` patch in your deployment overlay.
2. **Remove the override entirely** if your internal PyPI proxy has
   `requests>=2.33.0` available.

This is tracked as a follow-up — the override exists for transient Databricks
internal-proxy gaps, and will be removed once those gaps close.

## Pre-deploy reachability check

Run `make enterprise-doctor` from the deployment host:

```
$ make enterprise-doctor
enterprise_config: effective settings
  ENTERPRISE_MODE=true
  HTTPS_PROXY=http://proxy.corp.example.com:3128
  ...
[PASS] NPM_REGISTRY        https://jfrog.example.com/api/npm/npm-virtual/  HTTP 200
[PASS] UV_DEFAULT_INDEX    https://jfrog.example.com/api/pypi/pypi-virtual/simple/  HTTP 200
[PASS] GITHUB_RELEASE_MIRROR  https://jfrog.example.com/artifactory/github-mirror  HTTP 200
```

Any FAIL line is something your network team needs to fix before deployment.

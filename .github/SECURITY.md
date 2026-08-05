# Security policy

CoDA (Coding Agents on Databricks Apps) runs inside a customer's Databricks workspace and holds the user's PAT in-process. Vulnerabilities in CoDA can affect the security posture of every workspace that deploys it — we take responsible disclosure seriously.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

We support two private disclosure channels, in order of preference:

1. **GitHub private vulnerability reporting** (preferred).
   Open a security advisory at
   <https://github.com/databrickslabs/coding-agents-databricks-apps/security/advisories/new>.
   The advisory is visible only to maintainers and to the reporter.

2. **Email**: `databrickslabs@databricks.com` with subject prefix
   `[SECURITY][coding-agents-databricks-apps]`. Encrypt sensitive
   details with PGP if available
   ([Databricks Labs PGP key](https://github.com/databrickslabs/.github/blob/main/SECURITY.md#pgp)).

When reporting, please include:

- The version / commit SHA you observed the vulnerability on
- Reproduction steps (a minimal `app.yaml` + repro command is ideal)
- The impact you believe an attacker could achieve
- Any mitigating circumstances or proof-of-concept code

## Response timeline

We commit to the following turnaround on a best-effort basis:

| Phase | Target |
|---|---|
| Acknowledgement of receipt | 2 business days |
| Initial triage + severity assignment | 5 business days |
| Fix or mitigation plan | 14 business days |
| Coordinated disclosure | 90 days from initial report (or sooner if a fix is shipped) |

Severity assignment follows [CVSS v3.1](https://www.first.org/cvss/v3.1/specification-document):

| Severity | Patch SLA |
|---|---|
| Critical (9.0–10.0) | 7 days |
| High (7.0–8.9) | 14 days |
| Medium (4.0–6.9) | 30 days |
| Low (< 4.0) | next scheduled release |

The SLAs above are calendar days from confirmed-and-reproducible to shipped patch. The 7-day cooldown we apply to npm and PyPI dependencies (see `utils.get_npm_version` and `[tool.uv] exclude-newer` in `pyproject.toml`) does *not* apply to CoDA's own security patches — those ship as soon as the fix is reviewed and tested.

## Scope

In scope:

- The CoDA application code (`app.py`, `setup_*.py`, `install_*.sh`, `pat_rotator.py`, `utils.py`, `enterprise_config.py`, etc.)
- The release artifacts attached to GitHub Releases
- The deployment pipeline (`Makefile`, `databricks.yml`, `app.yaml.template`)

Out of scope (report to the relevant project):

- Vulnerabilities in Databricks Apps itself (report to Databricks security
  via your support channel)
- Vulnerabilities in upstream agent CLIs (Claude Code, OpenCode, Codex,
  Gemini CLI, Hermes) — report to those projects
- Vulnerabilities in upstream Python or npm packages — report to those
  maintainers, then notify us so we can update the pin

## Coordinated disclosure

We follow the principles in
[disclose.io](https://disclose.io/terms/) and will:

- Not pursue legal action against good-faith researchers
- Credit reporters in the release notes / advisory unless they prefer
  anonymity
- Share an advance copy of the patch advisory with the reporter before
  public disclosure

## Supply chain controls

For reviewers conducting vendor security assessments (SIG, CAIQ, etc.):

- **npm dependencies** are resolved with a 7-day release-age cooldown
  (`utils.get_npm_version`), and pinned to specific versions before each
  `npm install -g` (see `setup_codex.py`, `setup_gemini.py`,
  `setup_opencode.py`).
- **PyPI dependencies** use `[tool.uv] exclude-newer = "7 days"` (see
  `pyproject.toml`) and are pinned in `requirements.txt` / `uv.lock`.
- **Hermes** is installed from a SHA-pinned git URL (see
  `enterprise_config.DEFAULT_HERMES_PIN_SHA`, overridable via `HERMES_PIP_URL`
  for mirrored installs); the pin is rotated deliberately on CoDA releases,
  not auto-updated.
- **Enterprise mode** (see `docs/enterprise.md`) routes all dependency
  fetches through an operator-configured proxy (JFrog Artifactory / Nexus
  / internal PyPI) instead of public registries.
- **CVE scanning** runs on every push via `.github/workflows/dependency-audit.yml`.
- **Software Bill of Materials (SBOM)** is attached to each GitHub Release
  as a CycloneDX-format JSON file, signed with cosign keyless OIDC (see
  `.github/workflows/release.yml`). [docs/SECURITY.md](../docs/SECURITY.md)
  has the verification commands.

## Known limitations

`docs/enterprise.md` § *Security model and known limits* enumerates the
deliberate trade-offs in the current design (no mirror-binary checksum
verification, no mirror allow-listing, single-user authorization model,
etc.). These are not vulnerabilities — they are documented threat-model
boundaries. Disclosed gaps are tracked publicly there so reviewers can
make informed risk decisions.

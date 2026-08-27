"""Guard the Apps-overlay foot-gun.

`databricks apps deploy` with an overlay **replaces** `app.yaml` wholesale — it
does not merge.
So any env var that exists in the base `app.yaml` but is missing from an overlay
silently disappears from the deployed container and falls back to whatever the
consuming code defaults to.

For the per-CLI install toggles that default is "install it" (each setup script
reads `os.environ.get("ENABLE_<CLI>", "true")`), so an omitted toggle is not a
no-op: it turns an intentionally-disabled agent back on. These tests assert every
tracked overlay declares the full set.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every ENABLE_* toggle honoured by a setup script. Keep in sync with the
# `os.environ.get("ENABLE_...")` reads in setup_*.py — test_toggles_match_code
# below fails if a new one is added to the code but not listed here.
CLI_TOGGLES = frozenset(
    {
        "ENABLE_HERMES",
        "ENABLE_PI",
        "ENABLE_OPENCODE",
        "ENABLE_CODEX",
        "ENABLE_GEMINI",
        "ENABLE_CLAUDE",
    }
)


def _tracked_app_yamls() -> list[Path]:
    """Return the explicitly supported replacement-manifest set."""
    return [
        REPO_ROOT / name
        for name in ("app.yaml", "app.yaml.template", "app.yaml.workshop")
        if (REPO_ROOT / name).is_file()
    ]


def _env_names(path: Path) -> set[str]:
    parsed = yaml.safe_load(path.read_text())
    return {entry["name"] for entry in parsed.get("env", [])}


def test_finds_the_overlays():
    """Sanity check — a silent empty list would make the tests below vacuous."""
    names = {p.name for p in _tracked_app_yamls()}
    assert "app.yaml" in names
    assert names == {"app.yaml", "app.yaml.template", "app.yaml.workshop"}


@pytest.mark.parametrize(
    "path", _tracked_app_yamls(), ids=lambda p: p.name
)
def test_overlay_declares_every_cli_toggle(path: Path):
    missing = CLI_TOGGLES - _env_names(path)
    assert not missing, (
        f"{path.name} omits {sorted(missing)}. Overlays replace app.yaml rather "
        f"than merging with it, and each ENABLE_* defaults to true when absent — "
        f"so an omitted toggle silently re-enables that CLI's install."
    )


@pytest.mark.parametrize(
    "path", _tracked_app_yamls(), ids=lambda p: p.name
)
def test_toggle_values_are_quoted_booleans(path: Path):
    """`value: true` (unquoted) parses as a YAML bool, not the string the setup
    scripts call .strip().lower() on. Keep them quoted."""
    parsed = yaml.safe_load(path.read_text())
    for entry in parsed.get("env", []):
        if entry["name"] in CLI_TOGGLES:
            assert entry["value"] in ("true", "false"), (
                f"{path.name}: {entry['name']} is {entry['value']!r}; expected the "
                f'quoted string "true" or "false"'
            )


@pytest.mark.parametrize(
    "path", _tracked_app_yamls(), ids=lambda p: p.name
)
def test_codex_and_gemini_are_enabled_with_compatible_defaults(path: Path):
    """Every supported replacement manifest installs the two default agents."""
    env = {
        entry["name"]: entry.get("value")
        for entry in (yaml.safe_load(path.read_text()) or {}).get("env", [])
    }
    assert env["ENABLE_CODEX"] == "true"
    assert env["ENABLE_GEMINI"] == "true"
    # The generic system.ai request is resolved to the newest Responses-capable
    # model from the workspace catalog during setup.
    assert str(env["ANTHROPIC_MODEL"]).startswith("system.ai.claude-")
    assert str(env["PI_MODEL"]).startswith("system.ai.claude-")
    assert str(env["CODEX_MODEL"]).startswith("system.ai.gpt-")
    assert str(env["GEMINI_MODEL"]).startswith("system.ai.gemini-")


def test_codex_fallback_model_uses_system_ai_discovery_seed():
    """The no-env setup path starts from the system.ai discovery namespace."""
    source = (REPO_ROOT / "setup_codex.py").read_text()
    assert 'os.environ.get("CODEX_MODEL", "system.ai.gpt-5")' in source


def test_toggles_match_code():
    """Every ENABLE_* the setup scripts read must be listed in CLI_TOGGLES."""
    grep = subprocess.run(
        ["git", "grep", "-ho", r"ENABLE_[A-Z]*", "--", "setup_*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    found = set(grep.stdout.split())
    # ENABLE_SP_APIKEYHELPER is an auth switch, not a per-CLI install toggle.
    found.discard("ENABLE_SP_APIKEYHELPER")
    unlisted = found - CLI_TOGGLES
    assert not unlisted, (
        f"setup scripts read {sorted(unlisted)} but the overlay guard doesn't "
        f"know about it — add it to CLI_TOGGLES and to every supported overlay."
    )


# ---------------------------------------------------------------------------
# No workspace-specific values committed to a public repo
# ---------------------------------------------------------------------------
#
# docs/agent-instructions.md §5: personal/workspace values must be commented out
# or defaulted off before landing on main. This repo is public, and these values
# had accumulated on main — a customer workspace id, a UC catalog, a personal
# sandbox repo, and a specific app URL — each of which is both an information
# leak and actively wrong for anyone else deploying the template.
#
# The subtle failure they cause: DATABRICKS_GATEWAY_HOST is TRUSTED with no
# reachability probe (utils.get_gateway_host tier 1), so a stale committed URL
# means every model call returns "400 Invalid Token" rather than falling back.
# Leaving it unset lets tier 2 derive and probe the right gateway per workspace.

# Patterns that indicate a value belongs to one specific workspace/person.
WORKSPACE_SPECIFIC = [
    (r"adb-\d{10,}", "an Azure workspace id"),
    (r"\bdbc-[0-9a-f]{4,}-[0-9a-f]{4,}", "an AWS workspace host"),
    (r"/Volumes/[a-z0-9_]*(sandbox|prod|dev)[a-z0-9_]*/", "a concrete UC Volume path"),
    (r"[a-z0-9_]*(sandbox|prod|dev)[a-z0-9_]*", "a specific environment catalog"),
    (r"github\.com/[A-Za-z0-9._-]*(okeeffe|dgokeeffe)", "a personal GitHub repo"),
]


def _uncommented_values(path: Path) -> list[tuple[str, str]]:
    """(name, value) for every ACTIVE env entry with a literal string value.

    Commented-out examples are fine — they're documentation, and placeholders
    like `<your-workspace>` live there.
    """
    parsed = yaml.safe_load(path.read_text())
    out = []
    for entry in parsed.get("env", []):
        value = entry.get("value")
        if isinstance(value, str):
            out.append((entry["name"], value))
    return out


@pytest.mark.parametrize("path", _tracked_app_yamls(), ids=lambda p: p.name)
def test_no_workspace_specific_values(path: Path):
    problems = []
    for name, value in _uncommented_values(path):
        for pattern, description in WORKSPACE_SPECIFIC:
            if re.search(pattern, value, re.IGNORECASE):
                problems.append(f"{name} = {value!r} contains {description}")
    assert not problems, (
        f"{path.name} commits workspace-specific values to a public repo:\n  "
        + "\n  ".join(problems)
        + "\n\nComment the entry out with a <placeholder>, or resolve it via a "
          "`valueFrom` resource reference. See docs/agent-instructions.md §5."
    )


@pytest.mark.parametrize("path", _tracked_app_yamls(), ids=lambda p: p.name)
def test_gateway_host_is_not_pinned_to_a_workspace(path: Path):
    """DATABRICKS_GATEWAY_HOST set to a URL is trusted without probing, so a
    committed one breaks every other workspace. Unset (auto-derive) or
    commented is correct; note "" means DISABLE, which is a third behaviour."""
    for name, value in _uncommented_values(path):
        if name == "DATABRICKS_GATEWAY_HOST" and value.strip():
            pytest.fail(
                f"{path.name} pins DATABRICKS_GATEWAY_HOST to {value!r}. Leave it "
                f"unset so utils.get_gateway_host() tier 2 derives and probes the "
                f"deploying workspace's own gateway."
            )

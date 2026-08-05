"""Guard the Apps-overlay foot-gun.

`databricks apps deploy` with an overlay (e.g. `make deploy-workshop` swapping
in `app.yaml.workshop`) **replaces** `app.yaml` wholesale — it does not merge.
So any env var that exists in the base `app.yaml` but is missing from an overlay
silently disappears from the deployed container and falls back to whatever the
consuming code defaults to.

For the per-CLI install toggles that default is "install it" (each setup script
reads `os.environ.get("ENABLE_<CLI>", "true")`), so an omitted toggle is not a
no-op: it turns an intentionally-disabled agent back on. These tests assert every
tracked overlay declares the full set.
"""

from __future__ import annotations

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
    }
)


def _tracked_app_yamls() -> list[Path]:
    """All git-tracked app.yaml files. Untracked local variants are ignored."""
    out = subprocess.run(
        ["git", "ls-files", "app.yaml", "app.yaml.*"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO_ROOT / name for name in out]


def _env_names(path: Path) -> set[str]:
    parsed = yaml.safe_load(path.read_text())
    return {entry["name"] for entry in parsed.get("env", [])}


def test_finds_the_overlays():
    """Sanity check — a silent empty list would make the tests below vacuous."""
    names = {p.name for p in _tracked_app_yamls()}
    assert "app.yaml" in names
    assert len(names) >= 3, f"expected several overlays, found {sorted(names)}"


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
        f"know about it — add it to CLI_TOGGLES and to every app.yaml*."
    )

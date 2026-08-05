#!/usr/bin/env python
"""Pre-deploy reachability check for enterprise/proxy-mode CoDA.

Runs every URL configured via the enterprise env-var contract through a
single HTTP GET and reports PASS/FAIL. Intended to be run on the deployment
host (Azure DevOps self-hosted agent, customer CI runner, etc.) before
`make deploy` so connectivity problems surface before the app is sent to
the Databricks workspace.

Exit code:
  0 — all configured targets reachable (or nothing configured to check).
  1 — at least one target failed.

Usage:
  python scripts/enterprise_doctor.py
  make enterprise-doctor
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add repo root to sys.path so this script works when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import enterprise_config  # noqa: E402


def main() -> int:
    # Print the effective config first so the operator can correlate
    # PASS/FAIL with the values they set.
    for line in enterprise_config.startup_banner().splitlines():
        print(line)
    print()

    targets = enterprise_config.doctor_targets()
    if not targets:
        print("No enterprise targets configured — nothing to probe.")
        print("(Set UV_DEFAULT_INDEX, NPM_REGISTRY, GITHUB_RELEASE_MIRROR, etc.")
        print(" in your environment to enable reachability checks.)")
        return 0

    results = enterprise_config.doctor()
    width = max(len(name) for name, _, _, _ in results)
    any_failed = False
    for name, url, ok, detail in results:
        marker = "[ OK ]" if ok else "[FAIL]"
        print(f"{marker} {name:<{width}}  {url}  ({detail})")
        if not ok:
            any_failed = True

    print()
    if any_failed:
        print(
            "One or more targets are unreachable. The customer's network team "
            "needs to allow egress (or fix the mirror config) before deploy."
        )
        return 1
    print("All configured targets reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

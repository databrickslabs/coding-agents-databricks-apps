"""Regression tests for bundled-resource path resolution in the setup scripts.

Commit fec2152 moved every setup script into setup/ (R100 renames) but left the
resources they copy (agents/, .codex/, content_filter_proxy.py) at the repo root.
Scripts that located those resources via ``Path(__file__).parent`` silently broke:
the proxy never launched (OpenCode), Claude subagents weren't installed, and the
Codex model catalog wasn't copied. These tests pin each resolver to an existing
resource so a future move can't silently regress it again.

The setup scripts run heavy side effects at import (npm installs, curl), so we
extract and execute ONLY the resolver function from the source via AST — this
tests the real resolver code without triggering the script body.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_DIR = REPO_ROOT / "setup"


def _extract_resolver(script_path: Path, func_name: str):
    """Compile and exec just ``func_name`` from ``script_path`` (no body run)."""
    tree = ast.parse(script_path.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            ns = {"Path": Path, "__file__": str(script_path)}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(script_path), "exec"), ns)
            return ns[func_name]
    raise AssertionError(f"{func_name}() not found in {script_path}")


def test_claude_agents_resolver_points_at_existing_dir():
    """setup_claude.py must resolve agents/ to a real dir with the subagents."""
    resolve = _extract_resolver(SETUP_DIR / "setup_claude.py", "resolve_agents_src")
    agents = resolve()
    assert agents.is_dir(), f"setup_claude resolves agents/ to a missing dir: {agents}"
    names = {p.name for p in agents.glob("*.md")}
    expected = {"build-feature.md", "implementer.md", "prd-writer.md", "test-generator.md"}
    assert expected <= names, f"missing bundled subagents: {expected - names}"


def test_codex_catalog_resolver_points_at_existing_file():
    """setup_codex.py must resolve the model catalog to a real file."""
    resolve = _extract_resolver(SETUP_DIR / "setup_codex.py", "resolve_codex_catalog_src")
    catalog = resolve()
    assert catalog.is_file(), f"setup_codex resolves the model catalog to a missing file: {catalog}"
    assert catalog.name == "databricks-models.json"

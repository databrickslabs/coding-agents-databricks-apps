#!/usr/bin/env python3
"""Structured, secret-safe live smoke test for a deployed CoDA container.

Run *inside* the CoDA terminal. Intended to be driven through CoDA's authenticated
JSON terminal API by the verify-coda-live skill, not by scraping xterm's canvas.

Checks the product contract:
  1. Pi and OpenCode can infer through Unity AI Gateway with app-SP auth.
  2. Their advertised model catalogs match compatible READY serving endpoints.
  3. GitHub CLI authentication and safe repository reads work.
  4. Databricks CLI authentication and a safe /Shared write/read/delete round-trip work.

Outputs one JSON document. It never prints a bearer token, PAT, client secret, or
auth.json contents. Exit 0 = all required checks passed; exit 1 = one or more failed.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("HOME") or "/app/python/source_code")
APP_ROOT = Path(__file__).resolve().parent.parent
PI_CONFIG = HOME / ".pi" / "agent" / "models.json"
OPENCODE_CONFIG = HOME / ".config" / "opencode" / "opencode.json"
CLAUDE_CONFIG = HOME / ".claude" / "settings.json"
DATABRICKS_CFG = HOME / ".databrickscfg"
REPO = "databrickslabs/coding-agents-databricks-apps"

# These are the model families the current CoDA configs know how to drive.
# Custom serving endpoints are deliberately excluded from exact catalog parity:
# setup_pi.py only configures Anthropic wire format, and setup_opencode.py only
# registers the Databricks Claude/Gemini and GPT providers below.
PI_PREFIXES = ("databricks-claude-",)
OPENCODE_PREFIXES = ("databricks-claude-", "databricks-gemini-", "databricks-gpt-")

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _clean(text: str, limit: int = 20_000) -> str:
    """Remove terminal control codes and cap output. Never use for a token."""
    text = ANSI_RE.sub("", text or "")
    return text[-limit:]


def run(cmd: list[str], *, timeout: int = 60, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run a command, returning JSON-safe evidence rather than raising."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(HOME),
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": _clean(proc.stdout),
            "stderr": _clean(proc.stderr),
            "command": [cmd[0], *["<prompt>" if i == len(cmd) - 1 and len(cmd) > 2 else x for i, x in enumerate(cmd[1:], 1)]],
        }
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": f"{cmd[0]} not installed", "command": [cmd[0]]}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": _clean((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            "stderr": f"timed out after {timeout}s",
            "command": [cmd[0]],
        }
    except Exception as exc:  # noqa: BLE001 — verifier reports, never hides
        return {"ok": False, "returncode": None, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}", "command": [cmd[0]]}


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_json_output(result: dict[str, Any]) -> Any:
    if not result.get("ok"):
        return None
    try:
        return json.loads(result.get("stdout") or "")
    except Exception:
        return None


def ready_endpoints(raw: Any) -> list[str]:
    """READY endpoint names from CLI JSON (dict or list shape)."""
    if isinstance(raw, dict):
        endpoints = raw.get("endpoints") or []
    elif isinstance(raw, list):
        endpoints = raw
    else:
        endpoints = []
    names: list[str] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        state = endpoint.get("state") or {}
        if isinstance(state, dict):
            ready = str(state.get("ready") or "").upper()
        else:
            # Databricks CLI versions have emitted both {state: {ready: READY}}
            # and {state: READY}; accept the canonical values, not an assumed
            # one-version shape.
            ready = str(state).upper()
        name = endpoint.get("name")
        if name and ready == "READY":
            names.append(str(name))
    return sorted(set(names))


def profile_summary() -> dict[str, Any]:
    cp = configparser.ConfigParser()
    try:
        cp.read(DATABRICKS_CFG)
    except Exception:
        pass
    profiles = cp.sections()
    default = cp["DEFAULT"] if "DEFAULT" in cp else {}
    return {
        "file_exists": DATABRICKS_CFG.exists(),
        "profiles": profiles,
        "default_pat_present": bool((default.get("token") or "").strip()),
        "sp_profile_present": "omnigents-host" in profiles,
        "broker_url_present": bool(os.environ.get("CODA_SP_TOKEN_BROKER_URL", "").strip()),
        "sp_client_secret_visible": bool(os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()),
    }


def databricks_identity() -> tuple[dict[str, Any], Any]:
    result = run(["databricks", "current-user", "me", "--output", "json"], timeout=30)
    parsed = parse_json_output(result)
    evidence = {
        "command_ok": result["ok"],
        "returncode": result["returncode"],
        "stderr": result["stderr"],
        "identity": parsed,
    }
    return evidence, parsed


def serving_model_evidence() -> tuple[dict[str, Any], list[str]]:
    result = run(["databricks", "serving-endpoints", "list", "--output", "json"], timeout=45)
    parsed = parse_json_output(result)
    ready = ready_endpoints(parsed)
    return {
        "command_ok": result["ok"],
        "returncode": result["returncode"],
        "stderr": result["stderr"],
        "ready_endpoint_names": ready,
    }, ready


def pi_models(config: dict[str, Any]) -> list[str]:
    provider = ((config.get("providers") or {}).get("databricks-claude") or {})
    models = provider.get("models") or []
    return sorted({str(m.get("id")) for m in models if isinstance(m, dict) and m.get("id")})


def claude_active_models(config: dict[str, Any]) -> list[str]:
    env = config.get("env") or {}
    names = []
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        value = str(env.get(key) or "")
        # Claude's 1M routing suffix is metadata, not a serving endpoint id.
        value = re.sub(r"\[1m\]$", "", value)
        if value:
            names.append(value)
    return sorted(set(names))


def opencode_models(config: dict[str, Any]) -> list[str]:
    providers = config.get("provider") or {}
    names: set[str] = set()
    for provider_name in ("databricks", "databricks-openai"):
        models = ((providers.get(provider_name) or {}).get("models") or {})
        if isinstance(models, dict):
            names.update(str(x) for x in models)
    return sorted(names)


def opencode_displayed_models() -> dict[str, Any]:
    """What `opencode models` actually displays, not only what its JSON says."""
    results = {
        "databricks": run(["opencode", "models", "databricks"], timeout=45),
        "databricks-openai": run(["opencode", "models", "databricks-openai"], timeout=45),
    }
    displayed: set[str] = set()
    # Current CLI output is one `provider/model-id` per line. Be tolerant of
    # surrounding decoration while still requiring the configured provider id.
    pattern = re.compile(r"(?:^|\s)(databricks(?:-openai)?)/([^\s]+)")
    for result in results.values():
        for match in pattern.finditer(result.get("stdout") or ""):
            displayed.add(match.group(2).strip())
    return {
        "command_ok": all(r["ok"] for r in results.values()),
        "models": sorted(displayed),
        "errors": [r["stderr"] for r in results.values() if not r["ok"]],
    }


def catalog_comparison(ready: list[str]) -> dict[str, Any]:
    pi_cfg = load_json(PI_CONFIG)
    oc_cfg = load_json(OPENCODE_CONFIG)
    claude_cfg = load_json(CLAUDE_CONFIG)
    pi_configured = pi_models(pi_cfg)
    oc_configured = opencode_models(oc_cfg)
    oc_display = opencode_displayed_models()
    claude_configured = claude_active_models(claude_cfg)
    pi_expected = sorted(n for n in ready if n.startswith(PI_PREFIXES))
    oc_expected = sorted(n for n in ready if n.startswith(OPENCODE_PREFIXES))
    claude_expected = sorted(n for n in ready if n.startswith(PI_PREFIXES))

    pi_provider = ((pi_cfg.get("providers") or {}).get("databricks-claude") or {})
    oc_providers = oc_cfg.get("provider") or {}
    pi_base = str(pi_provider.get("baseUrl") or "")
    oc_db_base = str((((oc_providers.get("databricks") or {}).get("options") or {}).get("baseURL") or ""))
    oc_openai_base = str((((oc_providers.get("databricks-openai") or {}).get("options") or {}).get("baseURL") or ""))

    def compare(configured: list[str], expected: list[str]) -> dict[str, Any]:
        extra = sorted(set(configured) - set(expected))
        missing = sorted(set(expected) - set(configured))
        return {
            "configured": configured,
            "expected_ready_compatible": expected,
            "extra_not_ready": extra,
            "missing_ready": missing,
            "exact_match": not extra and not missing,
        }

    return {
        "claude": {
            **compare(claude_configured, claude_expected),
            "config_exists": CLAUDE_CONFIG.exists(),
            "api_key_helper_present": bool(claude_cfg.get("apiKeyHelper")),
        },
        "pi": {
            **compare(pi_configured, pi_expected),
            "config_exists": PI_CONFIG.exists(),
            "base_url": pi_base,
            "uses_gateway_route": "ai-gateway" in pi_base or pi_base.startswith("http://127.0.0.1:"),
            "api_key_is_helper_command": str(pi_provider.get("apiKey") or "").startswith("!"),
        },
        "opencode": {
            **compare(oc_configured, oc_expected),
            "config_exists": OPENCODE_CONFIG.exists(),
            "databricks_base_url": oc_db_base,
            "openai_base_url": oc_openai_base,
            "uses_proxy_or_gateway": oc_db_base.startswith("http://127.0.0.1:") and (not oc_openai_base or "ai-gateway" in oc_openai_base),
            "cli_model_command_ok": oc_display["command_ok"],
            "cli_displayed": oc_display["models"],
            "cli_display_extra": sorted(set(oc_display["models"]) - set(oc_expected)),
            "cli_display_missing": sorted(set(oc_expected) - set(oc_display["models"])),
            "cli_display_exact_match": (
                oc_display["command_ok"]
                and set(oc_display["models"]) == set(oc_expected)
            ),
            "cli_errors": oc_display["errors"],
        },
    }


def token_identity_from_pi_helper() -> dict[str, Any]:
    """Resolve Pi's helper token in memory and call SCIM /Me; never print token."""
    cfg = load_json(PI_CONFIG)
    provider = ((cfg.get("providers") or {}).get("databricks-claude") or {})
    command = str(provider.get("apiKey") or "")
    if not command.startswith("!"):
        return {"ok": False, "reason": "Pi apiKey is not a helper command"}
    try:
        token_proc = subprocess.run(
            shlex.split(command[1:]), capture_output=True, text=True, timeout=20, cwd=str(HOME)
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"helper failed: {type(exc).__name__}: {exc}"}
    token = (token_proc.stdout or "").strip()
    if token_proc.returncode != 0 or not token:
        return {"ok": False, "reason": _clean(token_proc.stderr) or "helper returned no token"}

    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    if not host:
        # CoDA deliberately strips DATABRICKS_HOST from terminal env. The
        # broker-owned profile still carries the workspace host; use that for
        # this verifier's safe /Me request rather than treating correct secret
        # stripping as an auth failure.
        try:
            cfg = configparser.ConfigParser(interpolation=None)
            cfg.read(DATABRICKS_CFG)
            host = (cfg.get("omnigents-host", "host", fallback="") or "").rstrip("/")
        except Exception:
            host = ""
    if not host:
        return {"ok": False, "reason": "DATABRICKS_HOST absent and profile host unavailable"}
    req = urllib.request.Request(
        f"{host}/api/2.0/preview/scim/v2/Me",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            body = json.loads(response.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": f"SCIM /Me failed: {type(exc).__name__}: {exc}"}
    finally:
        token = ""  # do not retain it longer than needed

    username = str(body.get("userName") or "")
    app_id = str(body.get("applicationId") or "")
    classified = "user" if "@" in username else "service_principal"
    return {
        "ok": True,
        "classified_as": classified,
        "userName": username,
        "displayName": body.get("displayName"),
        "applicationId": app_id or None,
        "id": body.get("id"),
    }


def inference_checks(catalogs: dict[str, Any], *, skip: bool) -> dict[str, Any]:
    if skip:
        return {"skipped": True, "reason": "--skip-inference"}

    result: dict[str, Any] = {}
    claude = run(
        ["claude", "--print", "--no-session", "--model", "sonnet", "Reply with exactly CODA_CLAUDE_OK"],
        timeout=180,
    )
    result["claude"] = {
        "ok": claude["ok"] and "CODA_CLAUDE_OK" in claude["stdout"],
        "marker_seen": "CODA_CLAUDE_OK" in claude["stdout"],
        "returncode": claude["returncode"],
        "stdout": claude["stdout"],
        "stderr": claude["stderr"],
    }

    pi_models_list = catalogs["pi"]["configured"]
    if pi_models_list:
        model = pi_models_list[0]
        pi = run(
            [
                "pi", "--print", "--no-session", "--no-tools", "--no-context-files",
                "--no-extensions", "--no-skills", "--model",
                f"databricks-claude/{model}",
                "Reply with exactly CODA_PI_OK",
            ],
            timeout=150,
        )
        result["pi"] = {
            "ok": pi["ok"] and "CODA_PI_OK" in pi["stdout"],
            "model": model,
            "marker_seen": "CODA_PI_OK" in pi["stdout"],
            "returncode": pi["returncode"],
            "stdout": pi["stdout"],
            "stderr": pi["stderr"],
        }
    else:
        result["pi"] = {"ok": False, "reason": "no configured Pi model"}

    oc_models_list = catalogs["opencode"]["configured"]
    claude_like = [x for x in oc_models_list if x.startswith(("databricks-claude-", "databricks-gemini-"))]
    if claude_like:
        model = claude_like[0]
        oc = run(
            ["opencode", "run", "--pure", "--format", "json", "--model", f"databricks/{model}", "Reply with exactly CODA_OPENCODE_OK"],
            timeout=180,
        )
        result["opencode"] = {
            "ok": oc["ok"] and "CODA_OPENCODE_OK" in oc["stdout"],
            "model": model,
            "marker_seen": "CODA_OPENCODE_OK" in oc["stdout"],
            "returncode": oc["returncode"],
            "stdout": oc["stdout"],
            "stderr": oc["stderr"],
        }
    else:
        result["opencode"] = {"ok": False, "reason": "no configured OpenCode databricks model"}
    return result


def github_checks() -> dict[str, Any]:
    status = run(["gh", "auth", "status", "--hostname", "github.com"], timeout=20)
    user = run(["gh", "api", "user", "--jq", ".login"], timeout=20)
    repo = run(["gh", "repo", "view", REPO, "--json", "nameWithOwner,viewerPermission,isPrivate"], timeout=30)
    ls_remote = run(["git", "ls-remote", "--heads", f"https://github.com/{REPO}.git", "main"], timeout=30)
    return {
        "ok": all(x["ok"] for x in (status, user, repo, ls_remote)),
        "auth_status": {"ok": status["ok"], "stderr": status["stderr"]},
        "login": user["stdout"].strip() if user["ok"] else None,
        "repo": parse_json_output(repo),
        "git_ls_remote_main": ls_remote["ok"],
        "errors": [x["stderr"] for x in (status, user, repo, ls_remote) if not x["ok"]],
    }


def workspace_round_trip(*, skip: bool) -> dict[str, Any]:
    if skip:
        return {"skipped": True, "reason": "--skip-workspace-write"}
    unique = f"/Shared/coda-live-smoke-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    with tempfile.TemporaryDirectory(prefix="coda-live-") as temp_dir:
        src = Path(temp_dir) / "probe.txt"
        dst = Path(temp_dir) / "exported.txt"
        content = f"coda smoke {uuid.uuid4()}\n"
        src.write_text(content)
        steps: dict[str, Any] = {}
        try:
            steps["mkdirs"] = run(["databricks", "workspace", "mkdirs", unique], timeout=30)
            steps["import"] = run(
                ["databricks", "workspace", "import", f"{unique}/probe.txt", "--file", str(src), "--format", "RAW", "--overwrite"],
                timeout=30,
            )
            steps["get_status"] = run(
                ["databricks", "workspace", "get-status", f"{unique}/probe.txt", "--output", "json"],
                timeout=30,
            )
            steps["export"] = run(
                ["databricks", "workspace", "export", f"{unique}/probe.txt", "--format", "AUTO", "--file", str(dst)],
                timeout=30,
            )
            round_trip_equal = dst.exists() and dst.read_text() == content
        finally:
            steps["cleanup"] = run(
                ["databricks", "workspace", "delete", unique, "--recursive"], timeout=30
            )
        required = ("mkdirs", "import", "get_status", "export", "cleanup")
        return {
            "ok": all(steps[k]["ok"] for k in required) and round_trip_equal,
            "workspace_path": unique,
            "round_trip_equal": round_trip_equal,
            "steps": {
                k: {"ok": v["ok"], "returncode": v["returncode"], "stderr": v["stderr"]}
                for k, v in steps.items()
            },
        }


def required_failures(report: dict[str, Any], expected_auth: str) -> list[str]:
    failures: list[str] = []
    profiles = report["auth_material"]
    if expected_auth == "sp":
        if not profiles["broker_url_present"]:
            failures.append("SP broker URL absent")
        if profiles["default_pat_present"]:
            failures.append("PAT present while testing SP-only baseline")
        if report["model_token_identity"].get("classified_as") != "service_principal":
            failures.append("Pi helper token did not identify as a service principal")
    elif expected_auth == "pat":
        if not profiles["default_pat_present"]:
            failures.append("PAT absent while testing PAT baseline")
        if report["model_token_identity"].get("classified_as") != "user":
            failures.append("Pi helper token did not identify as a user")

    if not report["databricks_cli"]["command_ok"]:
        failures.append("databricks current-user me failed")
    for agent in ("claude", "pi", "opencode"):
        catalog = report["model_catalogs"][agent]
        if not catalog["exact_match"]:
            failures.append(f"{agent} model catalog does not exactly match READY compatible endpoints")
        if not catalog.get("config_exists"):
            failures.append(f"{agent} config missing")
    if not report["model_catalogs"]["opencode"].get("cli_display_exact_match"):
        failures.append("OpenCode displayed model list does not exactly match READY compatible endpoints")
    inference = report["inference"]
    if not inference.get("skipped"):
        for agent in ("claude", "pi", "opencode"):
            if not inference.get(agent, {}).get("ok"):
                failures.append(f"{agent} inference smoke failed")
    if not report["github"]["ok"]:
        failures.append("GitHub CLI/repository read smoke failed")
    workspace = report["workspace_round_trip"]
    if not workspace.get("skipped") and not workspace.get("ok"):
        failures.append("Databricks workspace write/read/delete round-trip failed")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-model-auth", choices=("sp", "pat"), default="sp")
    parser.add_argument("--skip-inference", action="store_true", help="Do not make the two model calls")
    parser.add_argument("--skip-workspace-write", action="store_true", help="Do not create/delete the /Shared probe")
    args = parser.parse_args()

    db_identity, _ = databricks_identity()
    models_evidence, ready = serving_model_evidence()
    catalogs = catalog_comparison(ready)
    report: dict[str, Any] = {
        "contract": {
            "expected_model_auth": args.expect_model_auth,
            "target": "deployed CoDA container",
            "repo_commit": run(["git", "-C", str(APP_ROOT), "rev-parse", "HEAD"], timeout=10)["stdout"].strip(),
        },
        "auth_material": profile_summary(),
        "model_token_identity": token_identity_from_pi_helper(),
        "databricks_cli": db_identity,
        "workspace_models": models_evidence,
        "model_catalogs": catalogs,
        "inference": inference_checks(catalogs, skip=args.skip_inference),
        "github": github_checks(),
        "workspace_round_trip": workspace_round_trip(skip=args.skip_workspace_write),
    }
    failures = required_failures(report, args.expect_model_auth)
    report["failures"] = failures
    report["ok"] = not failures
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

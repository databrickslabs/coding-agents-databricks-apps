"""Shared writer for the Databricks bearer-token helper script.

Both Claude Code (via ``apiKeyHelper`` in settings.json) and Pi (via a
``!command`` apiKey in models.json) resolve their model-auth token by running
this helper *per request / per TTL*. Centralising the writer keeps a single
source of truth for the token-resolution logic and its interpreter wiring, so
the two agents can never drift apart.

The helper prints exactly one line to stdout: the bearer token. Resolution
order (see the emitted script's docstring for the full rationale):
  1. SP OAuth token fetched from the loopback broker (host path).
  2. Legacy ``omnigents-host`` M2M profile, for upgrades from older containers.
  3. The PAT from ``$DATABRICKS_TOKEN`` or ``~/.databrickscfg`` [DEFAULT].
All are resolved fresh on each invocation, so a long-running agent survives
PAT rotation / SP-OAuth expiry without a restart.
"""

import configparser
import json
import os
import sys
from pathlib import Path

from sp_token_broker import fetch_sp_token

# Legacy SP OAuth profile name. New installs persist only a secret-free host
# pointer under this name and fetch bearers from the loopback broker.
SP_PROFILE = "omnigents-host"


def resolve_sp_oauth_token() -> str | None:
    """Resolve a fresh token from the Omnigent host M2M profile."""
    broker_url = os.environ.get("CODA_SP_TOKEN_BROKER_URL", "").strip()
    if broker_url:
        token = fetch_sp_token(broker_url)
        if token:
            return token
    # Do not let a missing legacy profile trigger the SDK's ambient auth
    # discovery/network path. On local setup tests (and on a cold PAT-only
    # container) there is no profile; return immediately so setup scripts can
    # use DATABRICKS_TOKEN instead of hanging for an unavailable SP login.
    cfg_path = Path(os.path.expanduser("~/.databrickscfg"))
    try:
        config = configparser.ConfigParser()
        config.read(cfg_path)
        if SP_PROFILE not in config:
            return None
    except Exception:
        return None
    try:
        from databricks.sdk.core import Config

        headers = Config(profile=SP_PROFILE).authenticate()
        auth = (headers or {}).get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else auth.strip()
        if token:
            return token
    except Exception:
        pass
    return None


def resolve_databricks_token() -> str | None:
    """Resolve a fresh SP OAuth token, falling back to the current PAT."""
    token = resolve_sp_oauth_token()
    if token:
        return token

    token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    if token:
        return token
    try:
        config = configparser.ConfigParser()
        config.read(os.path.expanduser("~/.databrickscfg"))
        return config.get("DEFAULT", "token", fallback="").strip() or None
    except Exception:
        return None


_HELPER_SRC = '''#!/usr/bin/env python3
"""Print a Databricks bearer token for Claude Code / Pi token resolution.

stdout MUST be the token only -- both callers use it verbatim.
"""
import configparser
import os
import shutil
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

SP_PROFILE = "omnigents-host"
_MAX_TOKEN_BYTES = 16 * 1024
# Set on the uv re-run so the child (which has the SDK) doesn't recurse.
_REEXEC_GUARD = "OMNIGENTS_APIKEY_HELPER_REEXEC"


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def urlopen(url, timeout):
    return _NO_REDIRECT_OPENER.open(url, timeout=timeout)


def _broker_token():
    url = os.environ.get("CODA_SP_TOKEN_BROKER_URL", "").strip()
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
            and parsed.username is None and parsed.password is None
            and parsed.port is not None and not parsed.query and not parsed.fragment
            and len(parsed.path.split("/")) == 3
            and parsed.path.startswith("/token/")
            and len(parsed.path.rsplit("/", 1)[1]) >= 32
        )
    except ValueError:
        valid = False
    if not valid:
        return None
    try:
        with urlopen(url, timeout=5) as response:
            content_type = (response.headers.get("Content-Type", "") or "").split(";", 1)[0]
            body = response.read(_MAX_TOKEN_BYTES + 1)
        if content_type.strip().lower() != "text/plain" or len(body) > _MAX_TOKEN_BYTES:
            return None
        token = body.decode("utf-8").strip()
        return token if token and "\\r" not in token and "\\n" not in token else None
    except Exception:
        return None


def _sp_oauth_token():
    token = _broker_token()
    if token:
        return token

    # Upgrade fallback: mint via an older persisted M2M profile. New installs
    # never write client_id/client_secret to terminal-visible HOME.
    # The M2M (client_id/secret) profile the
    # Omnigent host writes is NOT usable via `databricks auth token` (that CLI
    # verb is U2M-only and refuses M2M); Config.authenticate() does the
    # client-credentials flow. Verified accepted by /anthropic (C-O1, HTTP 200).
    # Absent profile / non-host instance -> returns None, caller falls back.
    try:
        from databricks.sdk.core import Config
    except ImportError:
        # The helper is normally registered to run under the app's venv
        # interpreter (which has databricks-sdk), so this branch shouldn't
        # trigger. If it is ever invoked with a bare python3 that lacks the
        # SDK, re-exec once under an interpreter that has it: prefer the
        # recorded venv python (CODA_VENV_PYTHON), else fall back to
        # `uv run --with databricks-sdk python`.
        if os.environ.get(_REEXEC_GUARD) == "1":
            return None
        env = dict(os.environ, **{_REEXEC_GUARD: "1"})
        try:
            venv_python = os.environ.get("CODA_VENV_PYTHON")
            if venv_python and os.path.exists(venv_python):
                cmd = [venv_python, os.path.abspath(__file__)]
            else:
                uv = shutil.which("uv")
                if not uv:
                    return None
                cmd = [uv, "run", "--with", "databricks-sdk", "python",
                       os.path.abspath(__file__)]
            out = subprocess.run(
                cmd, env=env, capture_output=True, text=True, check=False,
            )
        except Exception:
            return None
        return (out.stdout or "").strip() or None
    except Exception:
        return None
    try:
        headers = Config(profile=SP_PROFILE).authenticate()
    except Exception:
        return None
    auth = (headers or {}).get("Authorization", "")
    # Strip the "Bearer " prefix so stdout carries the raw token only.
    return auth[7:].strip() if auth.startswith("Bearer ") else (auth.strip() or None)


def _pat_token():
    tok = os.environ.get("DATABRICKS_TOKEN", "").strip()
    if tok:
        return tok
    cfg_path = os.path.expanduser("~/.databrickscfg")
    try:
        cp = configparser.ConfigParser()
        cp.read(cfg_path)
        return (cp["DEFAULT"].get("token") or "").strip() or None
    except Exception:
        return None


def main():
    token = _sp_oauth_token() or _pat_token()
    if not token:
        print("no token source (no SP token broker, no PAT)", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(token)


if __name__ == "__main__":
    main()
'''


def write_token_helper(target_dir) -> Path:
    """Write the executable token helper into ``target_dir``; return its path.

    Idempotent: overwrites any prior copy so a version bump propagates. The
    helper emits only the token on stdout (all diagnostics go to stderr).
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    helper_path = target_dir / "anthropic-token-helper.py"
    helper_path.write_text(_HELPER_SRC)
    helper_path.chmod(0o700)
    return helper_path


def helper_command(helper_path) -> str:
    """The `!command` string Pi uses to resolve its apiKey via the helper.

    Runs the helper under the app's venv interpreter (has databricks-sdk) so it
    never has to re-exec; falls back to the current interpreter. Pi resolves the
    `!`-prefixed value as a shell command fresh per request.
    """
    py = os.environ.get("CODA_VENV_PYTHON") or sys.executable or "python3"
    return f"!{py} {helper_path}"


def write_databricks_token_wrapper(target_dir, real_cli: str) -> Path:
    """Write a narrow CLI shim for Omnigent's profile-based auth command."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = target_dir / "databricks"
    source = f'''#!{sys.executable}
import configparser
import datetime
import json
import os
import sys
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

REAL_CLI_FALLBACK = {json.dumps(real_cli)}
PROFILE = {json.dumps(SP_PROFILE)}


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def urlopen(url, timeout):
    return _NO_REDIRECT_OPENER.open(url, timeout=timeout)


def _real_cli():
    """Resolve the CLI at call time so a boot race can't pin a stale binary.

    The wrapper is written as soon as the SP broker is up, which can be BEFORE
    install_databricks_cli.sh has finished putting the current CLI in
    ~/.local/bin. Baking whatever existed at that moment (the image's much
    older /usr/local/bin build) silently downgraded every CLI call for the life
    of the container. That is not only missing flags: the old CLI ignores
    `bundle.engine: direct`, so a Databricks Asset Bundle deploy runs through
    Terraform instead \u2014 which drops an app's compute_size and needs egress to
    releases.hashicorp.com.
    """
    preferred = os.path.join(
        os.environ.get("HOME", "/app/python/source_code"), ".local", "bin", "databricks"
    )
    if os.path.isfile(preferred) and os.access(preferred, os.X_OK):
        return preferred
    return REAL_CLI_FALLBACK


# `--profile` and its documented shorthand `-p` are GLOBAL flags on the
# Databricks CLI, so both forms must be recognised — an agent (or the repo's
# Makefile) typing `-p omnigents-host` hit the un-brokered path otherwise.
PROFILE_FLAGS = ("--profile", "-p")


def _profile(args):
    for index, arg in enumerate(args):
        if arg in PROFILE_FLAGS and index + 1 < len(args):
            return args[index + 1]
        for flag in PROFILE_FLAGS:
            if arg.startswith(flag + "="):
                return arg.split("=", 1)[1]
    return None


def _strip_profile_flags(args):
    """Drop `--profile/-p <PROFILE>` before handing argv to the real CLI.

    Load-bearing. The Go CLI reads `auth_type = databricks-cli` out of the
    omnigents-host profile and then looks for ITS OWN OAuth token cache —
    `databricks_cli_path` is a databricks-sdk (Python) Config field the Go CLI
    ignores. So an explicit `--profile omnigents-host` made the real CLI fail
    with "cache: no cached credentials; run `databricks auth login` to sign in"
    even though this shim had just injected a valid brokered token into the
    environment: the named profile shadows DATABRICKS_TOKEN/DATABRICKS_HOST.
    Stripping the selector leaves the env creds as the resolution path, which is
    the same identity the profile names.
    """
    out = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in PROFILE_FLAGS:
            skip = True
            continue
        if any(arg.startswith(flag + "=") for flag in PROFILE_FLAGS):
            continue
        out.append(arg)
    return out


def _broker_token():
    url = os.environ.get("CODA_SP_TOKEN_BROKER_URL", "").strip()
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
            and parsed.username is None and parsed.password is None
            and parsed.port is not None and not parsed.query and not parsed.fragment
            and len(parsed.path.split("/")) == 3
            and parsed.path.startswith("/token/")
            and len(parsed.path.rsplit("/", 1)[1]) >= 32
        )
    except ValueError:
        valid = False
    if not valid:
        return None
    try:
        with urlopen(url, timeout=5) as response:
            content_type = (response.headers.get("Content-Type", "") or "").split(";", 1)[0]
            body = response.read(16 * 1024 + 1)
        if content_type.strip().lower() != "text/plain" or len(body) > 16 * 1024:
            return None
        token = body.decode("utf-8").strip()
        return token if token and "\\r" not in token and "\\n" not in token else None
    except Exception:
        return None


def _config_path():
    configured = os.environ.get("DATABRICKS_CONFIG_FILE", "").strip()
    path = configured or os.path.join(
        os.environ.get("HOME", "/app/python/source_code"), ".databrickscfg"
    )
    return os.path.abspath(path)

def _read_config():
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(_config_path())
    except Exception:
        pass
    return cfg


def _profile_host():
    try:
        cfg = _read_config()
        if not cfg.has_section(PROFILE):
            return ""
        return (cfg.get(PROFILE, "host", fallback="") or "").strip()
    except Exception:
        return ""


def _env_has_credentials():
    env = os.environ
    if (env.get("DATABRICKS_AUTH_TYPE") or "").strip():
        return True
    if (env.get("DATABRICKS_TOKEN") or "").strip():
        return True
    if (env.get("DATABRICKS_CLIENT_ID") or "").strip() and (
        env.get("DATABRICKS_CLIENT_SECRET") or ""
    ).strip():
        return True
    if (env.get("DATABRICKS_PASSWORD") or "").strip():
        return True
    return False


def _default_profile_has_credentials():
    """True when [DEFAULT] in ~/.databrickscfg can authenticate on its own.

    That is the PAT-bootstrap path (a human pasted a token, `pat_rotator`
    refreshes it). When it exists we must NOT silently switch identity to the
    app service principal.
    """
    try:
        defaults = _read_config().defaults()
    except Exception:
        return False
    if (defaults.get("auth_type") or "").strip():
        return True
    if (defaults.get("token") or "").strip():
        return True
    if (defaults.get("client_id") or "").strip() and (
        defaults.get("client_secret") or ""
    ).strip():
        return True
    if (defaults.get("password") or "").strip():
        return True
    return False


def _host_arg(args):
    for index, arg in enumerate(args):
        if arg == "--host" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--host="):
            return arg.split("=", 1)[1]
    return None


args = sys.argv[1:]
explicit_profile = _profile(args)
profile = explicit_profile or os.environ.get("DATABRICKS_CONFIG_PROFILE")

# `databricks auth login` cannot work in this container: the OAuth redirect
# lands on a loopback port no outside browser can reach, and on an
# ENABLE_SP_APIKEYHELPER-only instance there is no PAT bootstrap either. Agents
# that hit the un-brokered error above followed its advice, ran `auth login`,
# and stalled on an interactive prompt — burning a session on "deployments are
# impossible here". Short-circuit it into a no-op WHEN (and only when) the
# broker really can mint a token for this workspace, so `auth login && bundle
# deploy` chains succeed.
#
# Deliberately NARROW so Omnigent's own login path is untouched:
#   * only a bare `--host` equal to the brokered workspace matches — omnigent's
#     _run_databricks_browser_login passes `--host <ws>/?o=<org>` when it has an
#     org selector, which does NOT match and falls through to the real CLI;
#   * `omnigent login` only reaches that browser flow interactively
#     (`sys.stdin.isatty()`), and it verifies the minted token afterwards, so a
#     no-op surfaces as its own actionable error, never a silent success;
#   * CODA_BROKER_ALLOW_AUTH_LOGIN=1 disables the short-circuit entirely for
#     anyone who genuinely wants to drive the interactive OAuth flow.
_allow_login = (os.environ.get("CODA_BROKER_ALLOW_AUTH_LOGIN", "").strip().lower()
                in ("1", "true", "yes"))
if args[:2] == ["auth", "login"] and profile in (None, "", PROFILE) and not _allow_login:
    requested_host = (_host_arg(args) or "").rstrip("/")
    broker_host = _profile_host().rstrip("/")
    if broker_host and requested_host in ("", broker_host):
        if _broker_token():
            sys.stderr.write(
                """databricks auth login: skipped — this container already has
brokered Databricks OAuth (CoDA app service principal). No interactive
login and no PAT are needed.

  host    : {{broker_host}}
  profile : {{PROFILE}}  (also used when no profile is selected)

Just run the command you wanted, e.g.
  databricks current-user me
  databricks bundle deploy -t dev
A fresh token is minted per invocation, so nothing expires mid-session.
Set CODA_BROKER_ALLOW_AUTH_LOGIN=1 to force the real interactive flow.
""".format(broker_host=broker_host, PROFILE=PROFILE)
            )
            raise SystemExit(0)

if args[:2] == ["auth", "token"] and explicit_profile == PROFILE:
    token = _broker_token()
    if not token:
        real_cli = _real_cli()
        os.execv(real_cli, [real_cli, *args])
    # Emit the FULL OAuth token shape the databricks-sdk CLI token source
    # (DatabricksCliTokenSource) requires: access_token + token_type + expiry.
    # ALWAYS emit JSON — NOT gated on `--output json`. The SDK builds the token
    # command as `databricks auth token --profile <p>` WITHOUT `--output json`
    # (credentials_provider._build_core_cli_command) yet still json.loads()s the
    # output, because the real CLI defaults `auth token` to JSON. A shim that
    # printed a raw token on the no-flag path made the SDK fail with "cannot
    # unmarshal CLI result: Expecting value: line 1 column 1", so
    # Config(profile=...).authenticate() — used by omnigent's
    # resolve_databricks_workspace for the model-catalog fetch — failed and pi
    # fell back to a single-model picker. Match the real CLI: default to JSON.
    #
    # The broker returns only the raw token (no expiry metadata) and always
    # mints a FRESH token per call. Set a short, conservative expiry (now + 5
    # min, well inside the ~1h SP-OAuth TTL) so the SDK re-invokes this shim —
    # re-hitting the broker for a fresh token — rather than caching one whose
    # real lifetime we can't prove. Format matches CliTokenSource._parse_expiry
    # ("%Y-%m-%dT%H:%M:%S", trailing Z ok).
    expiry = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=5)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {{
        "access_token": token,
        "token_type": "Bearer",
        "expiry": expiry,
    }}
    # A bare `auth token` (no --output) also defaults to JSON on the real CLI,
    # so emitting JSON unconditionally is faithful. `--output text` is not used
    # by the SDK, so we don't special-case it.
    print(json.dumps(payload))
    raise SystemExit(0)

# Direct terminal commands (e.g. `databricks current-user me`) do not invoke
# the SDK's `auth token` subcommand, so they would otherwise reach the real CLI
# with the secret-free omnigents-host profile and fail looking for an OAuth
# cache. For that one profile, mint a broker token and hand it to the real CLI
# through process-local env vars. The token never enters the shell's exported
# environment or a file, and other profiles/commands are delegated untouched.
#
# The same injection also has to cover the case where NO profile is selected at
# all. Omnigent's native-harness terminals deliberately unset
# DATABRICKS_CONFIG_PROFILE (`_claude_terminal_env_unset`), so an agent running
# inside a runner sees neither the env selector nor a [DEFAULT] PAT and every
# CLI call died with "cannot configure default credentials" — which is what made
# bundle deploys look impossible from Omnigent. Fall back to the broker only
# when there is genuinely nothing else to authenticate with, so a pasted-PAT
# container keeps deploying as the human, not as the app service principal.
use_broker = profile == PROFILE or (
    not profile
    and bool(_profile_host())
    and not _env_has_credentials()
    and not _default_profile_has_credentials()
)
if use_broker:
    token = _broker_token()
    if token:
        env = dict(os.environ)
        env["DATABRICKS_TOKEN"] = token
        host = _profile_host()
        if host:
            env["DATABRICKS_HOST"] = host
        # The wrapper is also safe when invoked directly, rather than only via
        # _run_host_once's already-scrubbed environment. Keep the profile and
        # all ambient app-auth selectors from shadowing the injected token.
        for key in (
            "DATABRICKS_CONFIG_PROFILE",
            "DATABRICKS_CLIENT_ID",
            "DATABRICKS_CLIENT_SECRET",
            "DATABRICKS_WORKSPACE_ID",
            "DATABRICKS_APP_NAME",
            "DATABRICKS_APP_URL",
            "DATABRICKS_AUTH_TYPE",
        ):
            env.pop(key, None)
        real_cli = _real_cli()
        forwarded = _strip_profile_flags(args) if explicit_profile == PROFILE else args
        os.execve(real_cli, [real_cli, *forwarded], env)

_resolved_cli = _real_cli()
os.execv(_resolved_cli, [_resolved_cli, *args])
'''
    wrapper_path.write_text(source)
    wrapper_path.chmod(0o700)
    return wrapper_path

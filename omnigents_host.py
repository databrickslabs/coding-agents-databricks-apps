"""Register this CoDA instance as an Omnigents agent host.

When ``OMNIGENTS_SERVER_URL`` is set, CoDA launches ``omnigents host`` as a
supervised background process so the Omnigents server can run coding-agent
sessions inside this container — the same filesystem, AI-Gateway creds, and
Unity Catalog scope the browser terminals already use.

Two credentials, two jobs (see GOAL.md §3):

* **Host tunnel** — authenticates ``omnigents host`` *to the Omnigents server*
  over its outbound WSS tunnel. The Databricks Apps ingress proxy in front of
  the server **rejects PATs (302 → OIDC login) and accepts OAuth / service-
  principal tokens**, so the host MUST present an OAuth token minted from the
  app SP's ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET``. CoDA strips
  those creds from the environment early in startup, so we capture them in the
  app process and expose only short-lived tokens through a loopback broker.
* **Harness LLM** — the runner the host spawns authenticates to AI Gateway via
  CoDA's already-injected ``ANTHROPIC_*`` env, forwarded host→runner by
  Omnigents' ``HARNESS_CREDENTIAL_ENV_VARS``. No new LLM credential is minted.

Off by default: with ``OMNIGENTS_SERVER_URL`` unset, nothing here runs and
CoDA behaves exactly as before.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import logging
import os
import shutil
import select
import shlex
import subprocess
import sys
import tempfile
import threading
import time

import yaml

from sp_token_broker import fetch_sp_token
from token_helper import write_databricks_token_wrapper
from utils import config_profile_env, ensure_https

logger = logging.getLogger(__name__)

# Profile name written to ~/.databrickscfg for the host's OAuth (M2M) auth.
# Kept distinct from DEFAULT (CoDA's PAT) so the two credentials never collide.
_HOST_PROFILE = "omnigents-host"

# Supervisor restart policy.
_RESTART_BACKOFF_SECONDS = 10
_MAX_BACKOFF_SECONDS = 120

# Observable state for /api/omnigent-host/status. Updated as startup progresses
# so the integration can be diagnosed without app log access.
_status: dict[str, object] = {
    "configured": False,
    "running": False,
    "installed": False,
    "host_launched": False,
    "server_url": None,
    "pid": None,
    "stage": "idle",
    "last_error": None,
    "log_tail": [],
}

_lock = threading.RLock()
_stop_event: threading.Event | None = None
_thread: threading.Thread | None = None
_proc: subprocess.Popen[str] | None = None
_sp_creds: dict[str, str] | None = None
_log_tail: list[str] = []
_LOG_TAIL_LIMIT = 80
_runner_tailer_started = False


def _stable_host_identity() -> tuple[str, str] | None:
    """Return a deterministic Omnigents host identity for this Databricks App."""
    app_client_id = (_sp_creds or {}).get("client_id") or os.environ.get("DATABRICKS_CLIENT_ID", "")
    if not app_client_id:
        return None
    app_name = os.environ.get("DATABRICKS_APP_NAME", "").strip() or "coda"
    digest = hashlib.sha256(f"coda-omnigents-host:{app_client_id}".encode()).hexdigest()[:32]
    return f"host_{digest}", app_name


def get_status() -> dict[str, object]:
    """Return a copy of the current host-integration state."""
    with _lock:
        snapshot = dict(_status)
        snapshot["log_tail"] = list(_log_tail)
        return snapshot


def _set(**kw: object) -> None:
    with _lock:
        _status.update(kw)


def _append_log(line: str) -> None:
    line = line.rstrip()
    if not line:
        return
    with _lock:
        _log_tail.append(line)
        del _log_tail[:-_LOG_TAIL_LIMIT]
        _status["log_tail"] = list(_log_tail)


def reset_for_tests() -> None:
    """Reset module state between tests."""
    global _proc, _sp_creds, _stop_event, _thread, _runner_tailer_started

    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
    with _lock:
        _proc = None
        _sp_creds = None
        _stop_event = None
        _thread = None
        _runner_tailer_started = False
        _log_tail.clear()
        _status.clear()
        _status.update({
            "configured": False,
            "running": False,
            "installed": False,
            "host_launched": False,
            "server_url": None,
            "pid": None,
            "stage": "idle",
            "last_error": None,
            "log_tail": [],
        })


def omnigents_host_enabled() -> bool:
    """True when CoDA is configured to register as an Omnigents host."""
    return bool(os.environ.get("OMNIGENTS_SERVER_URL", "").strip())


def _omnigents_bin() -> str:
    """Path to the host CLI. The package was renamed omnigents→omnigent; the
    installed executable is ``omnigent`` (alias ``omni``). Fall back to the
    legacy ``omnigents`` name for older builds."""
    home = os.environ.get("HOME", "/app/python/source_code")
    bindir = os.path.join(home, ".local", "bin")
    for name in ("omnigent", "omnigents"):
        path = os.path.join(bindir, name)
        if os.path.exists(path):
            return path
    return os.path.join(bindir, "omnigent")  # default for the install check


def _materialize_spec(spec: str, sp_creds: dict[str, str] | None = None) -> str:
    """Resolve OMNIGENTS_WHEEL_SPEC to a locally-usable install source.

    A ``/Volumes/...`` UC Volume path is downloaded via the Databricks files
    SDK into a temp dir (the app SP's READ_VOLUME grant authorizes this) and
    that local dir is returned — Databricks Apps does not FUSE-mount Volumes
    for every app, so we can't rely on the path existing on disk. Any other
    spec (an existing dir, a git ref, a PyPI name) is returned unchanged.

    Authenticates the SDK with the captured app-SP creds: by the time this
    runs, CoDA has already stripped DATABRICKS_CLIENT_ID/SECRET from the env
    (and popped the PAT vars), so a bare ``WorkspaceClient()`` can't configure
    default credentials. We pass the captured SP creds explicitly.
    """
    if spec.startswith("/Volumes/") and not os.path.isdir(spec):
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.core import Config

        tmp = tempfile.mkdtemp(prefix="oa-wheels-")
        if sp_creds:
            # auth_type="oauth-m2m" pins the SDK to the SP creds. Without it the
            # unified-auth resolver ALSO discovers the ambient DATABRICKS_TOKEN
            # (the PAT the rotator just bootstrapped) and refuses with "more than
            # one authorization method configured: oauth and pat".
            w = WorkspaceClient(config=Config(
                host=sp_creds["host"],
                client_id=sp_creds["client_id"],
                client_secret=sp_creds["client_secret"],
                auth_type="oauth-m2m",
            ))
        else:
            w = WorkspaceClient()
        listed = list(w.files.list_directory_contents(spec))
        wheels = [e for e in listed if (e.path or "").endswith(".whl")]
        if not wheels:
            raise FileNotFoundError(f"no .whl in UC Volume {spec}")
        for entry in wheels:
            dest = os.path.join(tmp, os.path.basename(entry.path))
            resp = w.files.download(entry.path)
            with open(dest, "wb") as f:
                f.write(resp.contents.read())
        logger.info("Downloaded %d host wheels from %s", len(wheels), spec)
        return tmp
    return spec


def _install_command(spec: str, *, force: bool = False) -> list[str]:
    """Build the ``uv tool install`` command for the configured source.

    ``spec`` (already materialized to a local path when it was a UC Volume)
    may be:

    * a **directory** of wheels — we ``--find-links`` it and install the main
      ``omnigent`` wheel, letting uv resolve the sibling
      ``omnigent-client`` / ``omnigent-ui-sdk`` wheels from the same dir
      while pulling the rest from public PyPI; or
    * a plain install **spec** (git ref / PyPI name / wheel path).

    ``databricks-sdk`` is added because the host's Databricks auth path imports
    it — without it in the tool env, ``_resolve_databricks_auth`` raises
    ImportError, the token factory caches ``None``, and the tunnel WS upgrade
    goes out unauthenticated → 302 loop. (The old ``click==8.1.8`` pin is no
    longer needed: current ``omnigent`` pins click correctly in its deps.)
    """
    pin = ["--with", "databricks-sdk"]
    # ``uv tool install`` no-ops when the tool is already installed, so a new
    # wheel in the UC Volume would be ignored on restart. ``--force`` makes it
    # reinstall over the existing tool — required to roll out a new runner
    # build. Opt-in via OMNIGENTS_FORCE_REINSTALL (see ensure_installed).
    force_flag = ["--force"] if force else []
    if os.path.isdir(spec):
        main = sorted(
            f for f in os.listdir(spec)
            if f.startswith("omnigent-") and f.endswith(".whl")
        )
        if not main:
            raise FileNotFoundError(f"no omnigent-*.whl in {spec}")
        return [
            "uv", "tool", "install",
            *force_flag,
            "--find-links", spec,
            "--index-url", "https://pypi.org/simple",
            *pin,
            os.path.join(spec, main[-1]),
        ]
    return [
        "uv", "tool", "install", *force_flag,
        "--index-url", "https://pypi.org/simple", *pin, spec,
    ]


def ensure_installed(sp_creds: dict[str, str] | None = None) -> bool:
    """Install the host CLI if it isn't already present (FR-1).

    ``sp_creds`` authenticates a UC-Volume wheel download (see
    :func:`_materialize_spec`). Returns True if the CLI is available afterward.

    Set ``OMNIGENTS_FORCE_REINSTALL=1`` to reinstall even when the binary is
    already present — the way to roll out a new runner build, since
    ``uv tool install`` (and this early return) would otherwise keep the stale
    binary across restarts.
    """
    force = os.environ.get("OMNIGENTS_FORCE_REINSTALL", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if os.path.exists(_omnigents_bin()) and not force:
        return True
    spec = os.environ.get("OMNIGENTS_WHEEL_SPEC", "").strip()
    if not spec:
        logger.warning(
            "OMNIGENTS_SERVER_URL is set but OMNIGENTS_WHEEL_SPEC is not — "
            "cannot install the omnigents host CLI. Host NOT started."
        )
        return False
    try:
        cmd = _install_command(_materialize_spec(spec, sp_creds), force=force)
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
    except Exception as e:  # download/install failure must not crash the worker
        detail = getattr(e, "stderr", "") or str(e)
        logger.warning("omnigents install failed: %s; host NOT started", detail[:300])
        _set(last_error=f"install: {detail[:280]}")
        return False
    return os.path.exists(_omnigents_bin())


def _ensure_https(host: str) -> str:
    """Normalize a host and prefix https:// if absent.

    Wraps the shared ``utils.ensure_https`` but first trims whitespace and a
    trailing slash (the host/server values here arrive from env vars and config
    that may carry either).
    """
    return ensure_https(host.strip().rstrip("/"))


def capture_sp_credentials() -> dict[str, str] | None:
    """Snapshot the app SP's M2M OAuth creds before CoDA strips them.

    MUST be called in ``initialize_app`` *before* the
    ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET`` pop. Returns the
    creds needed to mint an OAuth token for the host tunnel, or ``None`` when
    they aren't present (e.g. local dev / PAT-only).
    """
    client_id = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
    host = _ensure_https(os.environ.get("DATABRICKS_HOST", ""))
    if not (client_id and client_secret and host):
        return None
    return {"client_id": client_id, "client_secret": client_secret, "host": host}


def _write_oauth_profile(creds: dict[str, str]) -> None:
    """Write the broker-owned workspace pointer without persisting SP creds."""
    home = os.environ.get("HOME", "/app/python/source_code")
    cfg_path = os.path.join(home, ".databrickscfg")

    config = configparser.ConfigParser(interpolation=None)
    if os.path.exists(cfg_path):
        config.read(cfg_path)
    config[_HOST_PROFILE] = {
        "host": creds["host"],
    }

    fd, tmp_path = tempfile.mkstemp(dir=home, prefix=".databrickscfg.")
    try:
        with os.fdopen(fd, "w") as f:
            config.write(f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, cfg_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    logger.info("Wrote secret-free profile '%s' for Omnigent runner routing", _HOST_PROFILE)


def _ensure_claude() -> None:
    """Install the Claude Code CLI to ~/.local/bin if missing (best-effort).

    The native ``claude-native`` harness gates on the ``claude`` binary being on
    PATH (onboarding/harness_readiness.py); without it the host reports
    ``claude-native: not configured`` and every native session is rejected at
    launch with ``harness_not_configured``. CoDA installs ``claude`` in
    ``setup_claude.py``, but that runs inside ``run_setup()`` which is gated
    behind the interactive PAT bootstrap — the auto host-connect path (SP creds,
    no PAT) never runs it. So install here too, mirroring ``_ensure_tmux``: same
    ``claude.ai/install.sh`` fetch, to the same ``~/.local/bin`` the host
    prepends to the runner's PATH. Idempotent; never blocks the host on failure.
    """
    home = os.environ.get("HOME", "/app/python/source_code")
    claude_path = os.path.join(home, ".local", "bin", "claude")
    if os.path.exists(claude_path):
        logger.info("claude already present at %s", claude_path)
        return
    try:
        result = subprocess.run(
            ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
            env={**os.environ, "HOME": home},
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0 and os.path.exists(claude_path):
            logger.info("claude installed to %s", claude_path)
        else:
            logger.warning(
                "claude install did NOT land (rc=%s); claude-native will report "
                "not-configured. stdout=%r stderr=%r",
                result.returncode,
                (result.stdout or "").strip()[-500:],
                (result.stderr or "").strip()[-500:],
            )
    except Exception as e:  # never block host launch on claude install
        logger.warning("claude install failed (non-fatal): %s", e)


def _ensure_claude_settings(sp_creds: dict[str, str]) -> None:
    """Write ~/.claude/settings.json so native Claude Code can auth (best-effort).

    Native ``claude`` needs ``ANTHROPIC_BASE_URL`` (the Databricks AI Gateway
    ``/anthropic`` endpoint) plus a token source; without them it replies
    "Not logged in · Please run /login" and no session produces a real answer.
    CoDA's ``setup_claude.py`` writes that config, but it runs inside
    ``run_setup()`` behind the interactive PAT bootstrap — the auto host-connect
    path (SP creds, no PAT) never runs it. So run it here, mirroring
    ``_ensure_claude`` / ``_ensure_tmux``.

    ``setup_claude.py`` gates its settings.json write on ``DATABRICKS_TOKEN``
    (used once to discover serving endpoints), so mint an SP OAuth bearer and
    pass it in. Going forward the installed apiKeyHelper (spec C, now on by
    default) re-mints per-TTL from the ``omnigents-host`` profile, so the
    one-shot token is only needed for the initial write. Set
    ``DISABLE_SP_APIKEYHELPER=true`` to force the legacy static-token path.
    Idempotent; never blocks host launch on failure.
    """
    try:
        token = _sp_bearer(sp_creds)
    except Exception as e:
        logger.warning("could not mint SP token for claude settings: %s", e)
        return
    env = os.environ.copy()
    env["DATABRICKS_TOKEN"] = token
    # apiKeyHelper is on by default now; nothing to enable here. (An operator
    # can still force the legacy static-token path with DISABLE_SP_APIKEYHELPER.)
    env.setdefault("CODA_VENV_PYTHON", sys.executable)
    home = env.get("HOME", "/app/python/source_code")
    local_bin = os.path.join(home, ".local", "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
    try:
        result = subprocess.run(
            [sys.executable, "setup_claude.py"],
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        settings = os.path.join(home, ".claude", "settings.json")
        logger.info(
            "setup_claude.py rc=%s; settings.json exists=%s",
            result.returncode,
            os.path.exists(settings),
        )
        if result.returncode != 0:
            logger.warning(
                "setup_claude.py stderr=%r", (result.stderr or "").strip()[-500:]
            )
    except Exception as e:  # never block host launch on claude settings
        logger.warning("setup_claude.py failed (non-fatal): %s", e)


def _pi_enabled() -> bool:
    """True unless ENABLE_PI is explicitly falsey (mirrors setup_pi.py's gate)."""
    return os.environ.get("ENABLE_PI", "true").strip().lower() not in ("false", "0", "no")


def _ensure_pi() -> None:
    """Install the Pi CLI to ~/.local/bin if missing (best-effort).

    Mirrors ``_ensure_claude``: the auto host-connect path (SP creds, no PAT)
    never runs ``run_setup()``, so ``setup_pi.py``'s npm install doesn't happen
    there. Install here too so the ``pi`` harness resolves the binary on the
    runner's PATH. Unlike Claude (curl installer), Pi is an npm package, so use
    the same ``npm install --prefix=$HOME/.local`` idiom as ``setup_gemini.py``.
    Idempotent; never blocks the host on failure.
    """
    home = os.environ.get("HOME", "/app/python/source_code")
    pi_path = os.path.join(home, ".local", "bin", "pi")
    if os.path.exists(pi_path):
        logger.info("pi already present at %s", pi_path)
        return
    try:
        result = subprocess.run(
            ["npm", "install", "-g", "--ignore-scripts",
             f"--prefix={os.path.join(home, '.local')}",
             "@earendil-works/pi-coding-agent"],
            env={**os.environ, "HOME": home},
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0 and os.path.exists(pi_path):
            logger.info("pi installed to %s", pi_path)
        else:
            logger.warning(
                "pi install did NOT land (rc=%s); the pi harness will report "
                "not-configured. stdout=%r stderr=%r",
                result.returncode,
                (result.stdout or "").strip()[-500:],
                (result.stderr or "").strip()[-500:],
            )
    except Exception as e:  # never block host launch on pi install
        logger.warning("pi install failed (non-fatal): %s", e)


def _ensure_pi_settings(sp_creds: dict[str, str]) -> None:
    """Write ~/.pi/agent/models.json so the pi harness can auth (best-effort).

    Mirrors ``_ensure_claude_settings``: the auto host-connect path never runs
    ``run_setup()``, so ``setup_pi.py``'s config write doesn't happen there.
    ``setup_pi.py`` gates its config write on ``DATABRICKS_TOKEN``, so mint an SP
    OAuth bearer and pass it in, then re-run the script -- single source of truth
    for the models.json schema and gateway/model resolution, exactly like Claude.
    setup_pi.py writes a per-request ``!command`` apiKey that runs the shared
    token helper (SP OAuth from the omnigents-host profile here), so there is no
    static token to refresh afterward. Idempotent; never blocks host launch on
    failure.
    """
    try:
        token = _sp_bearer(sp_creds)
    except Exception as e:
        logger.warning("could not mint SP token for pi settings: %s", e)
        return
    env = os.environ.copy()
    env["DATABRICKS_TOKEN"] = token
    env.setdefault("CODA_VENV_PYTHON", sys.executable)
    home = env.get("HOME", "/app/python/source_code")
    local_bin = os.path.join(home, ".local", "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
    try:
        result = subprocess.run(
            [sys.executable, "setup_pi.py"],
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        models = os.path.join(home, ".pi", "agent", "models.json")
        logger.info(
            "setup_pi.py rc=%s; models.json exists=%s",
            result.returncode,
            os.path.exists(models),
        )
        if result.returncode != 0:
            logger.warning(
                "setup_pi.py stderr=%r", (result.stderr or "").strip()[-500:]
            )
    except Exception as e:  # never block host launch on pi settings
        logger.warning("setup_pi.py failed (non-fatal): %s", e)


def _opencode_enabled() -> bool:
    """True unless ENABLE_OPENCODE is explicitly falsey (mirrors setup_opencode.py)."""
    return os.environ.get("ENABLE_OPENCODE", "true").strip().lower() not in (
        "false",
        "0",
        "no",
    )


def _ensure_opencode() -> None:
    """Install the OpenCode CLI to ~/.local/bin if missing (best-effort).

    Mirrors ``_ensure_pi``: the auto host-connect path (SP creds, no PAT) never
    runs ``run_setup()``, so ``setup_opencode.py``'s npm install doesn't happen
    there. Install here too so the ``opencode-native`` harness resolves the
    ``opencode`` binary on the runner's PATH. Idempotent; never blocks the host.
    """
    home = os.environ.get("HOME", "/app/python/source_code")
    opencode_path = os.path.join(home, ".local", "bin", "opencode")
    if os.path.exists(opencode_path):
        logger.info("opencode already present at %s", opencode_path)
        return
    try:
        result = subprocess.run(
            ["npm", "install", "-g",
             f"--prefix={os.path.join(home, '.local')}",
             "opencode-ai"],
            env={**os.environ, "HOME": home},
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0 and os.path.exists(opencode_path):
            logger.info("opencode installed to %s", opencode_path)
        else:
            logger.warning(
                "opencode install did NOT land (rc=%s); the opencode-native "
                "harness will report not-configured. stdout=%r stderr=%r",
                result.returncode,
                (result.stdout or "").strip()[-500:],
                (result.stderr or "").strip()[-500:],
            )
    except Exception as e:  # never block host launch on opencode install
        logger.warning("opencode install failed (non-fatal): %s", e)


def _ensure_opencode_settings(sp_creds: dict[str, str]) -> None:
    """Write ~/.config/opencode/opencode.json so opencode-native can auth (best-effort).

    Mirrors ``_ensure_pi_settings``: the auto host-connect path never runs
    ``run_setup()``, so ``setup_opencode.py``'s config write doesn't happen
    there. ``setup_opencode.py`` gates its config write on ``DATABRICKS_TOKEN``,
    so mint an SP OAuth bearer and pass it in, then re-run the script -- single
    source of truth for the opencode.json schema and endpoint/model resolution.

    Why this matters for opencode-native: unlike Pi (whose native resolver reads
    ``~/.omnigent/config.yaml``), opencode-native reads its provider from
    ``agent_spec.executor.config["profile"]`` — which CoDA can't set (server-side
    spec) — and otherwise falls back to the user's GLOBAL
    ``~/.config/opencode/opencode.json`` (via ``maybe_merge_user_provider_config``).
    So this global config IS the credential path for opencode-native on the host.
    setup_opencode.py points it at the content-filter proxy (which injects a fresh
    ~/.databrickscfg token), lists only endpoints the workspace actually serves,
    and pins a valid default model. Idempotent; never blocks host launch.
    """
    try:
        token = _sp_bearer(sp_creds)
    except Exception as e:
        logger.warning("could not mint SP token for opencode settings: %s", e)
        return
    env = os.environ.copy()
    env["DATABRICKS_TOKEN"] = token
    home = env.get("HOME", "/app/python/source_code")
    local_bin = os.path.join(home, ".local", "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
    try:
        result = subprocess.run(
            [sys.executable, "setup_opencode.py"],
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        config = os.path.join(home, ".config", "opencode", "opencode.json")
        logger.info(
            "setup_opencode.py rc=%s; opencode.json exists=%s",
            result.returncode,
            os.path.exists(config),
        )
        if result.returncode != 0:
            logger.warning(
                "setup_opencode.py stderr=%r", (result.stderr or "").strip()[-500:]
            )
    except Exception as e:  # never block host launch on opencode settings
        logger.warning("setup_opencode.py failed (non-fatal): %s", e)


def _ensure_tmux() -> None:
    """Install a static tmux to ~/.local/bin if missing (best-effort).

    The native ``omnigent claude`` / ``omnigent codex`` harnesses launch the
    agent through a local tmux terminal and refuse to start without tmux on
    PATH — surfacing in the Web UI as "Claude Code isn't configured on <host>".
    CoDA's ``run_setup()`` also installs tmux, but that path is gated behind the
    interactive PAT bootstrap; the host can connect via SP creds before any PAT
    is pasted, so install here too. Runs BEFORE ``_run_setup_once`` so the
    harness-readiness probe sees tmux. Idempotent (install_tmux.sh no-ops when
    tmux already resolves). Never blocks the host on failure.
    """
    home = os.environ.get("HOME", "/app/python/source_code")
    local_bin = os.path.join(home, ".local", "bin")
    tmux_path = os.path.join(local_bin, "tmux")
    if os.path.exists(tmux_path):
        logger.info("tmux already present at %s", tmux_path)
        return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install_tmux.sh")
    if not os.path.exists(script):
        logger.warning("install_tmux.sh not found at %s; native harnesses need tmux", script)
        return
    try:
        # Log the install outcome: the native claude/codex harnesses report
        # "not configured" whenever tmux is absent, and this install fetches a
        # static binary from GitHub — a fetch that can fail on a container with
        # restricted egress. capture_output hid that failure before, leaving
        # only a silent claude-native:False downstream.
        result = subprocess.run(
            ["bash", script], check=False, capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0 and os.path.exists(tmux_path):
            logger.info("tmux installed: %s", (result.stdout or "").strip().splitlines()[-1:])
        else:
            logger.warning(
                "tmux install did NOT land (rc=%s); native harnesses will report "
                "not-configured. stdout=%r stderr=%r",
                result.returncode,
                (result.stdout or "").strip()[-500:],
                (result.stderr or "").strip()[-500:],
            )
    except Exception as e:  # never block host launch on tmux install
        logger.warning("tmux install failed (non-fatal): %s", e)


def _start_runner_log_tailer() -> None:
    """Stream runner log files into the app logger (best-effort).

    ``omnigent host`` spawns each session's runner as a separate process that
    writes to ``$HOME/.omnigent/logs/host-runner/runner-*.log`` — files that
    never reach the app's stdout, so runner-side failures (e.g. a native
    terminal that "failed to start; see runner logs") are invisible through
    ``databricks apps logs``, which has no container shell to read them. This
    daemon thread watches that directory and forwards new/growing runner logs
    to the same ``logger.info`` / ``_append_log`` sinks the host stdout uses,
    so those failures surface without a browser terminal. Idempotent (starts
    at most one tailer); never blocks or crashes the supervisor.
    """
    global _runner_tailer_started
    with _lock:
        if _runner_tailer_started:
            return
        _runner_tailer_started = True

    home = os.environ.get("HOME", "/app/python/source_code")
    log_dir = os.path.join(home, ".omnigent", "logs", "host-runner")

    def _tail() -> None:
        offsets: dict[str, int] = {}
        while True:
            try:
                names = os.listdir(log_dir) if os.path.isdir(log_dir) else []
                for name in names:
                    if not (name.startswith("runner-") and name.endswith(".log")):
                        continue
                    path = os.path.join(log_dir, name)
                    with open(path, errors="replace") as f:
                        f.seek(offsets.get(name, 0))
                        for line in f:
                            if line.endswith("\n"):
                                text = line.rstrip()
                                if text:
                                    _append_log(text)
                                    logger.info("[runner:%s] %s", name, text)
                        offsets[name] = f.tell()
            except Exception:  # never let a tail error take down the thread
                pass
            time.sleep(1.0)

    threading.Thread(target=_tail, daemon=True, name="omnigent-runner-log-tail").start()


def _run_setup_once() -> None:
    """Auto-configure harnesses from CoDA's ambient LLM creds (best-effort).

    Without this, ``omnigent host`` connects but no harness is configured for
    the runner — a session started from the Web UI fails ("Claude Code isn't
    configured on <host>" / the runner can't auth to the model). ``omnigent
    setup`` detects credentials already in the env (CoDA's ``ANTHROPIC_*`` /
    AI-Gateway vars) and writes them into ``~/.omnigent/config.yaml``.

    The command is interactive: it auto-adopts ambient creds, then drops into a
    harness menu. We feed ``q`` so it exits cleanly (rc 0) right after the
    auto-config — verified locally: the adopted creds persist on quit. Runs in
    the NORMAL env (keeps ``ANTHROPIC_*``), not the stripped host-tunnel env.
    Best-effort: failure here must not stop the host from launching.
    """
    try:
        env = os.environ.copy()
        home = env.get("HOME", "/app/python/source_code")
        local_bin = os.path.join(home, ".local", "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        result = subprocess.run(
            [_omnigents_bin(), "setup"],
            input="q\n",
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=home,
        )
        for line in (result.stdout + result.stderr).splitlines():
            if "auto-configured" in line or "configured for omnigent" in line:
                logger.info("[omnigents-setup] %s", line.strip())
        logger.info("omnigent setup completed (rc=%s)", result.returncode)
    except Exception as e:  # never block host launch on setup
        logger.warning("omnigent setup failed (non-fatal): %s", e)


def _configure_omnigent_databricks_auth() -> None:
    """Pin ~/.omnigent/config.yaml's auth to the omnigents-host Databricks profile.

    ``omnigent setup`` (run above) auto-adopts CoDA's ambient ``ANTHROPIC_*`` env
    as an ``auth: {type: api_key, ...}`` entry. But the runner's native-Pi
    credential resolver (``omnigent.pi_native_credentials._databricks_pi_provider``)
    only recognizes a ``kind="databricks"`` provider — it reads the host from a
    ``~/.databrickscfg`` profile and builds a ``{host}/anthropic`` base URL with a
    per-request ``!<auth_command>`` bearer. Without it, a dispatched Pi session
    logs "no omnigent-configured provider … Pi will use its own login" and runs
    unauthenticated. The same databricks entry is what native Claude/Codex use, so
    this fixes all three harnesses on the runner, not just Pi.

    So overwrite the ``auth`` block with ``{type: databricks, profile:
    omnigents-host}``. That profile contains only the workspace host; its token
    command is intercepted by the loopback-broker CLI shim. Read-merge-write to
    preserve setup's other keys; idempotent; best-effort.
    """
    home = os.environ.get("HOME", "/app/python/source_code")
    config_path = os.path.join(home, ".omnigent", "config.yaml")
    desired = {"type": "databricks", "profile": _HOST_PROFILE}
    try:
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            config = {}
        if not isinstance(config, dict):
            config = {}
        if config.get("auth") == desired:
            logger.info("omnigent auth already pinned to '%s' profile", _HOST_PROFILE)
            return
        config["auth"] = desired
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        tmp = f"{config_path}.tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp, config_path)  # atomic
        logger.info(
            "Pinned omnigent auth to Databricks profile '%s' (runner native-Pi/Claude/Codex)",
            _HOST_PROFILE,
        )
    except Exception as e:  # never block host launch on config write
        logger.warning("could not pin omnigent databricks auth (non-fatal): %s", e)


def _install_broker_cli_wrapper() -> None:
    """Intercept only Omnigent's profile token command; delegate all other CLI use."""
    real_cli = shutil.which("databricks")
    if not real_cli:
        raise RuntimeError("Databricks CLI is required for Omnigent native harness auth")
    home = os.environ.get("HOME", "/app/python/source_code")
    wrapper_dir = os.path.join(home, ".coda-broker-bin")
    wrapper = write_databricks_token_wrapper(wrapper_dir, real_cli)
    logger.info("Installed Omnigent token-broker CLI wrapper at %s", wrapper)


def _run_host_once(server_url: str, stop_event: threading.Event | None = None) -> int:
    """Run ``omnigents host`` in the foreground until it exits. Returns rc."""
    global _proc

    home = os.environ.get("HOME", "/app/python/source_code")
    # `omnigent host` takes only the server URL (no --profile on current main).
    cmd = [_omnigents_bin(), "host", server_url]

    # The runner inherits this env; CoDA's ANTHROPIC_* AI-Gateway creds are
    # already present and get forwarded host→runner via Omnigents'
    # HARNESS_CREDENTIAL_ENV_VARS. We never inject the SP secret here. Fetch a
    # short-lived bearer from the loopback broker for this tunnel launch and
    # clear ambient Databricks vars that could shadow it in unified auth.
    env = config_profile_env(_HOST_PROFILE)
    env.pop("DATABRICKS_CONFIG_PROFILE", None)
    token = fetch_sp_token()
    if not token:
        raise RuntimeError("SP token broker returned no token for host launch")
    env["DATABRICKS_HOST"] = (_sp_creds or {}).get("host", "")
    env["DATABRICKS_TOKEN"] = token
    env["DATABRICKS_AUTH_TYPE"] = "pat"
    env["OMNIGENT_DATABRICKS_TOKEN_COMMAND"] = shlex.join(
        [sys.executable, os.path.join(home, ".claude", "anthropic-token-helper.py")]
    )
    local_bin = os.path.join(home, ".local", "bin")
    broker_bin = os.path.join(home, ".coda-broker-bin")
    path_parts = [broker_bin, local_bin, env.get("PATH", "")]
    env["PATH"] = ":".join(part for part in path_parts if part)
    stable_identity = _stable_host_identity()
    if stable_identity is not None:
        env.setdefault("OMNIGENT_HOST_ID", stable_identity[0])
        env.setdefault("OMNIGENT_HOST_NAME", stable_identity[1])
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=home,
        env=env,
    )
    with _lock:
        _proc = proc
        _status["pid"] = proc.pid
        _status["running"] = True
    try:
        stdout = proc.stdout
        while proc.poll() is None:
            if stop_event is not None and stop_event.is_set():
                proc.terminate()
                break
            if stdout is not None:
                readable, _, _ = select.select([stdout], [], [], 0.2)
                if readable:
                    line = stdout.readline()
                    if line:
                        _append_log(line)
                        logger.info("[omnigents-host] %s", line.rstrip())
            else:
                time.sleep(0.2)

        if stdout is not None:
            for line in stdout:
                _append_log(line)
                logger.info("[omnigents-host] %s", line.rstrip())
        return proc.wait()
    finally:
        with _lock:
            if _proc is proc:
                _proc = None
            _status["pid"] = None


def _supervise(
    server_url: str,
    sp_creds: dict[str, str],
    stop_event: threading.Event,
) -> None:
    """Install, write the profile, then run the host with bounded backoff.

    Runs entirely in a background thread so NOTHING here blocks app startup
    (NFR-4) — the wheel install can take minutes and must never delay the
    gunicorn worker's readiness or trip the Databricks Apps boot deadline.
    """
    _set(stage="installing")
    if not ensure_installed(sp_creds):
        _set(stage="install_failed")  # ensure_installed already logged why
        return
    # Log the installed omnigent version to the host's stdout so `databricks
    # apps logs` shows which wheel is actually live — the runner subprocess's
    # own logs are unreliable to grep (rolling buffer, zombie-runner spam).
    try:
        _ver = subprocess.run(
            [_omnigents_bin(), "--version"],
            check=False, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        logger.info("OMNIGENT VERSION INSTALLED: %s", _ver or "(unknown)")
    except Exception as e:  # never let a version probe block boot
        logger.info("OMNIGENT VERSION INSTALLED: (probe failed: %s)", e)
    _set(installed=True, stage="writing_profile")
    try:
        _write_oauth_profile(sp_creds)
        _install_broker_cli_wrapper()
    except Exception as e:
        logger.warning("Could not configure broker-backed host auth: %s; host NOT started", e)
        _set(stage="profile_failed", last_error=str(e))
        return
    # Configure harnesses from CoDA's ambient LLM creds so runners can auth
    # (otherwise sessions started from the Web UI fail "not configured").
    # Install tmux FIRST so the readiness probe in _run_setup_once sees it and
    # the native claude/codex harnesses report configured.
    _set(stage="configuring_harnesses")
    _ensure_claude()
    _ensure_claude_settings(sp_creds)
    # Pi harness (host path): install the binary + write models.json. Pi's
    # apiKey is a per-request `!command` that runs the shared token helper (SP
    # OAuth from the omnigents-host profile here), so there is no static token
    # to refresh -- a long-running pi resolves a live token each request. Gated
    # by ENABLE_PI, mirroring the interactive path.
    if _pi_enabled():
        _ensure_pi()
        _ensure_pi_settings(sp_creds)
    # OpenCode harness (host path): install the binary + write the GLOBAL
    # ~/.config/opencode/opencode.json. opencode-native reads its provider from
    # the agent spec's executor.config.profile (which CoDA can't set) and else
    # falls back to this global config, so it IS opencode's credential path on
    # the host. setup_opencode.py routes through the content-filter proxy (fresh
    # token injected), lists only served endpoints, and pins a valid default
    # model. Gated by ENABLE_OPENCODE, mirroring the Pi/interactive path.
    if _opencode_enabled():
        _ensure_opencode()
        _ensure_opencode_settings(sp_creds)
    _ensure_tmux()
    _run_setup_once()
    # Pin omnigent's auth to the databricks host profile so the runner's native
    # credential resolver (Pi/Claude/Codex) authenticates via the AI Gateway
    # instead of falling back to "its own login". Must run AFTER _run_setup_once
    # (which writes the env-adopted api_key entry this overwrites).
    _configure_omnigent_databricks_auth()
    # Surface per-session runner logs through the app logger so runner-side
    # failures are visible via `databricks apps logs` (no container shell).
    _start_runner_log_tailer()
    _set(host_launched=True, running=True, stage="running")
    logger.info("Omnigents host supervisor active → %s", server_url)

    backoff = _RESTART_BACKOFF_SECONDS
    while not stop_event.is_set():
        try:
            rc = _run_host_once(server_url, stop_event=stop_event)
            if stop_event.is_set():
                break
            logger.warning("omnigents host exited rc=%s; restarting in %ss", rc, backoff)
        except Exception as e:  # never let host failures take down CoDA
            logger.warning("omnigents host crashed: %s; restarting in %ss", e, backoff)
            _set(last_error=str(e))
        stop_event.wait(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
    _set(running=False, stage="stopped", pid=None)


def connect_host(
    server_url: str,
    sp_creds: dict[str, str] | None,
) -> tuple[bool, dict[str, object]]:
    """Start a supervised ``omnigent host`` for a runtime-supplied server URL."""
    global _sp_creds, _stop_event, _thread

    server_url = server_url.strip()
    if not server_url:
        _set(configured=False, running=False, stage="invalid_server_url", last_error="server_url required")
        return False, get_status()
    if not sp_creds:
        msg = (
            "No app SP credentials captured; Databricks Apps host tunnel "
            "requires OAuth-capable app credentials."
        )
        _set(configured=False, running=False, stage="no_sp_creds", last_error=msg)
        return False, get_status()

    with _lock:
        if _status.get("configured") is True and _status.get("stage") not in (
            "idle",
            "stopped",
            "install_failed",
            "profile_failed",
            "invalid_server_url",
            "no_sp_creds",
        ):
            _status["last_error"] = "host already running"
            return False, get_status()

        _sp_creds = dict(sp_creds)
        _stop_event = threading.Event()
        _status.update({
            "configured": True,
            "running": True,
            "server_url": server_url,
            "stage": "starting",
            "last_error": None,
        })
        _thread = threading.Thread(
            target=_supervise,
            args=(server_url, _sp_creds, _stop_event),
            daemon=True,
            name="omnigent-host",
        )
        _thread.start()
    logger.info("Spawned Omnigent host supervisor thread → %s", server_url)
    return True, get_status()


def disconnect_host() -> dict[str, object]:
    """Stop the running host supervisor/process, if any."""
    global _proc

    with _lock:
        stop_event = _stop_event
        proc = _proc
        if stop_event is not None:
            stop_event.set()
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    _set(configured=False, running=False, stage="stopped", pid=None)
    return get_status()


def start_host(sp_creds: dict[str, str] | None) -> None:
    """Legacy boot-time wrapper around :func:`connect_host`.

    Runtime control should call :func:`connect_host` directly. This remains so
    older app.yaml deployments with ``OMNIGENTS_SERVER_URL`` still behave.
    """
    if not omnigents_host_enabled():
        _set(stage="idle")
        return
    connect_host(os.environ["OMNIGENTS_SERVER_URL"], sp_creds)


def _sp_bearer(sp_creds: dict[str, str]) -> str:
    """Mint an app-SP OAuth (client-credentials) token for server API calls.

    The host this CoDA registers is owned by the app SP (the tunnel authenticates
    as the SP). Server-side actions on that host — sharing it, launching a runner
    — must therefore be called AS the SP, since the server scopes them to the
    host owner. We reuse the captured M2M creds the host tunnel already uses.
    """
    from databricks.sdk.core import Config

    cfg = Config(
        host=sp_creds["host"],
        client_id=sp_creds["client_id"],
        client_secret=sp_creds["client_secret"],
        auth_type="oauth-m2m",
    )
    headers = cfg.authenticate()  # {"Authorization": "Bearer <token>"}
    token = headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        raise RuntimeError("could not mint SP OAuth token")
    return token


def share_and_launch(
    server_url: str,
    sp_creds: dict[str, str] | None,
    grant_user: str,
    launch: bool = True,
) -> dict[str, object]:
    """Grant ``grant_user`` ``use`` on this CoDA host, optionally launch a runner.

    Demonstrates "use CoDA via Omnigent": the host is SP-owned, so the operator's
    personal Omnigent UI can't see it until the owner (the SP) shares it. This
    issues that share via the server's ``PUT /v1/hosts/{id}/permissions/{user}``
    using an SP token, then (optionally) ``POST /v1/hosts/{id}/runners`` to start
    a session on the host. Returns a result dict for the API to surface.
    """
    import json
    import urllib.request

    if not sp_creds:
        return {"ok": False, "error": "no SP creds captured"}
    ident = _stable_host_identity()
    if ident is None:
        return {"ok": False, "error": "could not resolve host id"}
    host_id = ident[0]
    base = _ensure_https(server_url)
    token = _sp_bearer(sp_creds)

    def _call(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{base}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode()[:500]
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:500]

    result: dict[str, object] = {"host_id": host_id}
    gc, gb = _call("PUT", f"/v1/hosts/{host_id}/permissions/{grant_user}", {"level": "use"})
    result["grant_status"] = gc
    result["grant_body"] = gb
    result["ok"] = gc in (200, 201, 204)
    if launch and result["ok"]:
        lc, lb = _call("POST", f"/v1/hosts/{host_id}/runners", {})
        result["launch_status"] = lc
        result["launch_body"] = lb
    return result

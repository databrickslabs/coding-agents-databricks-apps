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
  those creds from the environment early in startup, so we capture them
  *before* that strip and write a short-lived OAuth profile for the host.
* **Harness LLM** — the runner the host spawns authenticates to AI Gateway via
  CoDA's already-injected ``ANTHROPIC_*`` env, forwarded host→runner by
  Omnigents' ``HARNESS_CREDENTIAL_ENV_VARS``. No new LLM credential is minted.

Off by default: with ``OMNIGENTS_SERVER_URL`` unset, nothing here runs and
CoDA behaves exactly as before.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

# Profile name written to ~/.databrickscfg for the host's OAuth (M2M) auth.
# Kept distinct from DEFAULT (CoDA's PAT) so the two credentials never collide.
_HOST_PROFILE = "omnigents-host"

# Supervisor restart policy.
_RESTART_BACKOFF_SECONDS = 10
_MAX_BACKOFF_SECONDS = 120


def omnigents_host_enabled() -> bool:
    """True when CoDA is configured to register as an Omnigents host."""
    return bool(os.environ.get("OMNIGENTS_SERVER_URL", "").strip())


def _omnigents_bin() -> str:
    home = os.environ.get("HOME", "/app/python/source_code")
    return os.path.join(home, ".local", "bin", "omnigents")


def ensure_installed() -> bool:
    """Install the ``omnigents`` host CLI if it isn't already present (FR-1).

    Source is ``OMNIGENTS_WHEEL_SPEC`` (a pip/uv install spec — e.g. a git ref
    or a wheel path). ``click`` is pinned to 8.1.8: the Omnigents CLI assigns
    ``Context.protected_args``, which is read-only in click >=8.2, so a newer
    click breaks ``omnigents host`` at arg-parse. Returns True if the CLI is
    available afterward.
    """
    if os.path.exists(_omnigents_bin()):
        return True
    spec = os.environ.get("OMNIGENTS_WHEEL_SPEC", "").strip()
    if not spec:
        logger.warning(
            "OMNIGENTS_SERVER_URL is set but OMNIGENTS_WHEEL_SPEC is not — "
            "cannot install the omnigents host CLI. Host NOT started."
        )
        return False
    try:
        subprocess.run(
            ["uv", "tool", "install", "--with", "click==8.1.8", spec],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        detail = getattr(e, "stderr", "") or str(e)
        logger.warning("omnigents install failed: %s; host NOT started", detail[:300])
        return False
    return os.path.exists(_omnigents_bin())


def capture_sp_credentials() -> dict[str, str] | None:
    """Snapshot the app SP's M2M OAuth creds before CoDA strips them.

    MUST be called in ``initialize_app`` *before* the
    ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET`` pop. Returns the
    creds needed to mint an OAuth token for the host tunnel, or ``None`` when
    they aren't present (e.g. local dev / PAT-only).
    """
    client_id = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
    host = os.environ.get("DATABRICKS_HOST", "").strip()
    if not (client_id and client_secret and host):
        return None
    return {"client_id": client_id, "client_secret": client_secret, "host": host}


def _write_oauth_profile(creds: dict[str, str]) -> None:
    """Write an OAuth (M2M) profile to ~/.databrickscfg for the host.

    Uses the Databricks SDK config-file format so ``omnigents host
    --profile omnigents-host`` resolves an OAuth token (client-credentials
    flow) rather than a PAT — the token type the Apps proxy accepts.
    """
    home = os.environ.get("HOME", "/app/python/source_code")
    cfg_path = os.path.join(home, ".databrickscfg")

    profile_block = (
        f"\n[{_HOST_PROFILE}]\n"
        f"host = {creds['host']}\n"
        f"client_id = {creds['client_id']}\n"
        f"client_secret = {creds['client_secret']}\n"
    )

    # Append only if the profile isn't already present (idempotent across
    # restarts). The PAT rotator owns [DEFAULT]; we only ever touch our block.
    existing = ""
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            existing = f.read()
    if f"[{_HOST_PROFILE}]" in existing:
        return
    with open(cfg_path, "a") as f:
        f.write(profile_block)
    logger.info("Wrote OAuth profile '%s' for Omnigents host tunnel", _HOST_PROFILE)


def _run_host_once(server_url: str) -> int:
    """Run ``omnigents host`` in the foreground until it exits. Returns rc."""
    home = os.environ.get("HOME", "/app/python/source_code")
    cmd = [_omnigents_bin(), "host", server_url, "--profile", _HOST_PROFILE]

    # The runner inherits this env; CoDA's ANTHROPIC_* AI-Gateway creds are
    # already present and get forwarded host→runner via Omnigents'
    # HARNESS_CREDENTIAL_ENV_VARS. We do NOT inject the SP secret here — the
    # host resolves it from the OAuth profile we wrote.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=home,
    )
    for line in proc.stdout:  # type: ignore[union-attr]
        logger.info("[omnigents-host] %s", line.rstrip())
    return proc.wait()


def _supervise(server_url: str) -> None:
    """Restart the host with bounded backoff; never crash the app."""
    backoff = _RESTART_BACKOFF_SECONDS
    while True:
        try:
            rc = _run_host_once(server_url)
            logger.warning("omnigents host exited rc=%s; restarting in %ss", rc, backoff)
        except Exception as e:  # never let host failures take down CoDA
            logger.warning("omnigents host crashed: %s; restarting in %ss", e, backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)


def start_host(sp_creds: dict[str, str] | None) -> None:
    """Launch the supervised Omnigents host thread, if enabled.

    Call from ``initialize_app`` with the creds captured by
    :func:`capture_sp_credentials`. No-op when disabled or creds are missing.
    """
    if not omnigents_host_enabled():
        return
    server_url = os.environ["OMNIGENTS_SERVER_URL"].strip()

    if not sp_creds:
        logger.warning(
            "OMNIGENTS_SERVER_URL is set but no app SP credentials were "
            "captured — the host tunnel needs OAuth (a PAT is rejected by the "
            "Apps proxy). Host NOT started."
        )
        return

    if not ensure_installed():
        return  # ensure_installed already logged why

    try:
        _write_oauth_profile(sp_creds)
    except Exception as e:
        logger.warning("Could not write OAuth host profile: %s; host NOT started", e)
        return

    thread = threading.Thread(
        target=_supervise,
        args=(server_url,),
        daemon=True,
        name="omnigents-host",
    )
    thread.start()
    logger.info("Started Omnigents host supervisor → %s", server_url)

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
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

# Profile name written to ~/.databrickscfg for the host's OAuth (M2M) auth.
# Kept distinct from DEFAULT (CoDA's PAT) so the two credentials never collide.
_HOST_PROFILE = "omnigents-host"

# Supervisor restart policy.
_RESTART_BACKOFF_SECONDS = 10
_MAX_BACKOFF_SECONDS = 120

# Observable state for /api/omnigents-status (FR-9). Updated as startup
# progresses so the integration can be diagnosed without app log access.
_status: dict[str, object] = {
    "enabled": False,
    "sp_creds_captured": False,
    "installed": False,
    "host_launched": False,
    "stage": "not_started",
    "last_error": None,
}


def get_status() -> dict[str, object]:
    """Return a copy of the current host-integration state."""
    return dict(_status)


def _set(**kw: object) -> None:
    _status.update(kw)


def omnigents_host_enabled() -> bool:
    """True when CoDA is configured to register as an Omnigents host."""
    return bool(os.environ.get("OMNIGENTS_SERVER_URL", "").strip())


def _omnigents_bin() -> str:
    home = os.environ.get("HOME", "/app/python/source_code")
    return os.path.join(home, ".local", "bin", "omnigents")


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
            w = WorkspaceClient(config=Config(
                host=sp_creds["host"],
                client_id=sp_creds["client_id"],
                client_secret=sp_creds["client_secret"],
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


def _install_command(spec: str) -> list[str]:
    """Build the ``uv tool install`` command for the configured source.

    ``spec`` (already materialized to a local path when it was a UC Volume)
    may be:

    * a **directory** of wheels — we ``--find-links`` it and install the main
      ``omnigents`` wheel, letting uv resolve the sibling
      ``omnigents-client`` / ``omnigents-ui-sdk`` wheels from the same dir
      while pulling the rest from public PyPI; or
    * a plain install **spec** (git ref / PyPI name / wheel path).

    ``click`` is pinned to 8.1.8: the Omnigents CLI assigns
    ``Context.protected_args``, read-only in click >=8.2, which breaks
    ``omnigents host`` at arg-parse.
    """
    pin = ["--with", "click==8.1.8"]
    if os.path.isdir(spec):
        main = sorted(
            f for f in os.listdir(spec)
            if f.startswith("omnigents-") and f.endswith(".whl")
        )
        if not main:
            raise FileNotFoundError(f"no omnigents-*.whl in {spec}")
        return [
            "uv", "tool", "install",
            "--find-links", spec,
            "--index-url", "https://pypi.org/simple",
            *pin,
            os.path.join(spec, main[-1]),
        ]
    return ["uv", "tool", "install", "--index-url", "https://pypi.org/simple", *pin, spec]


def ensure_installed(sp_creds: dict[str, str] | None = None) -> bool:
    """Install the host CLI if it isn't already present (FR-1).

    ``sp_creds`` authenticates a UC-Volume wheel download (see
    :func:`_materialize_spec`). Returns True if the CLI is available afterward.
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
        cmd = _install_command(_materialize_spec(spec, sp_creds))
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
    except Exception as e:  # download/install failure must not crash the worker
        detail = getattr(e, "stderr", "") or str(e)
        logger.warning("omnigents install failed: %s; host NOT started", detail[:300])
        _set(last_error=f"install: {detail[:280]}")
        return False
    return os.path.exists(_omnigents_bin())


def _ensure_https(host: str) -> str:
    """Prefix https:// if absent — Databricks config requires the scheme."""
    host = host.strip().rstrip("/")
    if host and not host.startswith(("http://", "https://")):
        return f"https://{host}"
    return host


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
        _set(stage="disabled")
        return
    _set(enabled=True, stage="enabled")
    server_url = os.environ["OMNIGENTS_SERVER_URL"].strip()

    if not sp_creds:
        msg = (
            "OMNIGENTS_SERVER_URL is set but no app SP credentials were "
            "captured — the host tunnel needs OAuth (a PAT is rejected by the "
            "Apps proxy). Host NOT started."
        )
        logger.warning(msg)
        _set(stage="no_sp_creds", last_error=msg)
        return
    _set(sp_creds_captured=True, stage="installing")

    if not ensure_installed(sp_creds):
        _set(stage="install_failed")  # ensure_installed already logged why
        return
    _set(installed=True, stage="writing_profile")

    try:
        _write_oauth_profile(sp_creds)
    except Exception as e:
        logger.warning("Could not write OAuth host profile: %s; host NOT started", e)
        _set(stage="profile_failed", last_error=str(e))
        return

    thread = threading.Thread(
        target=_supervise,
        args=(server_url,),
        daemon=True,
        name="omnigents-host",
    )
    thread.start()
    _set(host_launched=True, stage="running")
    logger.info("Started Omnigents host supervisor → %s", server_url)

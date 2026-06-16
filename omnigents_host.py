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

import hashlib
import logging
import os
import select
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
    global _proc, _sp_creds, _stop_event, _thread

    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
    with _lock:
        _proc = None
        _sp_creds = None
        _stop_event = None
        _thread = None
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
    if os.path.isdir(spec):
        main = sorted(
            f for f in os.listdir(spec)
            if f.startswith("omnigent-") and f.endswith(".whl")
        )
        if not main:
            raise FileNotFoundError(f"no omnigent-*.whl in {spec}")
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

    # auth_type = oauth-m2m is REQUIRED: without it the CLI/SDK doesn't infer
    # client-credentials from client_id/secret and fails with "OAuth is not
    # configured for this host" — so the host tunnel never gets an M2M token.
    profile_block = (
        f"\n[{_HOST_PROFILE}]\n"
        f"host = {creds['host']}\n"
        f"client_id = {creds['client_id']}\n"
        f"client_secret = {creds['client_secret']}\n"
        f"auth_type = oauth-m2m\n"
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


def _run_host_once(server_url: str, stop_event: threading.Event | None = None) -> int:
    """Run ``omnigents host`` in the foreground until it exits. Returns rc."""
    global _proc

    home = os.environ.get("HOME", "/app/python/source_code")
    # `omnigent host` takes only the server URL (no --profile on current main).
    # Auth is driven by DATABRICKS_CONFIG_PROFILE in the env below.
    cmd = [_omnigents_bin(), "host", server_url]

    # The runner inherits this env; CoDA's ANTHROPIC_* AI-Gateway creds are
    # already present and get forwarded host→runner via Omnigents'
    # HARNESS_CREDENTIAL_ENV_VARS. We do NOT inject the SP secret here — the
    # host resolves it from the OAuth profile we wrote.
    env = os.environ.copy()
    # Omnigents' token factory calls _resolve_databricks_auth() WITHOUT a
    # profile, so `--profile` is ignored for the tunnel token; it uses the
    # SDK's default resolution. Point that resolution at our M2M profile via
    # DATABRICKS_CONFIG_PROFILE, and clear every ambient Databricks env var that
    # would otherwise shadow the profile in the SDK's unified-auth resolution.
    # In particular DATABRICKS_WORKSPACE_ID + the DATABRICKS_APP_* vars (which
    # Apps injects) steer auth to the app's ambient identity and cause the
    # tunnel to 302 → OIDC even with the M2M profile present. Verified: a clean
    # env with only the profile vars connects; leaving these in does not.
    local_bin = os.path.join(home, ".local", "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
    stable_identity = _stable_host_identity()
    if stable_identity is not None:
        env.setdefault("OMNIGENT_HOST_ID", stable_identity[0])
        env.setdefault("OMNIGENT_HOST_NAME", stable_identity[1])
    env["DATABRICKS_CONFIG_PROFILE"] = _HOST_PROFILE
    for shadowing in (
        "DATABRICKS_TOKEN",
        "DATABRICKS_HOST",
        "DATABRICKS_WORKSPACE_ID",
        "DATABRICKS_APP_NAME",
        "DATABRICKS_APP_URL",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
    ):
        env.pop(shadowing, None)
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
    _set(installed=True, stage="writing_profile")
    try:
        _write_oauth_profile(sp_creds)
    except Exception as e:
        logger.warning("Could not write OAuth host profile: %s; host NOT started", e)
        _set(stage="profile_failed", last_error=str(e))
        return
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

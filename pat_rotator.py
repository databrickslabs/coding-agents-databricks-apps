"""Auto-rotate short-lived PATs in the background.

Mints a new 15-minute PAT every 10 minutes, writes to ~/.databrickscfg
(immediate CLI/SDK use), and revokes the old PAT. Rotation only runs
while active sessions exist. If the app restarts, the interactive PAT
prompt re-provisions credentials on next session. Fixes #81.
"""

import os
import time
import threading
import logging

import requests

import app_state
from utils import ensure_https, read_non_default_databrickscfg_sections

logger = logging.getLogger(__name__)

# Env overrides exist so e2e tests can compress the cycle to seconds without
# a code change. Production defaults: 15-min tokens rotated every 10 min.
DEFAULT_TOKEN_LIFETIME = int(os.environ.get("PAT_TOKEN_LIFETIME", "900"))
DEFAULT_ROTATION_INTERVAL = int(os.environ.get("PAT_ROTATION_INTERVAL", "600"))


def default_instance_name():
    """Best-effort unique-ish name for THIS CoDA instance.

    Used as the rotation-comment suffix so auto-rotated PATs are attributable
    to the specific CoDA that minted them (multiple CoDAs can share a
    workspace/identity). Priority: explicit override, then the Databricks App
    name, then the app URL host, else empty.
    """
    for key in ("CODA_INSTANCE_NAME", "DATABRICKS_APP_NAME", "DATABRICKS_APPS_APP_NAME"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    url = os.environ.get("DATABRICKS_APP_URL", "").strip()
    if url:
        # e.g. https://my-coda-1234.aws.databricksapps.com -> my-coda-1234
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return host.split(".", 1)[0]
    return ""


def rotation_comment(instance_name):
    """Build the token comment used to tag CoDA auto-rotated PATs.

    Kept stable-prefixed with ``coda-auto-rotated`` so existing tooling and the
    bootstrap-cleanup matcher keep working, with the instance name appended
    when known: ``coda-auto-rotated:<instance>``.
    """
    base = "coda-auto-rotated"
    return f"{base}:{instance_name}" if instance_name else base


class PATRotator:
    """Background PAT rotation with session-aware lifecycle.

    Rotation only runs while there are active sessions. When the last session
    is reaped (24h timeout), rotation stops. When a new session is created,
    rotation resumes.
    """

    def __init__(self, host=None, rotation_interval=DEFAULT_ROTATION_INTERVAL,
                 token_lifetime=DEFAULT_TOKEN_LIFETIME,
                 session_count_fn=None, instance_name=None, cli_refresh_fn=None):
        self._host = ensure_https(host or os.environ.get("DATABRICKS_HOST", ""))
        # Name of this CoDA instance, used to tag auto-rotated PATs so multiple
        # CoDAs sharing a workspace/identity produce attributable token names.
        self._instance_name = (
            instance_name if instance_name is not None else default_instance_name()
        )
        self._rotation_interval = rotation_interval
        self._token_lifetime = token_lifetime
        self._session_count_fn = session_count_fn or (lambda: 0)
        self._cli_refresh_fn = cli_refresh_fn
        self._current_token = os.environ.get("DATABRICKS_TOKEN", "").strip() or None
        self._current_token_id = None
        self._last_rotation_time = None
        self._lock = threading.Lock()
        self._rotation_lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._databrickscfg_path = os.path.join(
            os.environ.get("HOME", "/app/python/source_code"),
            ".databrickscfg"
        )

    @property
    def token(self):
        with self._lock:
            return self._current_token

    @property
    def instance_name(self):
        return self._instance_name

    @property
    def is_token_expired(self):
        """True if the token has likely expired based on last rotation time."""
        with self._lock:
            if not self._last_rotation_time or not self._current_token:
                return self._current_token is None
            return (time.time() - self._last_rotation_time) > self._token_lifetime

    @property
    def seconds_since_rotation(self):
        """Age of the current token in seconds, or None if never rotated.

        Surfaced in /health so a silently-dead rotator (auth about to expire
        while the app still looks 'healthy') is observable before every call
        starts 401-ing ~15 min in — the suspected coda-02 failure window.
        """
        with self._lock:
            if not self._last_rotation_time:
                return None
            return time.time() - self._last_rotation_time

    @property
    def is_alive(self):
        """True if the background rotation thread is running.

        A False here while a token is configured means rotation has stopped —
        auth will die when the current token's lifetime elapses.
        """
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        """Start the background rotation thread."""
        if not self._current_token:
            logger.warning("PAT rotation: no token configured — rotation disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._rotation_loop, daemon=True,
                                        name="pat-rotation")
        self._thread.start()
        logger.info(f"PAT rotation started (interval={self._rotation_interval}s, "
                    f"lifetime={self._token_lifetime}s)")

    def stop(self):
        """Signal the rotation thread to stop."""
        self._stop_event.set()

    def _rotation_loop(self):
        """Background loop: sleep, then rotate if sessions exist OR if the
        in-process token is about to expire. Always-rotating-near-expiry
        prevents the rotator from deadlocking when an idle skip outruns the
        token's lifetime — at that point our own auth would be dead and we
        could never mint a replacement.
        """
        # Force a refresh once we're inside one rotation interval of expiry.
        # That window is the maximum time we can afford to skip a rotation and
        # still be sure the next attempt can authenticate.
        expiry_grace = max(self._rotation_interval, 60)
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._rotation_interval)
            if self._stop_event.is_set():
                break
            try:
                session_count = self._session_count_fn()
                token_age = (
                    time.time() - self._last_rotation_time
                    if self._last_rotation_time else float("inf")
                )
                token_near_expiry = token_age > (self._token_lifetime - expiry_grace)
                if session_count == 0 and not token_near_expiry:
                    logger.info("PAT rotation: no active sessions — skipping rotation")
                    continue
                if session_count == 0 and token_near_expiry:
                    logger.info(
                        "PAT rotation: no active sessions, but token approaching "
                        f"expiry (age={int(token_age)}s, lifetime={self._token_lifetime}s) — rotating anyway"
                    )
                self._rotate_once()
            except Exception as e:
                logger.error(
                    "PAT rotation failed unexpectedly (%s)", type(e).__name__
                )

    def _rotate_once(self):
        """Serialize rotation attempts so only one token can mint at a time."""
        if not self._rotation_lock.acquire(blocking=False):
            logger.warning("PAT rotation skipped: another rotation is in progress")
            return False
        try:
            return self._rotate_once_serialized()
        finally:
            self._rotation_lock.release()

    def _rotate_once_serialized(self):
        """Mint new PAT, persist, refresh all CLIs, then revoke the old PAT."""
        if not self._current_token:
            return False

        logger.info("INFO: PAT rotation starting — minting new short-lived token...")

        # 1. Mint new token
        try:
            resp = requests.post(
                f"{self._host}/api/2.0/token/create",
                headers={"Authorization": f"Bearer {self._current_token}"},
                json={
                    "lifetime_seconds": self._token_lifetime,
                    "comment": rotation_comment(self._instance_name)
                },
                timeout=30
            )
        except requests.RequestException as e:
            logger.error(
                "PAT rotation: create request failed (%s)", type(e).__name__
            )
            return False

        if resp.status_code != 200:
            logger.error("PAT rotation: create failed (%s)", resp.status_code)
            return False

        data = resp.json()
        new_token = data["token_value"]
        new_token_id = data["token_info"]["token_id"]

        old_token_id = self._current_token_id

        # 2. Persist new token (env + file + app_state.json)
        with self._lock:
            self._current_token = new_token
            self._current_token_id = new_token_id
            self._last_rotation_time = time.time()
        config_ok, refresh_result = self._persist_token(new_token)
        refresh_ok = bool(
            refresh_result is not None and getattr(refresh_result, "ok", True)
        )
        persistence_ok = config_ok and refresh_ok
        app_state.set_last_rotation(new_token_id, self._last_rotation_time)

        # 3. Revoke old token only after every credential target accepted the
        # new token. On a partial failure, retain the old token for recovery.
        if old_token_id and persistence_ok:
            try:
                resp = requests.post(
                    f"{self._host}/api/2.0/token/delete",
                    headers={"Authorization": f"Bearer {new_token}"},
                    json={"token_id": old_token_id},
                    timeout=30
                )
                if resp.status_code == 200:
                    logger.info(f"INFO: PAT rotation complete — new token (id={new_token_id}, "
                                f"expires in {self._token_lifetime}s). "
                                f"Old token ELIMINATED (id={old_token_id}).")
                else:
                    logger.warning(f"INFO: PAT rotation complete — new token active (id={new_token_id}), "
                                   f"but old token revocation failed ({resp.status_code}). "
                                   f"Old token (id={old_token_id}) will expire naturally in {self._token_lifetime}s.")
            except requests.RequestException as e:
                logger.warning(
                    "INFO: PAT rotation complete — new token active (id=%s), "
                    "old token revocation request failed (%s). Old token "
                    "(id=%s) will expire naturally in %ss.",
                    new_token_id,
                    type(e).__name__,
                    old_token_id,
                    self._token_lifetime,
                )
        elif old_token_id:
            logger.warning(
                "PAT rotation incomplete — new token active (id=%s), but old "
                "token (id=%s) retained because credential refresh degraded.",
                new_token_id,
                old_token_id,
            )
        elif persistence_ok:
            logger.info(f"INFO: PAT rotation complete — new token (id={new_token_id}, "
                        f"expires in {self._token_lifetime}s). First rotation — no old token to revoke.")
        else:
            logger.warning(
                "PAT rotation incomplete — first minted token active (id=%s), "
                "but credential refresh degraded.",
                new_token_id,
            )

        # Telemetry records only fully persisted rotations.
        if persistence_ok:
            try:
                from telemetry import log_telemetry
                log_telemetry("event", "pat_rotation")
            except Exception:
                pass  # Telemetry must never break rotation

        return persistence_ok

    def revoke_bootstrap_token(self):
        """Revoke only the bootstrap PAT after the first rotation.

        Called once after the bootstrap PAT is replaced by a controlled
        short-lived token.  Lists all tokens, identifies the bootstrap
        as the most-recently-created token without a "coda-auto-rotated"
        comment, and revokes only that one.  Other user PATs (notebooks,
        CI, etc.) are left untouched.
        """
        current_id = self._current_token_id
        token = self._current_token
        if not token or not current_id:
            return

        try:
            resp = requests.get(
                f"{self._host}/api/2.0/token/list",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30
            )
            if resp.status_code != 200:
                logger.warning(f"Bootstrap cleanup: failed to list tokens ({resp.status_code})")
                return
        except requests.RequestException as e:
            logger.warning(f"Bootstrap cleanup: list request failed: {e}")
            return

        token_infos = resp.json().get("token_infos", [])

        # Find the bootstrap PAT: newest non-coda token that isn't the current one
        # A CoDA-rotated token's comment starts with "coda-auto-rotated"
        # (optionally ":<instance>"). The bootstrap PAT never has that prefix,
        # so exclude any coda-tagged token — including ones minted by *other*
        # CoDAs sharing this identity — from bootstrap-revocation candidates.
        candidates = [
            info for info in token_infos
            if info.get("token_id") != current_id
            and not info.get("comment", "").startswith("coda-auto-rotated")
        ]
        if not candidates:
            logger.info("Bootstrap cleanup: no bootstrap token candidate found")
            return

        # The bootstrap PAT is the most recently created candidate
        bootstrap = max(candidates, key=lambda t: t.get("creation_time", 0))
        tid = bootstrap.get("token_id")
        comment = bootstrap.get("comment", "(no comment)")

        try:
            del_resp = requests.post(
                f"{self._host}/api/2.0/token/delete",
                headers={"Authorization": f"Bearer {token}"},
                json={"token_id": tid},
                timeout=30
            )
            if del_resp.status_code == 200:
                logger.info(f"Bootstrap cleanup: revoked bootstrap PAT {tid} ({comment})")
            else:
                logger.warning(f"Bootstrap cleanup: failed to revoke {tid} ({del_resp.status_code})")
        except requests.RequestException as e:
            logger.warning(f"Bootstrap cleanup: revoke request failed: {e}")

    def _persist_token(self, token):
        """Write the token and run the bounded configured-CLI refresh path."""
        os.environ["DATABRICKS_TOKEN"] = token
        config_ok = self._write_databrickscfg(token)
        if self._cli_refresh_fn is None:
            from cli_auth import update_cli_tokens
            refresh = update_cli_tokens
        else:
            refresh = self._cli_refresh_fn

        try:
            result = refresh(token)
        except Exception as error:
            logger.warning(
                "PAT rotation CLI refresh failed (%s)", type(error).__name__
            )
            return config_ok, None

        failed = tuple(getattr(result, "failed", ()))
        refresh_ok = bool(getattr(result, "ok", True))
        if config_ok and refresh_ok:
            logger.info("PAT rotated: configured CLI auth refresh complete")
        else:
            logger.warning(
                "PAT rotated with degraded credential persistence: "
                "databrickscfg=%s failed_clis=%s",
                "ok" if config_ok else "failed",
                ",".join(failed) if failed else "unknown",
            )
        return config_ok, result

    def _write_databrickscfg(self, token):
        """Write token to ~/.databrickscfg for CLI/SDK tools.

        Rewrites ONLY the ``[DEFAULT]`` section and preserves every other
        profile. The Omnigent host appends an ``[omnigents-host]`` OAuth (M2M)
        profile that its spawned runners resolve from this file (a fresh runner
        process has no in-memory SDK token cache, so it re-reads the file every
        time). A naive ``open(..., "w")`` that emitted only ``[DEFAULT]`` wiped
        that block on each rotation, so runners spawned after the first rotation
        failed to authenticate their tunnel (302 -> OIDC login). Keep all
        non-DEFAULT sections so co-owned profiles survive rotation.
        """
        default_block = (
            "[DEFAULT]\n"
            f"host = {self._host}\n"
            f"token = {token}\n"
        )
        preserved = self._read_non_default_sections()
        content = default_block + preserved
        try:
            from cli_auth import _atomic_write_text

            _atomic_write_text(self._databrickscfg_path, content)
            os.chmod(self._databrickscfg_path, 0o600)
            return True
        except OSError as e:
            logger.warning(
                "Could not write .databrickscfg (%s)", type(e).__name__
            )
            return False

    def _read_non_default_sections(self):
        """Preserve co-owned ~/.databrickscfg sections across a DEFAULT rewrite.

        Delegates to the shared helper so the rotator and setup_databricks.py
        honor one "own only [DEFAULT]" contract (see
        utils.read_non_default_databrickscfg_sections).
        """
        return read_non_default_databrickscfg_sections(self._databrickscfg_path)


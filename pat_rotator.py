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
from utils import ensure_https

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_LIFETIME = 900        # 15 minutes
DEFAULT_ROTATION_INTERVAL = 600     # 10 minutes


class PATRotator:
    """Background PAT rotation with session-aware lifecycle.

    Rotation only runs while there are active sessions. When the last session
    is reaped (24h timeout), rotation stops. When a new session is created,
    rotation resumes.
    """

    def __init__(self, host=None, rotation_interval=DEFAULT_ROTATION_INTERVAL,
                 token_lifetime=DEFAULT_TOKEN_LIFETIME,
                 session_count_fn=None):
        self._host = ensure_https(host or os.environ.get("DATABRICKS_HOST", ""))
        self._rotation_interval = rotation_interval
        self._token_lifetime = token_lifetime
        self._session_count_fn = session_count_fn or (lambda: 0)
        self._current_token = os.environ.get("DATABRICKS_TOKEN", "").strip() or None
        self._current_token_id = None
        self._last_rotation_time = None
        self._lock = threading.Lock()
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
    def is_token_expired(self):
        """True if the token has likely expired based on last rotation time."""
        with self._lock:
            if not self._last_rotation_time or not self._current_token:
                return self._current_token is None
            return (time.time() - self._last_rotation_time) > self._token_lifetime

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
        """Background loop: sleep, rotate if sessions exist, repeat."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._rotation_interval)
            if self._stop_event.is_set():
                break
            try:
                session_count = self._session_count_fn()
                if session_count == 0:
                    logger.info("PAT rotation: no active sessions — skipping rotation")
                    continue
                self._rotate_once()
            except Exception as e:
                logger.error(f"PAT rotation failed unexpectedly: {e}")

    def _rotate_once(self):
        """Mint new PAT, persist, revoke old. Returns True on success."""
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
                    "comment": "coda-auto-rotated"
                },
                timeout=30
            )
        except requests.RequestException as e:
            logger.error(f"PAT rotation: create request failed: {e}")
            return False

        if resp.status_code != 200:
            logger.error(f"PAT rotation: create failed ({resp.status_code}): {resp.text}")
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
        self._persist_token(new_token)
        app_state.set_last_rotation(new_token_id, self._last_rotation_time)

        # 3. Revoke old token (best-effort — expires naturally anyway)
        if old_token_id:
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
                logger.warning(f"INFO: PAT rotation complete — new token active (id={new_token_id}), "
                               f"old token revocation request failed: {e}. "
                               f"Old token (id={old_token_id}) will expire naturally in {self._token_lifetime}s.")
        else:
            logger.info(f"INFO: PAT rotation complete — new token (id={new_token_id}, "
                        f"expires in {self._token_lifetime}s). First rotation — no old token to revoke.")

        return True

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
        candidates = [
            info for info in token_infos
            if info.get("token_id") != current_id
            and info.get("comment", "") != "coda-auto-rotated"
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
        """Write rotated token to all persistence layers."""
        os.environ["DATABRICKS_TOKEN"] = token
        self._write_databrickscfg(token)
        from cli_auth import update_cli_tokens
        update_cli_tokens(token)
        logger.info("PAT rotated: all CLIs updated")

    def _write_databrickscfg(self, token):
        """Write token to ~/.databrickscfg for CLI/SDK tools."""
        content = (
            "[DEFAULT]\n"
            f"host = {self._host}\n"
            f"token = {token}\n"
        )
        try:
            with open(self._databrickscfg_path, "w") as f:
                f.write(content)
            os.chmod(self._databrickscfg_path, 0o600)
        except OSError as e:
            logger.warning(f"Could not write .databrickscfg: {e}")


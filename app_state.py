"""Persistent app state at ~/.coda/app_state.json.

Holds app_creator and last_rotation_time so admins can inspect state
and the app can detect stale tokens across restarts.
"""

import json
import os
import logging
import time

logger = logging.getLogger(__name__)

_STATE_DIR = os.path.join(os.environ.get("HOME", "/app/python/source_code"), ".coda")
_STATE_FILE = os.path.join(_STATE_DIR, "app_state.json")


def _read():
    """Read current state, or empty dict if missing/corrupt."""
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write(state):
    """Write state atomically (write-then-rename)."""
    os.makedirs(_STATE_DIR, exist_ok=True)
    tmp = _STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, _STATE_FILE)
        os.chmod(_STATE_FILE, 0o600)
    except OSError as e:
        logger.warning(f"Could not write app state: {e}")


def set_app_owner(owner):
    """Persist the resolved app owner."""
    state = _read()
    state["app_owner"] = owner
    state["owner_resolved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write(state)


def set_last_rotation(token_id, timestamp=None):
    """Persist the last rotation time and token ID."""
    state = _read()
    state["last_rotation_time"] = timestamp or time.time()
    state["last_rotation_iso"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(state["last_rotation_time"])
    )
    state["last_token_id"] = token_id
    _write(state)


def get_last_rotation_time():
    """Return last rotation epoch time, or None."""
    return _read().get("last_rotation_time")


def get_state():
    """Return full state dict (for admin/debug endpoints)."""
    return _read()

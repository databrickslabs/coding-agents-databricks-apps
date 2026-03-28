# PAT Auto-Rotation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement automatic PAT rotation with 2-hour short-lived tokens, rotating every 90 minutes, with persistence to app secrets for restart survival. Fixes #81.

**Architecture:** New `pat_rotator.py` module with a `PATRotator` class that runs a background daemon thread. Uses current PAT to mint new PAT, persists to Secrets API via SP credentials, writes to `~/.databrickscfg`, revokes old PAT. Integrated into `initialize_app()`.

**Tech Stack:** Python, Flask, databricks-sdk, requests, threading

---

### Task 1: Create PATRotator module with tests

**Files:**
- Create: `pat_rotator.py`
- Create: `tests/test_pat_rotator.py`

**Step 1: Write the failing tests**

```python
# tests/test_pat_rotator.py
"""Tests for PAT auto-rotation — short-lived tokens with background refresh."""

import os
import time
import threading
from unittest import mock

import pytest


class TestPATRotation:
    """Core rotation logic."""

    def test_rotate_mints_new_token(self):
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com", rotation_interval=5400, token_lifetime=7200)
        rotator._current_token = "old-pat"
        rotator._current_token_id = "old-id"

        mock_response_create = mock.MagicMock()
        mock_response_create.status_code = 200
        mock_response_create.json.return_value = {
            "token_value": "new-pat",
            "token_info": {"token_id": "new-id", "expiry_time": int(time.time() + 7200) * 1000}
        }
        mock_response_delete = mock.MagicMock()
        mock_response_delete.status_code = 200

        with mock.patch("pat_rotator.requests.post") as mock_post:
            mock_post.side_effect = [mock_response_create, mock_response_delete]
            with mock.patch.object(rotator, "_persist_token"):
                result = rotator._rotate_once()

        assert result is True
        assert rotator._current_token == "new-pat"
        assert rotator._current_token_id == "new-id"

    def test_rotate_revokes_old_token(self):
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com")
        rotator._current_token = "old-pat"
        rotator._current_token_id = "old-id"

        mock_response_create = mock.MagicMock()
        mock_response_create.status_code = 200
        mock_response_create.json.return_value = {
            "token_value": "new-pat",
            "token_info": {"token_id": "new-id", "expiry_time": int(time.time() + 7200) * 1000}
        }
        mock_response_delete = mock.MagicMock()
        mock_response_delete.status_code = 200

        with mock.patch("pat_rotator.requests.post") as mock_post:
            mock_post.side_effect = [mock_response_create, mock_response_delete]
            with mock.patch.object(rotator, "_persist_token"):
                rotator._rotate_once()

        # Second call should be the delete with the OLD token id
        delete_call = mock_post.call_args_list[1]
        assert "token/delete" in delete_call[0][0]
        assert delete_call[1]["json"]["token_id"] == "old-id"

    def test_rotate_fails_gracefully_on_create_error(self):
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com")
        rotator._current_token = "old-pat"
        rotator._current_token_id = "old-id"

        mock_response = mock.MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        with mock.patch("pat_rotator.requests.post", return_value=mock_response):
            result = rotator._rotate_once()

        assert result is False
        assert rotator._current_token == "old-pat"  # Unchanged

    def test_rotate_continues_if_revoke_fails(self):
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com")
        rotator._current_token = "old-pat"
        rotator._current_token_id = "old-id"

        mock_create = mock.MagicMock()
        mock_create.status_code = 200
        mock_create.json.return_value = {
            "token_value": "new-pat",
            "token_info": {"token_id": "new-id", "expiry_time": int(time.time() + 7200) * 1000}
        }
        mock_delete = mock.MagicMock()
        mock_delete.status_code = 500

        with mock.patch("pat_rotator.requests.post") as mock_post:
            mock_post.side_effect = [mock_create, mock_delete]
            with mock.patch.object(rotator, "_persist_token"):
                result = rotator._rotate_once()

        # New token should still be active even if old revocation failed
        assert result is True
        assert rotator._current_token == "new-pat"


class TestTokenPersistence:
    """Writing token to ~/.databrickscfg."""

    def test_writes_databrickscfg(self, tmp_path):
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com")
        rotator._databrickscfg_path = str(tmp_path / ".databrickscfg")
        rotator._write_databrickscfg("test-token")

        content = (tmp_path / ".databrickscfg").read_text()
        assert "test-token" in content
        assert "https://test.databricks.com" in content

    def test_databrickscfg_permissions(self, tmp_path):
        import stat
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com")
        rotator._databrickscfg_path = str(tmp_path / ".databrickscfg")
        rotator._write_databrickscfg("test-token")

        mode = os.stat(str(tmp_path / ".databrickscfg")).st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_updates_env_var(self):
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com")
        with mock.patch.object(rotator, "_write_databrickscfg"):
            with mock.patch.object(rotator, "_persist_to_secret"):
                rotator._persist_token("new-token-value")
        assert os.environ.get("DATABRICKS_TOKEN") == "new-token-value"


class TestSecretPersistence:
    """Persisting rotated token to app secret via SP."""

    def test_persist_to_secret_calls_sdk(self):
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com",
                             secret_scope="my-scope", secret_key="DATABRICKS_TOKEN")

        with mock.patch("pat_rotator.WorkspaceClient") as mock_ws:
            rotator._persist_to_secret("new-token")
            mock_ws.return_value.secrets.put_secret.assert_called_once_with(
                scope="my-scope", key="DATABRICKS_TOKEN", string_value="new-token"
            )

    def test_persist_skipped_when_no_scope_configured(self):
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com",
                             secret_scope=None, secret_key=None)

        with mock.patch("pat_rotator.WorkspaceClient") as mock_ws:
            rotator._persist_to_secret("new-token")
            mock_ws.return_value.secrets.put_secret.assert_not_called()


class TestRotatorLifecycle:
    """Start/stop the background thread."""

    def test_start_creates_daemon_thread(self):
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com", rotation_interval=9999)
        rotator._current_token = "test-pat"
        with mock.patch.object(rotator, "_rotation_loop"):
            rotator.start()
            assert rotator._thread is not None
            assert rotator._thread.daemon is True
            rotator.stop()

    def test_no_start_without_token(self):
        from pat_rotator import PATRotator
        rotator = PATRotator(host="https://test.databricks.com")
        rotator._current_token = None
        rotator.start()
        assert rotator._thread is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pat_rotator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pat_rotator'`

**Step 3: Write implementation**

```python
# pat_rotator.py
"""Auto-rotate short-lived PATs in the background.

Mints a new 2-hour PAT every 90 minutes, persists to app secret
(survives restart), writes to ~/.databrickscfg (immediate CLI/SDK use),
and revokes the old PAT. Fixes #81.
"""

import os
import time
import threading
import logging

import requests
from databricks.sdk import WorkspaceClient

from utils import ensure_https

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_TOKEN_LIFETIME = 7200       # 2 hours
DEFAULT_ROTATION_INTERVAL = 5400    # 90 minutes


class PATRotator:
    """Background PAT rotation with secret persistence."""

    def __init__(self, host=None, rotation_interval=DEFAULT_ROTATION_INTERVAL,
                 token_lifetime=DEFAULT_TOKEN_LIFETIME,
                 secret_scope=None, secret_key=None):
        self._host = ensure_https(host or os.environ.get("DATABRICKS_HOST", ""))
        self._rotation_interval = rotation_interval
        self._token_lifetime = token_lifetime
        self._secret_scope = secret_scope
        self._secret_key = secret_key
        self._current_token = os.environ.get("DATABRICKS_TOKEN", "").strip() or None
        self._current_token_id = None
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

    def start(self):
        """Start the background rotation thread."""
        if not self._current_token:
            logger.warning("No PAT configured — rotation thread not started")
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
        """Background loop: sleep, rotate, repeat."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._rotation_interval)
            if self._stop_event.is_set():
                break
            try:
                self._rotate_once()
            except Exception as e:
                logger.error(f"PAT rotation failed unexpectedly: {e}")

    def _rotate_once(self):
        """Mint new PAT, persist, revoke old. Returns True on success."""
        if not self._current_token:
            return False

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

        # 2. Persist new token (secret + file + env)
        with self._lock:
            self._current_token = new_token
            self._current_token_id = new_token_id
        self._persist_token(new_token)
        logger.info(f"PAT rotated successfully (new_id={new_token_id})")

        # 3. Revoke old token (best-effort — old token expires in 2h anyway)
        if old_token_id:
            try:
                resp = requests.post(
                    f"{self._host}/api/2.0/token/delete",
                    headers={"Authorization": f"Bearer {new_token}"},
                    json={"token_id": old_token_id},
                    timeout=30
                )
                if resp.status_code == 200:
                    logger.info(f"Old PAT revoked (id={old_token_id})")
                else:
                    logger.warning(f"Old PAT revocation failed ({resp.status_code})")
            except requests.RequestException as e:
                logger.warning(f"Old PAT revocation request failed: {e}")

        return True

    def _persist_token(self, token):
        """Write rotated token to all persistence layers."""
        os.environ["DATABRICKS_TOKEN"] = token
        self._write_databrickscfg(token)
        self._persist_to_secret(token)

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

    def _persist_to_secret(self, token):
        """Persist token to Databricks app secret (survives restart)."""
        if not self._secret_scope or not self._secret_key:
            return
        try:
            w = WorkspaceClient()
            w.secrets.put_secret(scope=self._secret_scope, key=self._secret_key,
                                string_value=token)
            logger.info("Rotated PAT persisted to app secret")
        except Exception as e:
            logger.warning(f"Could not persist PAT to secret: {e}")
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_pat_rotator.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add pat_rotator.py tests/test_pat_rotator.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: add PATRotator for short-lived token auto-rotation (#81)"
```

---

### Task 2: Integrate PATRotator into app.py

**Files:**
- Modify: `app.py` (initialize_app, ~line 917)

**Step 1: Write failing test**

```python
# tests/test_pat_rotation_integration.py
"""Integration test: PATRotator wired into app."""

from unittest import mock

def test_app_has_pat_rotator():
    with mock.patch("app.initialize_app"):
        import app as app_module
    assert hasattr(app_module, "pat_rotator")
```

**Step 2: Run test — should fail**

Run: `uv run pytest tests/test_pat_rotation_integration.py -v`

**Step 3: Modify app.py**

Add import near top (after existing imports):
```python
from pat_rotator import PATRotator
```

Add module-level instance:
```python
# PAT auto-rotation (short-lived tokens, background refresh)
pat_rotator = PATRotator(
    secret_scope=os.environ.get("PAT_SECRET_SCOPE"),
    secret_key=os.environ.get("PAT_SECRET_KEY", "DATABRICKS_TOKEN"),
)
```

In `initialize_app()`, after the setup thread start, add:
```python
    # Start PAT auto-rotation if a PAT is configured
    pat_rotator.start()
```

**Step 4: Run all tests**

Run: `uv run pytest tests/ -v`

**Step 5: Commit**

```bash
git add app.py tests/test_pat_rotation_integration.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: wire PATRotator into app startup (#81)"
```

---

### Task 3: Update app.yaml with secret resource and rotation env vars

**Files:**
- Modify: `app.yaml`

**Step 1: Update app.yaml**

```yaml
command:
  - gunicorn
  - app:app
env:
  - name: HOME
    value: /app/python/source_code
  - name: DATABRICKS_TOKEN
    valueFrom: DATABRICKS_TOKEN
  - name: PAT_SECRET_SCOPE
    value: coda-app
  - name: PAT_SECRET_KEY
    value: DATABRICKS_TOKEN
  - name: ANTHROPIC_MODEL
    value: databricks-claude-opus-4-6
  - name: GEMINI_MODEL
    value: databricks-gemini-3-1-pro
  - name: CODEX_MODEL
    value: databricks-gpt-5-2
  - name: DATABRICKS_GATEWAY_HOST
    valueFrom: DATABRICKS_GATEWAY_HOST
  - name: CLAUDE_CODE_DISABLE_AUTO_MEMORY
    value: 0
resources:
  - name: pat-token
    secret:
      scope: coda-app
      key: DATABRICKS_TOKEN
      permission: WRITE
```

**Step 2: Commit**

```bash
git add app.yaml
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "chore: add secret resource with WRITE for PAT rotation (#81)"
```

---

### Task 4: Run full test suite and commit plan

**Step 1: Run tests**

Run: `uv run pytest tests/ -v`
Expected: All PASS

**Step 2: Commit plan doc**

```bash
git add docs/plans/2026-03-27-pat-auto-rotation-implementation.md
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "docs: PAT auto-rotation implementation plan (#81)"
```

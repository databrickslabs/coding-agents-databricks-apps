# Omnigent Host Runtime Control Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make CoDA a Databricks App surface for starting, stopping, and observing OSS `omnigent host <server-url>` inside the running app container.

**Architecture:** CoDA owns no Omnigent server semantics. It exposes a small runtime control plane that installs OSS `omnigent`, writes the Databricks Apps OAuth profile, and supervises exactly one `omnigent host <server-url>` process on demand. Host ownership follows the Databricks identity used for the host tunnel; for a pure Databricks App deployment this is the app service principal unless a later user-delegated token path is proven.

**Tech Stack:** Flask API, existing `omnigents_host.py` supervisor, `uv tool install`, Databricks SDK OAuth M2M profile, static HTML/JS UI, pytest.

---

## Non-Goals

- Do not implement an Omnigent server extension.
- Do not add a "connect to CoDA URL" API to Omnigent.
- Do not depend on internal `agent-framework`; use OSS package name and CLI: `omnigent`.
- Do not make host sharing or visibility claims beyond what OSS Omnigent supports.
- Do not bake a server URL into `app.yaml`.

## Product Contract

CoDA should provide one capability:

```bash
omnigent host <omnigent-server-url>
```

from inside the Databricks App container, with Databricks Apps-compatible auth and lifecycle management.

The user-facing UI should say the host will appear in Omnigent under the authenticated tunnel identity. In the current Databricks App implementation, that identity is the app service principal.

## Acceptance Criteria

- `OMNIGENTS_SERVER_URL` is no longer required or used for auto-start.
- A user can enter an Omnigent server URL in the CoDA UI and click Connect.
- CoDA starts a supervised `omnigent host <server-url>` process without redeploying.
- CoDA can disconnect the running host process.
- CoDA status shows: configured server URL, install state, process state, pid, stage, last error, and recent log lines.
- Starting a second host replaces or rejects the existing host deterministically; this plan chooses reject with `409`.
- App startup is unaffected when no host is connected.
- Existing Databricks Apps auth gotchas remain enforced: install includes `databricks-sdk`; host env strips `DATABRICKS_WORKSPACE_ID`, `DATABRICKS_APP_*`, `DATABRICKS_TOKEN`, `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, and `DATABRICKS_CLIENT_SECRET`; `DATABRICKS_CONFIG_PROFILE` drives the M2M profile.
- Tests cover disabled startup, connect, duplicate connect, disconnect, install failure, profile failure, and env stripping.

---

### Task 1: Convert `omnigents_host.py` From Boot Env Auto-Start To Runtime Supervisor

**Files:**
- Modify: `omnigents_host.py`
- Test: `tests/test_omnigents_host.py`

**Step 1: Write failing tests for runtime state**

Add tests that describe the new public API:

```python
def test_status_initially_idle(monkeypatch):
    monkeypatch.delenv("OMNIGENTS_SERVER_URL", raising=False)
    oh.reset_for_tests()
    status = oh.get_status()
    assert status["configured"] is False
    assert status["running"] is False
    assert status["server_url"] is None
    assert status["stage"] == "idle"


def test_connect_requires_server_url(monkeypatch):
    oh.reset_for_tests()
    ok, status = oh.connect_host(" ", sp_creds={"client_id": "c", "client_secret": "s", "host": "https://h"})
    assert ok is False
    assert status["stage"] == "invalid_server_url"


def test_connect_starts_supervisor_thread(monkeypatch):
    oh.reset_for_tests()
    monkeypatch.setattr(oh, "ensure_installed", lambda sp_creds=None: True)
    monkeypatch.setattr(oh, "_write_oauth_profile", lambda creds: None)
    monkeypatch.setattr(oh, "_run_host_once", lambda server_url, stop_event=None: 0)

    started = []

    class FakeThread:
        def __init__(self, target, args, daemon, name):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.name = name

        def start(self):
            started.append(self.name)

    monkeypatch.setattr(oh.threading, "Thread", FakeThread)
    ok, status = oh.connect_host(
        "https://omnigent.example.com",
        sp_creds={"client_id": "c", "client_secret": "s", "host": "https://h"},
    )
    assert ok is True
    assert started == ["omnigent-host"]
    assert status["server_url"] == "https://omnigent.example.com"
    assert status["stage"] == "starting"
```

**Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_omnigents_host.py -q
```

Expected: FAIL because `reset_for_tests` and `connect_host` do not exist and the current status shape is boot-env based.

**Step 3: Implement minimal runtime API**

In `omnigents_host.py`:

- Replace `omnigents_host_enabled()` as the primary control with explicit `connect_host(server_url, sp_creds)`.
- Keep `start_host(sp_creds)` as a backward-compatible no-op unless `OMNIGENTS_SERVER_URL` is set, but mark it legacy in comments.
- Add module state:

```python
_lock = threading.RLock()
_stop_event: threading.Event | None = None
_thread: threading.Thread | None = None
_proc: subprocess.Popen[str] | None = None
_sp_creds: dict[str, str] | None = None
_log_tail: list[str] = []
_LOG_TAIL_LIMIT = 80
```

- Update `_status` to:

```python
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
```

- Add:

```python
def connect_host(server_url: str, sp_creds: dict[str, str] | None) -> tuple[bool, dict[str, object]]:
    ...

def disconnect_host() -> dict[str, object]:
    ...

def reset_for_tests() -> None:
    ...
```

Behavior:

- Empty URL returns `(False, status)` with `stage="invalid_server_url"`.
- Missing SP creds returns `(False, status)` with `stage="no_sp_creds"`.
- Existing live supervisor returns `(False, status)` with `stage` unchanged and `last_error="host already running"`.
- Successful connect stores `_sp_creds`, `_stop_event`, `server_url`, starts the supervisor thread, and returns `(True, status)`.
- `disconnect_host()` sets `_stop_event`, terminates `_proc` if present, and sets `stage="stopped"`, `running=False`, `pid=None`.

**Step 4: Make `_run_host_once` stoppable**

Change:

```python
def _run_host_once(server_url: str) -> int:
```

to:

```python
def _run_host_once(server_url: str, stop_event: threading.Event | None = None) -> int:
```

Store `_proc` under `_lock` immediately after `Popen`. When `stop_event` is set, terminate the process and return its code. Preserve stdout log forwarding and also append sanitized lines to `_log_tail`.

**Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/test_omnigents_host.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add omnigents_host.py tests/test_omnigents_host.py
git commit -m "refactor: make omnigent host runtime-controlled"
```

---

### Task 2: Add Flask Runtime Control Endpoints

**Files:**
- Modify: `app.py`
- Test: `tests/test_omnigents_host_api.py`

**Step 1: Write failing API tests**

Create `tests/test_omnigents_host_api.py`:

```python
from unittest import mock


def _import_app():
    with mock.patch("app.initialize_app"):
        import importlib
        import app
        return importlib.reload(app)


def test_omnigent_host_status_returns_state(monkeypatch):
    app_module = _import_app()
    monkeypatch.setattr("omnigents_host.get_status", lambda: {"stage": "idle", "running": False})

    with app_module.app.test_client() as client:
        with mock.patch.object(app_module, "_is_databricks_apps", return_value=False):
            resp = client.get("/api/omnigent-host/status")

    assert resp.status_code == 200
    assert resp.get_json()["stage"] == "idle"


def test_omnigent_host_connect_requires_url(monkeypatch):
    app_module = _import_app()

    with app_module.app.test_client() as client:
        with mock.patch.object(app_module, "_is_databricks_apps", return_value=False):
            resp = client.post("/api/omnigent-host/connect", json={})

    assert resp.status_code == 400


def test_omnigent_host_connect_calls_supervisor(monkeypatch):
    app_module = _import_app()
    app_module._omnigent_sp_creds = {"client_id": "c", "client_secret": "s", "host": "https://h"}
    called = {}

    def fake_connect(url, sp_creds):
        called["url"] = url
        called["sp_creds"] = sp_creds
        return True, {"stage": "starting", "server_url": url}

    monkeypatch.setattr("omnigents_host.connect_host", fake_connect)

    with app_module.app.test_client() as client:
        with mock.patch.object(app_module, "_is_databricks_apps", return_value=False):
            resp = client.post(
                "/api/omnigent-host/connect",
                json={"server_url": "https://omnigent.example.com"},
            )

    assert resp.status_code == 200
    assert called["url"] == "https://omnigent.example.com"


def test_omnigent_host_connect_conflict(monkeypatch):
    app_module = _import_app()
    app_module._omnigent_sp_creds = {"client_id": "c", "client_secret": "s", "host": "https://h"}
    monkeypatch.setattr(
        "omnigents_host.connect_host",
        lambda url, sp_creds: (False, {"stage": "running", "last_error": "host already running"}),
    )

    with app_module.app.test_client() as client:
        with mock.patch.object(app_module, "_is_databricks_apps", return_value=False):
            resp = client.post(
                "/api/omnigent-host/connect",
                json={"server_url": "https://omnigent.example.com"},
            )

    assert resp.status_code == 409


def test_omnigent_host_disconnect_calls_supervisor(monkeypatch):
    app_module = _import_app()
    monkeypatch.setattr("omnigents_host.disconnect_host", lambda: {"stage": "stopped", "running": False})

    with app_module.app.test_client() as client:
        with mock.patch.object(app_module, "_is_databricks_apps", return_value=False):
            resp = client.post("/api/omnigent-host/disconnect")

    assert resp.status_code == 200
    assert resp.get_json()["stage"] == "stopped"
```

**Step 2: Run test to verify failure**

Run:

```bash
uv run pytest tests/test_omnigents_host_api.py -q
```

Expected: FAIL with 404s for the new routes.

**Step 3: Add endpoints**

In `app.py`:

- Add module global near other app globals:

```python
_omnigent_sp_creds = None
```

- In `initialize_app`, declare it global and assign the captured creds:

```python
global app_owner, _omnigent_sp_creds
...
_omnigent_sp_creds = capture_sp_credentials()
```

- Add routes:

```python
@app.route("/api/omnigent-host/status")
def omnigent_host_status():
    from omnigents_host import get_status
    return jsonify(get_status())


@app.route("/api/omnigent-host/connect", methods=["POST"])
def omnigent_host_connect():
    data = request.get_json(silent=True) or {}
    server_url = (data.get("server_url") or "").strip()
    if not server_url:
        return jsonify({"error": "server_url required"}), 400
    from omnigents_host import connect_host
    ok, status = connect_host(server_url, _omnigent_sp_creds)
    if not ok:
        code = 409 if status.get("last_error") == "host already running" else 400
        return jsonify(status), code
    return jsonify(status)


@app.route("/api/omnigent-host/disconnect", methods=["POST"])
def omnigent_host_disconnect():
    from omnigents_host import disconnect_host
    return jsonify(disconnect_host())
```

- Keep existing `/api/omnigents-status` as a compatibility alias that returns `get_status()`.
- Do not add these endpoints to the unauthenticated skip list; normal CoDA owner auth should protect them.

**Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/test_omnigents_host_api.py tests/test_auth_enforcement.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add app.py tests/test_omnigents_host_api.py
git commit -m "feat: add runtime omnigent host control API"
```

---

### Task 3: Remove Hardcoded Server Auto-Start From `app.yaml`

**Files:**
- Modify: `app.yaml`
- Test: `tests/test_omnigents_host.py`

**Step 1: Write a test that startup does not auto-connect without explicit env**

Update the existing disabled-by-default tests so they assert `start_host` remains inert unless the legacy env is set.

```python
def test_start_host_legacy_noop_without_env(monkeypatch):
    oh.reset_for_tests()
    monkeypatch.delenv("OMNIGENTS_SERVER_URL", raising=False)
    monkeypatch.setattr(oh, "connect_host", _fail("connect_host"))
    oh.start_host({"client_id": "c", "client_secret": "s", "host": "https://h"})
    assert oh.get_status()["stage"] == "idle"
```

**Step 2: Run test to verify current behavior**

Run:

```bash
uv run pytest tests/test_omnigents_host.py::test_start_host_legacy_noop_without_env -q
```

Expected: PASS after Task 1.

**Step 3: Edit `app.yaml`**

Remove the active `OMNIGENTS_SERVER_URL` value. Leave only install source if still required:

```yaml
  # Optional install source for runtime Omnigent host control.
  # The server URL is supplied at runtime through the CoDA UI/API.
  OMNIGENTS_WHEEL_SPEC:
    value: "/Volumes/ot_demo/omnigents/artifacts/wheels"
```

If the live app should not even install unless the user connects, keep `OMNIGENTS_WHEEL_SPEC` only as the install source; do not trigger any work at boot.

**Step 4: Run tests**

Run:

```bash
uv run pytest tests/test_omnigents_host.py tests/test_omnigents_host_api.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add app.yaml tests/test_omnigents_host.py
git commit -m "chore: remove hardcoded omnigent server startup"
```

---

### Task 4: Add Minimal CoDA UI Control

**Files:**
- Modify: `static/index.html`
- Test: manual browser check

**Step 1: Add DOM controls in the toolbar**

In the existing toolbar area, add a compact group:

```html
<div class="group-label">Omnigent Host</div>
<div id="omnigent-host-panel">
  <input id="omnigent-server-url" type="url" placeholder="https://omnigent.example.com">
  <div class="tool-row">
    <button id="omnigent-connect-btn" title="Start omnigent host">Connect</button>
    <button id="omnigent-disconnect-btn" title="Stop omnigent host">Stop</button>
  </div>
  <div id="omnigent-host-status" class="toolbar-status">Idle</div>
</div>
```

Keep styling aligned with current toolbar controls; do not add a landing page or a separate modal.

**Step 2: Add JavaScript helpers**

Add functions near the existing API helper code:

```javascript
async function refreshOmnigentHostStatus() {
  const resp = await fetch('/api/omnigent-host/status');
  const status = await resp.json();
  renderOmnigentHostStatus(status);
  return status;
}

function renderOmnigentHostStatus(status) {
  const el = document.getElementById('omnigent-host-status');
  if (!el) return;
  const stage = status.stage || 'unknown';
  const server = status.server_url ? ` ${status.server_url}` : '';
  el.textContent = `${stage}${server}`;
}

async function connectOmnigentHost() {
  const input = document.getElementById('omnigent-server-url');
  const serverUrl = (input?.value || '').trim();
  if (!serverUrl) {
    renderOmnigentHostStatus({ stage: 'server URL required' });
    return;
  }
  const resp = await fetch('/api/omnigent-host/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ server_url: serverUrl }),
  });
  const status = await resp.json();
  renderOmnigentHostStatus(status);
}

async function disconnectOmnigentHost() {
  const resp = await fetch('/api/omnigent-host/disconnect', { method: 'POST' });
  const status = await resp.json();
  renderOmnigentHostStatus(status);
}
```

Wire the buttons after DOM load:

```javascript
document.getElementById('omnigent-connect-btn')?.addEventListener('click', connectOmnigentHost);
document.getElementById('omnigent-disconnect-btn')?.addEventListener('click', disconnectOmnigentHost);
setInterval(refreshOmnigentHostStatus, 5000);
refreshOmnigentHostStatus().catch(() => {});
```

**Step 3: Manual check locally**

Run:

```bash
uv run python app.py
```

Open the app, confirm the toolbar has the Omnigent Host group, entering a blank URL shows an inline required message, and status polling does not throw console errors.

**Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat: add omnigent host runtime controls"
```

---

### Task 5: Verification On A Databricks App

**Files:**
- No code changes unless verification exposes a bug.

**Step 1: Run local tests**

Run:

```bash
uv run pytest tests/test_omnigents_host.py tests/test_omnigents_host_api.py tests/test_auth_enforcement.py -q
```

Expected: PASS.

**Step 2: Deploy or start a dedicated opt-in Databricks App**

Use the repo's existing deployment workflow. Do not test this on a production/shared app first.

**Step 3: Connect from the UI**

Enter the Omnigent server URL and click Connect.

Expected:

- `/api/omnigent-host/status` reaches `stage="running"` or shows a specific auth/connect error.
- App `/health` remains healthy.
- Logs include `[omnigents-host]` lines.

**Step 4: Verify server-side host ownership**

Query `/v1/hosts` on the Omnigent server with the same identity expected for the host tunnel.

Expected for pure Databricks App mode:

- Host appears when queried as the CoDA app SP.
- Host may not appear when queried as a human user. That is expected unless a user-delegated host-token path is separately implemented and verified.

**Step 5: Recover if the app wedges**

If Databricks Apps gets stuck after repeated deploy/test cycles, recover with:

```bash
databricks apps stop <app-name>
databricks apps start <app-name>
```

Do not redeploy repeatedly as the recovery mechanism.

**Step 6: Commit verification notes**

If verification changes docs or test notes:

```bash
git add docs/plans/2026-06-14-omnigent-host-runtime-control.md
git commit -m "docs: record omnigent host runtime verification"
```

---

## Cleanup Owed Before Or During Verification

- Rotate any PAT pasted in the prior session.
- Tear down `omnigents-daveok` and its Lakebase if unused to avoid metering.

## Live Verification Result

Verified on 2026-06-15 against Databricks App `coda` in profile `daveok`.

- Deployed snapshot `01f1684ca59d1365a836e5923665544a` from `/Workspace/Users/david.okeeffe@databricks.com/apps/coda`; app status `RUNNING`, compute `ACTIVE`.
- `/health` returned healthy after deploy and after host reconnect.
- `/api/omnigent-host/status` and legacy `/api/omnigents-status` returned `stage="idle"`, `configured=false` immediately after deploy, proving no boot-time server URL auto-start.
- `POST /api/omnigent-host/connect` with `https://omnigents-daveok-7405607084296055.15.azure.databricksapps.com` moved through install and reached `stage="running"`, `installed=true`, `host_launched=true`, with pid `1381`.
- CoDA log tail showed OSS `omnigent host` connected as `dbletX4BNFW` with host id `host_3b5efa8f61654d6495b5c53ca34729da`.
- Duplicate connect returned HTTP `409` with `last_error="host already running"`.
- `POST /api/omnigent-host/disconnect` returned `stage="stopped"`, `running=false`, `pid=null`.
- Reconnect returned to `stage="running"` with pid `1435`.
- `omnigents-daveok` server logs showed the tunnel accepted and registered:
  `Host host_3b5efa8f61654d6495b5c53ca34729da connected (version=0.1.0, name=dbletX4BNFW, runners=[])`.
- Querying `/v1/hosts` as human `david.okeeffe@databricks.com` returned only the human-owned host, confirming the expected identity-scoped visibility boundary; the CoDA host is SP-owned.

## Implementation Notes

- The package name and CLI are `omnigent`, not `omnigents`; keep the legacy fallback only for compatibility.
- `omnigent host` on current OSS main takes the server URL; it does not take `--profile`.
- The host tool environment must include `databricks-sdk`.
- The host subprocess must not inherit Databricks Apps ambient auth variables that shadow the intended profile.
- This plan intentionally accepts app-SP host ownership for the Databricks App mode. A human-owned host requires a separate, proven user-delegated token path and is out of scope.

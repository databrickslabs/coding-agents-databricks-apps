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


def test_omnigent_host_connect_requires_url():
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
    assert called["sp_creds"] == app_module._omnigent_sp_creds


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


def test_omnigent_host_share_calls_helper(monkeypatch):
    app_module = _import_app()
    app_module._omnigent_sp_creds = {"client_id": "c", "client_secret": "s", "host": "https://h"}
    monkeypatch.setattr("omnigents_host.get_status", lambda: {"server_url": "https://srv"})
    captured = {}

    def fake_share(server_url, sp_creds, grant_user, launch=True):
        captured.update(server_url=server_url, grant_user=grant_user, launch=launch)
        return {"ok": True, "grant_status": 200, "launch_status": 200}

    monkeypatch.setattr("omnigents_host.share_and_launch", fake_share)

    with app_module.app.test_client() as client:
        with mock.patch.object(app_module, "_is_databricks_apps", return_value=False):
            with mock.patch.dict("os.environ", {"OMNIGENTS_SERVER_URL": ""}):
                resp = client.post(
                    "/api/omnigent-host/share",
                    json={"launch": True},
                    headers={"X-Forwarded-Email": "owner@example.com"},
                )

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert captured["server_url"] == "https://srv"
    assert captured["grant_user"] == "owner@example.com"
    assert captured["launch"] is True


def test_omnigent_host_share_requires_server_url(monkeypatch):
    app_module = _import_app()
    app_module._omnigent_sp_creds = {"client_id": "c", "client_secret": "s", "host": "https://h"}
    monkeypatch.setattr("omnigents_host.get_status", lambda: {"server_url": None})

    with app_module.app.test_client() as client:
        with mock.patch.object(app_module, "_is_databricks_apps", return_value=False):
            with mock.patch.dict("os.environ", {"OMNIGENTS_SERVER_URL": ""}):
                resp = client.post(
                    "/api/omnigent-host/share",
                    json={},
                    headers={"X-Forwarded-Email": "owner@example.com"},
                )

    assert resp.status_code == 400


def test_omnigent_host_share_explicit_grant_user(monkeypatch):
    """An explicit grant_user in the body shares to that user, not the caller."""
    app_module = _import_app()
    app_module._omnigent_sp_creds = {"client_id": "c", "client_secret": "s", "host": "https://h"}
    monkeypatch.setattr("omnigents_host.get_status", lambda: {"server_url": "https://srv"})
    captured = {}

    def fake_share(server_url, sp_creds, grant_user, launch=True):
        captured.update(server_url=server_url, grant_user=grant_user, launch=launch)
        return {"ok": True, "grant_status": 200}

    monkeypatch.setattr("omnigents_host.share_and_launch", fake_share)

    with app_module.app.test_client() as client:
        with mock.patch.object(app_module, "_is_databricks_apps", return_value=False):
            with mock.patch.dict("os.environ", {"OMNIGENTS_SERVER_URL": ""}):
                resp = client.post(
                    "/api/omnigent-host/share",
                    json={"grant_user": "teammate@example.com", "launch": False},
                    headers={"X-Forwarded-Email": "owner@example.com"},
                )

    assert resp.status_code == 200
    assert captured["grant_user"] == "teammate@example.com"
    assert captured["launch"] is False


# ---------------------------------------------------------------------------
# Owner gating on the host-share endpoint
# ---------------------------------------------------------------------------
#
# This endpoint acts with the app SP's authority: it grants another identity
# `use` on an SP-owned Omnigent host. It is NOT covered by _owner_check_disabled
# (the shared-app opt-out only opens the terminal + WebSocket), and app.yaml has
# always promised "configure-pat and omnigent-host/share stay owner-only".
#
# It previously gated itself with `if _is_databricks_apps() and app_owner:`,
# which short-circuits to *allow* when app_owner is None — so a transient
# Apps-API failure at boot left it ungated. Unlike configure-pat there is no
# bootstrap justification here, so it must fail CLOSED.


def _share_request(app_module, *, owner, caller):
    original = app_module.app_owner
    try:
        app_module.app_owner = owner
        with app_module.app.test_client() as client:
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                return client.post(
                    "/api/omnigent-host/share",
                    json={},
                    headers={"X-Forwarded-Email": caller} if caller else {},
                )
    finally:
        app_module.app_owner = original


def test_host_share_denied_when_owner_unresolved():
    """Must fail closed, not fall through to acting with SP authority."""
    app_module = _import_app()

    resp = _share_request(app_module, owner=None, caller="anyone@example.com")

    assert resp.status_code == 403, (
        f"host-share must deny when app_owner is unresolved, got {resp.status_code}"
    )


def test_host_share_denied_for_non_owner():
    app_module = _import_app()

    resp = _share_request(
        app_module, owner="owner@example.com", caller="intruder@example.com"
    )

    assert resp.status_code == 403


def test_host_share_denied_when_owner_unresolved_even_in_shared_mode(monkeypatch):
    """The shared-app opt-out must not widen this endpoint."""
    monkeypatch.setenv("CODA_DISABLE_OWNER_CHECK", "true")
    app_module = _import_app()

    resp = _share_request(app_module, owner=None, caller="anyone@example.com")

    assert resp.status_code == 403, (
        "CODA_DISABLE_OWNER_CHECK only opens the terminal + WebSocket; owner-only "
        f"write endpoints must stay gated, got {resp.status_code}"
    )


def test_host_share_allowed_for_owner_reaches_handler():
    """Sanity check that the gate isn't simply denying everything."""
    app_module = _import_app()

    resp = _share_request(
        app_module, owner="owner@example.com", caller="owner@example.com"
    )

    # No server URL configured, so the handler itself rejects with 400 — the
    # point is that it got past the owner gate rather than 403-ing.
    assert resp.status_code != 403

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

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

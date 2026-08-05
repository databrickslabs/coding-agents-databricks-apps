"""Integration test: PATRotator wired into app."""

import os
from unittest import mock


class TestPATRotatorIntegration:

    def test_app_has_pat_rotator(self):
        with mock.patch("app.initialize_app"):
            import app as app_module
        assert hasattr(app_module, "pat_rotator")

    def test_pat_rotator_is_correct_type(self):
        with mock.patch("app.initialize_app"):
            import app as app_module
        from pat_rotator import PATRotator
        assert isinstance(app_module.pat_rotator, PATRotator)


class TestPATStatusEndpoint:
    def test_pat_status_no_token(self):
        with mock.patch("app.initialize_app"):
            import app as app_module
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()

        original = os.environ.pop("DATABRICKS_TOKEN", None)
        try:
            resp = client.get("/api/pat-status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["configured"] is False
            assert data["valid"] is False
        finally:
            if original:
                os.environ["DATABRICKS_TOKEN"] = original

    def test_sp_apikeyhelper_is_valid_without_pat(self):
        with mock.patch("app.initialize_app"):
            import app as app_module
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()

        original_token = os.environ.pop("DATABRICKS_TOKEN", None)
        original_creds = app_module._omnigent_sp_creds
        original_owner = app_module.app_owner
        try:
            os.environ["ENABLE_SP_APIKEYHELPER"] = "true"
            app_module._omnigent_sp_creds = {"client_id": "app-sp"}
            app_module.app_owner = "owner@example.com"
            resp = client.get("/api/pat-status")
            assert resp.get_json() == {
                "auth_mode": "sp_oauth",
                "configured": True,
                "valid": True,
                "user": "owner@example.com",
            }
        finally:
            os.environ.pop("ENABLE_SP_APIKEYHELPER", None)
            app_module._omnigent_sp_creds = original_creds
            app_module.app_owner = original_owner
            if original_token:
                os.environ["DATABRICKS_TOKEN"] = original_token

    def test_configure_pat_empty_token(self):
        with mock.patch("app.initialize_app"):
            import app as app_module
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()

        resp = client.post("/api/configure-pat", json={"token": ""})
        assert resp.status_code == 400


class TestPATStatusAccessible:
    def test_pat_status_skips_auth(self):
        """pat-status endpoint should be accessible without auth."""
        with mock.patch("app.initialize_app"):
            import app as app_module
            app_module.app.config["TESTING"] = True
            app_module.app_owner = "owner@example.com"
            client = app_module.app.test_client()

        resp = client.get("/api/pat-status")
        assert resp.status_code == 200  # not 403

    def test_configure_pat_skips_auth(self):
        """configure-pat endpoint should be accessible without auth."""
        with mock.patch("app.initialize_app"):
            import app as app_module
            app_module.app.config["TESTING"] = True
            app_module.app_owner = "owner@example.com"
            client = app_module.app.test_client()

        # Should get 400 (bad request) not 403 (unauthorized)
        resp = client.post("/api/configure-pat", json={"token": ""})
        assert resp.status_code == 400


class TestInjectPATEndpoint:
    """Programmatic PAT injection for multi-CoDA provisioning."""

    def _client(self):
        with mock.patch("app.initialize_app"):
            import app as app_module
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()
        return app_module, client

    def test_disabled_without_secret(self):
        """No CODA_BOOTSTRAP_SECRET => endpoint behaves as 404 (opt-in only)."""
        app_module, client = self._client()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODA_BOOTSTRAP_SECRET", None)
            resp = client.post("/api/inject-pat", json={"token": "dapiX"})
        assert resp.status_code == 404

    def test_rejects_bad_secret(self):
        app_module, client = self._client()
        with mock.patch.dict(os.environ, {"CODA_BOOTSTRAP_SECRET": "s3cr3t"}, clear=False):
            resp = client.post(
                "/api/inject-pat",
                json={"token": "dapiX"},
                headers={"X-Coda-Bootstrap-Secret": "wrong"},
            )
        assert resp.status_code == 403

    def test_accepts_good_secret_and_bootstraps(self):
        app_module, client = self._client()
        # Ensure single-shot guard doesn't short-circuit.
        app_module.pat_rotator._current_token = None
        with mock.patch.dict(os.environ, {"CODA_BOOTSTRAP_SECRET": "s3cr3t"}, clear=False):
            with mock.patch.object(app_module, "_bootstrap_pat",
                                   return_value=(True, {"status": "ok", "user": "u",
                                                        "instance": "coda-1"}, 200)) as mb:
                resp = client.post(
                    "/api/inject-pat",
                    json={"token": "dapiGOOD"},
                    headers={"Authorization": "Bearer s3cr3t"},
                )
        assert resp.status_code == 200
        mb.assert_called_once_with("dapiGOOD")
        assert resp.get_json()["instance"] == "coda-1"

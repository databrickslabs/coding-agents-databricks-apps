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

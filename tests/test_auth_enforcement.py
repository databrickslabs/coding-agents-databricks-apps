"""Tests for HTTP authorization enforcement on session endpoints.

Regression test: /api/sessions and /api/session/attach were incorrectly
exempted from the before_request authorization check, allowing any
Databricks user to list sessions and read terminal output.

Also verifies case-insensitive email matching across all auth paths.
"""

from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_app_module():
    """Import app module with initialize_app mocked out."""
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
        return app_module


def _make_client(app_module):
    return app_module.app.test_client()


# ---------------------------------------------------------------------------
# 1. Session endpoints MUST enforce owner check
# ---------------------------------------------------------------------------

class TestSessionEndpointAuth:
    """All session/terminal endpoints must deny non-owners on Databricks Apps."""

    # -- Helper to run deny/allow checks on any endpoint --

    def _assert_denied(self, method, path, json_body=None):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            client = _make_client(app_module)
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                if method == "GET":
                    resp = client.get(path, headers={"X-Forwarded-Email": "intruder@evil.com"})
                else:
                    resp = client.post(path, json=json_body or {},
                                       headers={"X-Forwarded-Email": "intruder@evil.com"})
            assert resp.status_code == 403, (
                f"{method} {path} should return 403 for non-owner, got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original_owner

    def _assert_not_denied(self, method, path, json_body=None):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            client = _make_client(app_module)
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                if method == "GET":
                    resp = client.get(path, headers={"X-Forwarded-Email": "owner@databricks.com"})
                else:
                    resp = client.post(path, json=json_body or {},
                                       headers={"X-Forwarded-Email": "owner@databricks.com"})
            assert resp.status_code != 403, (
                f"{method} {path} should not return 403 for owner, got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original_owner

    # -- GET /api/sessions (list) --

    def test_list_sessions_denied_for_non_owner(self):
        self._assert_denied("GET", "/api/sessions")

    def test_list_sessions_allowed_for_owner(self):
        self._assert_not_denied("GET", "/api/sessions")

    # -- POST /api/session/attach --

    def test_attach_session_denied_for_non_owner(self):
        self._assert_denied("POST", "/api/session/attach", {"session_id": "fake"})

    def test_attach_session_allowed_for_owner(self):
        self._assert_not_denied("POST", "/api/session/attach", {"session_id": "nonexistent"})

    # -- POST /api/session (create) --

    def test_create_session_denied_for_non_owner(self):
        self._assert_denied("POST", "/api/session", {"label": "test"})

    def test_create_session_allowed_for_owner(self):
        self._assert_not_denied("POST", "/api/session", {"label": "test"})

    # -- POST /api/session/close --

    def test_close_session_denied_for_non_owner(self):
        self._assert_denied("POST", "/api/session/close", {"session_id": "fake"})

    def test_close_session_allowed_for_owner(self):
        self._assert_not_denied("POST", "/api/session/close", {"session_id": "nonexistent"})

    # -- POST /api/resize --

    def test_resize_denied_for_non_owner(self):
        self._assert_denied("POST", "/api/resize", {"session_id": "fake", "cols": 80, "rows": 24})

    def test_resize_allowed_for_owner(self):
        self._assert_not_denied("POST", "/api/resize", {"session_id": "fake", "cols": 80, "rows": 24})


# ---------------------------------------------------------------------------
# 2. Case-insensitive email matching
# ---------------------------------------------------------------------------

class TestCaseInsensitiveAuth:
    """Owner check must be case-insensitive for SSO header casing differences."""

    @pytest.mark.parametrize("header_email", [
        "Owner@Databricks.COM",
        "OWNER@DATABRICKS.COM",
        "oWnEr@dAtAbRiCkS.cOm",
    ], ids=["mixed-case", "all-caps", "alternating-case"])
    def test_http_auth_case_insensitive(self, header_email):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            with app_module.app.test_request_context(
                headers={"X-Forwarded-Email": header_email}
            ):
                authorized, user = app_module.check_authorization()
                assert authorized is True, (
                    f"HTTP auth should allow '{header_email}' matching owner "
                    f"'owner@databricks.com' (case-insensitive)"
                )
        finally:
            app_module.app_owner = original_owner

    @pytest.mark.parametrize("header_email", [
        "Owner@Databricks.COM",
        "OWNER@DATABRICKS.COM",
        "oWnEr@dAtAbRiCkS.cOm",
    ], ids=["mixed-case", "all-caps", "alternating-case"])
    def test_ws_auth_case_insensitive(self, header_email):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            with app_module.app.test_request_context(
                headers={"X-Forwarded-Email": header_email}
            ):
                result = app_module._check_ws_authorization()
                assert result is True, (
                    f"WS auth should allow '{header_email}' matching owner "
                    f"'owner@databricks.com' (case-insensitive)"
                )
        finally:
            app_module.app_owner = original_owner

    def test_get_request_user_lowercases(self):
        app_module = _get_app_module()
        with app_module.app.test_request_context(
            headers={"X-Forwarded-Email": "User@EXAMPLE.Com"}
        ):
            result = app_module.get_request_user()
            assert result == "user@example.com", (
                f"get_request_user() should lowercase, got '{result}'"
            )

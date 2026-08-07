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
def test_connect_endpoint_requires_allowlisted_server_sp(monkeypatch):
    """The M2M host-connect route accepts only the configured server SP."""
    import base64
    import json

    app_module = _get_app_module()
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "server-sp"}).encode()
    ).decode().rstrip("=")
    token = f"header.{payload}.signature"
    monkeypatch.setenv("OMNIGENT_SERVER_SP_CLIENT_ID", "server-sp")

    with app_module.app.test_request_context(
        headers={"X-Forwarded-Access-Token": token}
    ):
        assert app_module._omnigent_server_request_authorized() is True

    monkeypatch.setenv("OMNIGENT_SERVER_SP_CLIENT_ID", "other-sp")
    with app_module.app.test_request_context(
        headers={"X-Forwarded-Access-Token": token}
    ):
        assert app_module._omnigent_server_request_authorized() is False


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

    # -- GET /api/app-state --
    # Historically auth-exempt; removed because it leaks app_owner email +
    # PAT rotation timing to unauthenticated callers (review finding E-4).

    def test_app_state_denied_for_non_owner(self):
        self._assert_denied("GET", "/api/app-state")

    def test_app_state_allowed_for_owner(self):
        self._assert_not_denied("GET", "/api/app-state")


# ---------------------------------------------------------------------------
# 1b. /api/configure-pat MUST enforce owner check (hotfix)
# ---------------------------------------------------------------------------

class TestConfigurePatAuth:
    """The PAT bootstrap endpoint is auth-exempt at the before_request gate
    (intentionally — needed before the owner has set up). It MUST still gate
    on owner once app_owner is resolved, otherwise any workspace-SSO'd user
    can submit their own PAT and persistently impersonate the owner.
    """

    def test_configure_pat_denied_for_non_owner(self):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            client = _make_client(app_module)
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                resp = client.post(
                    "/api/configure-pat",
                    json={"token": "dapi-attacker"},
                    headers={"X-Forwarded-Email": "intruder@evil.com"},
                )
            assert resp.status_code == 403, (
                f"POST /api/configure-pat should return 403 for non-owner, got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original_owner

    def test_configure_pat_allowed_for_owner(self):
        """Owner can still bootstrap. We don't run the rotator, just confirm
        the auth guard doesn't return 403 — actual token validation is mocked
        out separately and not in scope here."""
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            client = _make_client(app_module)
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                # Don't pass a token — we expect 400 ("Token required"), which
                # proves the request got past the owner check.
                resp = client.post(
                    "/api/configure-pat",
                    json={},
                    headers={"X-Forwarded-Email": "owner@databricks.com"},
                )
            assert resp.status_code != 403, (
                f"POST /api/configure-pat should not return 403 for owner, got {resp.status_code}"
            )
            assert resp.status_code == 400, (
                f"Owner with empty body should get 400 'Token required', got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original_owner

    def test_configure_pat_bootstrap_window_allowed(self):
        """During the brief window where app_owner hasn't been resolved yet
        (e.g., Apps API hiccup at startup), the endpoint must still accept
        the request — otherwise the owner can never finish bootstrap.
        This is intentional and documented in the in-handler comment."""
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = None  # unresolved
            client = _make_client(app_module)
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                resp = client.post(
                    "/api/configure-pat",
                    json={},
                    headers={"X-Forwarded-Email": "anyone@databricks.com"},
                )
            # Should NOT be 403 — should fall through to "Token required" (400)
            assert resp.status_code != 403, (
                f"During bootstrap (app_owner unresolved), configure-pat should not 403, got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original_owner

    def test_configure_pat_case_insensitive_for_owner(self):
        """Owner email casing from the SSO header must match the lowercased
        app_owner — same case-insensitive contract as the rest of auth."""
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            client = _make_client(app_module)
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                resp = client.post(
                    "/api/configure-pat",
                    json={},
                    headers={"X-Forwarded-Email": "Owner@Databricks.COM"},
                )
            assert resp.status_code != 403, (
                f"Mixed-case owner header should be accepted, got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original_owner

    # ---- Idempotency: defence-in-depth against XSS/session-hijack ----

    def test_configure_pat_rejected_when_already_configured(self):
        """When the rotator is alive with a fresh token, re-submission must
        be refused with 409 — even from the legitimate owner. Defence-in-depth
        against an attacker driving the owner's browser to swap PATs."""
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            client = _make_client(app_module)
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True), \
                 mock.patch.object(type(app_module.pat_rotator), "token",
                                    new_callable=mock.PropertyMock, return_value="dapi-active"), \
                 mock.patch.object(type(app_module.pat_rotator), "is_token_expired",
                                    new_callable=mock.PropertyMock, return_value=False):
                resp = client.post(
                    "/api/configure-pat",
                    json={"token": "dapi-attempt-swap"},
                    headers={"X-Forwarded-Email": "owner@databricks.com"},
                )
            assert resp.status_code == 409, (
                f"Re-configure when rotator active should 409, got {resp.status_code}"
            )
            body = resp.get_json()
            assert "already configured" in body.get("error", "").lower(), (
                f"Error message should mention 'already configured', got: {body}"
            )
        finally:
            app_module.app_owner = original_owner

    def test_configure_pat_allowed_when_token_expired(self):
        """Expired-token escape hatch: if the rotator timed out (long idle),
        the owner must be able to re-bootstrap without restarting the app."""
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            client = _make_client(app_module)
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True), \
                 mock.patch.object(type(app_module.pat_rotator), "token",
                                    new_callable=mock.PropertyMock, return_value="dapi-stale"), \
                 mock.patch.object(type(app_module.pat_rotator), "is_token_expired",
                                    new_callable=mock.PropertyMock, return_value=True):
                # Empty body → should reach "Token required" (400), proving
                # we passed BOTH the owner gate and the idempotency check.
                resp = client.post(
                    "/api/configure-pat",
                    json={},
                    headers={"X-Forwarded-Email": "owner@databricks.com"},
                )
            assert resp.status_code == 400, (
                f"Expired-PAT path should reach token-required check (400), got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original_owner


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


# ---------------------------------------------------------------------------
# 3. Info-disclosure endpoints — auth-gated or trimmed
# ---------------------------------------------------------------------------

class TestInfoDisclosureEndpoints:
    """These endpoints were previously reachable without auth and leaked
    info about the app's state. They are now either owner-gated (setup-status,
    pat-status, app-state) or minimally informative (health).
    """

    def _post_or_get(self, app_module, method, path, headers):
        client = _make_client(app_module)
        with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
            if method == "GET":
                return client.get(path, headers=headers)
            return client.post(path, headers=headers)

    # -- /api/setup-status now requires auth --

    def test_setup_status_denied_for_non_owner(self):
        app_module = _get_app_module()
        original = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            resp = self._post_or_get(app_module, "GET", "/api/setup-status",
                                     {"X-Forwarded-Email": "intruder@evil.com"})
            assert resp.status_code == 403, (
                f"GET /api/setup-status should 403 for non-owner, got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original

    def test_setup_status_allowed_for_owner(self):
        app_module = _get_app_module()
        original = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            resp = self._post_or_get(app_module, "GET", "/api/setup-status",
                                     {"X-Forwarded-Email": "owner@databricks.com"})
            assert resp.status_code == 200, (
                f"Owner should see setup-status, got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original

    # -- /api/pat-status now requires auth --

    def test_pat_status_denied_for_non_owner(self):
        app_module = _get_app_module()
        original = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            resp = self._post_or_get(app_module, "GET", "/api/pat-status",
                                     {"X-Forwarded-Email": "intruder@evil.com"})
            assert resp.status_code == 403, (
                f"GET /api/pat-status should 403 for non-owner, got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original

    # -- /api/app-state now requires auth --

    def test_app_state_denied_for_non_owner(self):
        app_module = _get_app_module()
        original = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            resp = self._post_or_get(app_module, "GET", "/api/app-state",
                                     {"X-Forwarded-Email": "intruder@evil.com"})
            assert resp.status_code == 403, (
                f"GET /api/app-state should 403 for non-owner, got {resp.status_code}"
            )
        finally:
            app_module.app_owner = original

    # -- /health stays reachable unauth, but only the owner sees detail --

    def test_health_minimal_response_for_unauthenticated_caller(self):
        """/health stays exempt from the SSO gate so the platform can probe it,
        but an unauthenticated caller must NOT see version, session counts,
        setup state or rotator internals — those enable version-targeted
        exploit selection and leak the app's auth posture."""
        app_module = _get_app_module()
        client = _make_client(app_module)
        original = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            # On Apps with no X-Forwarded-Email, check_authorization() fails closed.
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                resp = client.get("/health")
        finally:
            app_module.app_owner = original

        # Still 200 — the gate must not turn a liveness probe into a 403.
        assert resp.status_code == 200
        body = resp.get_json()
        assert set(body) == {"status"}, (
            f"unauth /health should return only status, got keys: {sorted(body)}"
        )
        assert body["status"] in ("healthy", "degraded")
        # Explicit anti-leak assertions
        assert "version" not in body, "/health must not expose version"
        assert "setup_status" not in body, "/health must not expose setup_status"
        assert "active_sessions" not in body, "/health must not expose session count"
        assert "auth" not in body, "/health must not expose rotator internals"

    def test_health_full_payload_for_owner(self):
        """The owner keeps the diagnostic payload — that's what makes a zombie
        app (worker answering while PAT rotation is dead) observable."""
        app_module = _get_app_module()
        client = _make_client(app_module)
        original = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                resp = client.get(
                    "/health", headers={"X-Forwarded-Email": "owner@databricks.com"}
                )
        finally:
            app_module.app_owner = original

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] in ("healthy", "degraded")
        for key in ("version", "setup_status", "active_sessions", "auth"):
            assert key in body, f"owner /health should include {key}, got {sorted(body)}"

    def test_health_reports_degraded_to_unauthenticated_caller(self):
        """`status` is the one field everyone sees — a liveness probe that can't
        report unhealthiness is useless. Verify a dead rotator still surfaces."""
        app_module = _get_app_module()
        client = _make_client(app_module)
        original = app_module.app_owner
        rotator = app_module.pat_rotator
        try:
            app_module.app_owner = "owner@databricks.com"
            with mock.patch.object(type(rotator), "token", "dapi-fake"), \
                 mock.patch.object(type(rotator), "is_alive", False), \
                 mock.patch.object(type(rotator), "is_token_expired", True), \
                 mock.patch.object(type(rotator), "seconds_since_rotation", 999), \
                 mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                resp = client.get("/health")
        finally:
            app_module.app_owner = original

        body = resp.get_json()
        assert body == {"status": "degraded"}, (
            f"expected a bare degraded signal, got {body}"
        )

"""Tests for WebSocket authorization parity with HTTP check_authorization().

Verifies that _check_ws_authorization() mirrors the fail-closed behavior of
check_authorization() on Databricks Apps: deny when app_owner is None, deny
when no user headers are present, and allow only the owner.
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


# ---------------------------------------------------------------------------
# 1. Fail-closed when app_owner is None on Databricks Apps
# ---------------------------------------------------------------------------

class TestWsAuthOwnerNone:
    """When app_owner is None, WS should deny on Databricks Apps, allow on local."""

    def test_deny_ws_when_owner_none_on_databricks_apps(self):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = None
            with app_module.app.test_request_context(
                headers={"X-Forwarded-Email": "user@databricks.com"}
            ):
                with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                    result = app_module._check_ws_authorization()
                    assert result is False, "WS should deny when app_owner is None on Databricks Apps"
        finally:
            app_module.app_owner = original_owner

    def test_allow_ws_when_owner_none_on_local_dev(self):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = None
            with app_module.app.test_request_context():
                with mock.patch.object(app_module, "_is_databricks_apps", return_value=False):
                    result = app_module._check_ws_authorization()
                    assert result is True, "WS should allow when app_owner is None on local dev"
        finally:
            app_module.app_owner = original_owner


# ---------------------------------------------------------------------------
# 2. Fail-closed when no user headers on Databricks Apps
# ---------------------------------------------------------------------------

class TestWsAuthNoHeaders:
    """When no identity headers present, WS should deny on Databricks Apps."""

    def test_deny_ws_when_no_headers_on_databricks_apps(self):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            # Request with NO identity headers
            with app_module.app.test_request_context():
                with mock.patch.object(app_module, "_is_databricks_apps", return_value=True):
                    result = app_module._check_ws_authorization()
                    assert result is False, "WS should deny when no user headers on Databricks Apps"
        finally:
            app_module.app_owner = original_owner

    def test_allow_ws_when_no_headers_on_local_dev(self):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            with app_module.app.test_request_context():
                with mock.patch.object(app_module, "_is_databricks_apps", return_value=False):
                    result = app_module._check_ws_authorization()
                    assert result is True, "WS should allow when no headers on local dev"
        finally:
            app_module.app_owner = original_owner


# ---------------------------------------------------------------------------
# 3. Owner match / mismatch
# ---------------------------------------------------------------------------

class TestWsAuthOwnerCheck:
    """When app_owner is set and headers are present, check identity."""

    def test_allow_ws_when_owner_matches(self):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            with app_module.app.test_request_context(
                headers={"X-Forwarded-Email": "owner@databricks.com"}
            ):
                result = app_module._check_ws_authorization()
                assert result is True, "WS should allow when user matches owner"
        finally:
            app_module.app_owner = original_owner

    def test_deny_ws_when_owner_mismatch(self):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            with app_module.app.test_request_context(
                headers={"X-Forwarded-Email": "intruder@evil.com"}
            ):
                result = app_module._check_ws_authorization()
                assert result is False, "WS should deny when user does not match owner"
        finally:
            app_module.app_owner = original_owner

    @pytest.mark.parametrize("header_name", [
        "X-Forwarded-Email",
        "X-Forwarded-User",
        "X-Databricks-User-Email",
    ])
    def test_allow_ws_with_each_identity_header(self, header_name):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = "owner@databricks.com"
            with app_module.app.test_request_context(
                headers={header_name: "owner@databricks.com"}
            ):
                result = app_module._check_ws_authorization()
                assert result is True, f"WS should accept identity from {header_name}"
        finally:
            app_module.app_owner = original_owner


# ---------------------------------------------------------------------------
# 4. Parity with HTTP check_authorization
# ---------------------------------------------------------------------------

class TestWsHttpParity:
    """WS authorization should produce the same allow/deny as HTTP for all cases."""

    @pytest.mark.parametrize("owner,is_dbapps,headers,expected", [
        # app_owner=None, Databricks Apps → DENY
        (None, True, {}, False),
        (None, True, {"X-Forwarded-Email": "anyone@test.com"}, False),
        # app_owner=None, local dev → ALLOW
        (None, False, {}, True),
        # app_owner set, no headers, Databricks Apps → DENY
        ("owner@db.com", True, {}, False),
        # app_owner set, no headers, local dev → ALLOW
        ("owner@db.com", False, {}, True),
        # app_owner set, matching user → ALLOW
        ("owner@db.com", True, {"X-Forwarded-Email": "owner@db.com"}, True),
        # app_owner set, mismatched user → DENY
        ("owner@db.com", True, {"X-Forwarded-Email": "hacker@bad.com"}, False),
    ], ids=[
        "no-owner-dbapps-deny",
        "no-owner-dbapps-with-headers-deny",
        "no-owner-local-allow",
        "owner-no-headers-dbapps-deny",
        "owner-no-headers-local-allow",
        "owner-match-allow",
        "owner-mismatch-deny",
    ])
    def test_ws_auth_parity(self, owner, is_dbapps, headers, expected):
        app_module = _get_app_module()
        original_owner = app_module.app_owner
        try:
            app_module.app_owner = owner
            with app_module.app.test_request_context(headers=headers):
                with mock.patch.object(app_module, "_is_databricks_apps", return_value=is_dbapps):
                    result = app_module._check_ws_authorization()
                    assert result is expected
        finally:
            app_module.app_owner = original_owner

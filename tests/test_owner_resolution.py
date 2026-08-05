"""Tests for app-owner resolution.

Why this matters more than it looks: `check_authorization()` fails **closed** on
Databricks Apps when `app_owner` is unresolved. So a transient Apps-API failure
during `initialize_app()` doesn't degrade to "authorization disabled" — it makes
the app unusable for everyone until someone restarts it.

Three properties are covered here:

1. `APP_OWNER_EMAIL` short-circuits the API entirely (deterministic escape hatch).
2. The Apps API path prefers the spawner's `owner:{email}` description over
   `app.creator`, because when one identity creates apps for other people the
   creator is the spawner, not the intended owner.
3. Boot-time retry is *bounded* — `initialize_app()` runs before gunicorn binds
   the port, so a long backoff there stalls the whole app's startup. Unbounded
   recovery happens on a background thread instead.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def app_module():
    import app

    return app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("APP_OWNER_EMAIL", raising=False)
    monkeypatch.setenv("DATABRICKS_APP_NAME", "coda-test")
    # Keep the PAT fallback from firing and making real calls.
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)


class TestExplicitOwner:
    def test_app_owner_email_wins_without_any_api_call(self, app_module, monkeypatch):
        monkeypatch.setenv("APP_OWNER_EMAIL", "Owner@Example.COM")

        with mock.patch.object(app_module, "_owner_from_apps_api") as api:
            assert app_module.get_token_owner() == "owner@example.com"

        api.assert_not_called()

    def test_blank_app_owner_email_is_ignored(self, app_module, monkeypatch):
        monkeypatch.setenv("APP_OWNER_EMAIL", "   ")

        with mock.patch.object(
            app_module, "_owner_from_apps_api", return_value="creator@example.com"
        ):
            assert app_module.get_token_owner() == "creator@example.com"


class TestAppsApiResolution:
    def _client(self, *, creator=None, description=None):
        app_info = SimpleNamespace(creator=creator, description=description)
        return SimpleNamespace(apps=SimpleNamespace(get=lambda name: app_info))

    def _patch_sdk(self, app_module, client):
        # get_token_owner imports WorkspaceClient lazily from databricks.sdk.
        return mock.patch("databricks.sdk.WorkspaceClient", return_value=client)

    def test_spawner_description_takes_precedence_over_creator(self, app_module):
        client = self._client(
            creator="spawner-sp@example.com", description="owner:Real.User@example.com"
        )
        with self._patch_sdk(app_module, client), \
             mock.patch.object(app_module, "set_product_info"):
            assert app_module._owner_from_apps_api() == "real.user@example.com"

    def test_falls_back_to_creator(self, app_module):
        client = self._client(creator="Creator@Example.com", description="some app")
        with self._patch_sdk(app_module, client), \
             mock.patch.object(app_module, "set_product_info"):
            assert app_module._owner_from_apps_api() == "creator@example.com"

    def test_empty_owner_description_falls_through_to_creator(self, app_module):
        client = self._client(creator="creator@example.com", description="owner:")
        with self._patch_sdk(app_module, client), \
             mock.patch.object(app_module, "set_product_info"):
            assert app_module._owner_from_apps_api() == "creator@example.com"

    def test_returns_none_when_nothing_identifies_an_owner(self, app_module):
        client = self._client(creator=None, description=None)
        with self._patch_sdk(app_module, client), \
             mock.patch.object(app_module, "set_product_info"):
            assert app_module._owner_from_apps_api() is None

    def test_no_app_name_means_no_api_attempt(self, app_module, monkeypatch):
        monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
        assert app_module._owner_from_apps_api() is None


class TestBootRetryIsBounded:
    def test_retries_then_gives_up_without_blocking_boot(self, app_module):
        """A flaky API must not turn the port bind into a multi-minute wait."""
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("SP credentials not ready")

        with mock.patch.object(app_module, "_owner_from_apps_api", side_effect=boom), \
             mock.patch.object(app_module.time, "sleep") as sleep:
            result = app_module.get_token_owner()

        assert result is None
        assert len(calls) == app_module._OWNER_BOOT_ATTEMPTS
        # One fewer sleep than attempts — no pointless wait after the last try.
        assert sleep.call_count == app_module._OWNER_BOOT_ATTEMPTS - 1
        total_boot_delay = sum(c.args[0] for c in sleep.call_args_list)
        assert total_boot_delay <= 30, (
            f"boot-time owner resolution would stall gunicorn for "
            f"{total_boot_delay}s before binding the port"
        )

    def test_transient_failure_then_success(self, app_module):
        attempts = [RuntimeError("not ready"), "owner@example.com"]

        def flaky():
            value = attempts.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(app_module, "_owner_from_apps_api", side_effect=flaky), \
             mock.patch.object(app_module.time, "sleep"):
            assert app_module.get_token_owner() == "owner@example.com"

    def test_no_retry_when_api_answers_with_no_owner(self, app_module):
        """An authoritative "this app has no owner" isn't worth retrying."""
        with mock.patch.object(
            app_module, "_owner_from_apps_api", return_value=None
        ) as api, mock.patch.object(app_module.time, "sleep"):
            app_module.get_token_owner()

        assert api.call_count == 1


class TestBackgroundRecovery:
    def test_recovers_owner_and_publishes_it(self, app_module, monkeypatch):
        """The whole point: the app self-heals instead of staying fail-closed
        until a human notices and restarts it."""
        monkeypatch.setattr(app_module, "app_owner", None, raising=False)
        # setenv (not delenv) so monkeypatch tracks the key and restores it at
        # teardown — the code under test writes os.environ directly, which
        # monkeypatch cannot see.
        monkeypatch.setenv("APP_OWNER", "")

        threads = []

        def run_immediately(target=None, daemon=None, name=None):
            threads.append(target)
            return SimpleNamespace(start=target)

        with mock.patch.object(app_module.threading, "Thread", run_immediately), \
             mock.patch.object(app_module.time, "sleep"), \
             mock.patch.object(
                 app_module, "_owner_from_apps_api", return_value="owner@example.com"
             ), \
             mock.patch.object(app_module.app_state, "set_app_owner") as persist:
            app_module._retry_owner_resolution_in_background()

        assert app_module.app_owner == "owner@example.com"
        assert app_module.os.environ["APP_OWNER"] == "owner@example.com"
        persist.assert_called_once_with("owner@example.com")

    def test_stops_early_if_owner_resolved_elsewhere(self, app_module, monkeypatch):
        """A PAT configured in the UI can resolve the owner first; the retry
        thread must not then overwrite it or keep hammering the API."""
        monkeypatch.setattr(app_module, "app_owner", "already@example.com", raising=False)

        def run_immediately(target=None, daemon=None, name=None):
            return SimpleNamespace(start=target)

        with mock.patch.object(app_module.threading, "Thread", run_immediately), \
             mock.patch.object(app_module.time, "sleep"), \
             mock.patch.object(app_module, "_owner_from_apps_api") as api:
            app_module._retry_owner_resolution_in_background()

        api.assert_not_called()
        assert app_module.app_owner == "already@example.com"

"""Tests for utils.workspace_sync_auth — auth for the Workspace sync round-trip.

The post-commit hook is the only durable backup in this ephemeral environment,
so its auth must work on *both* layers the app supports:

1. a ``[DEFAULT]`` PAT in ``~/.databrickscfg`` (pasted / rotated), and
2. the SP-broker profile, where the on-disk profile carries only a host and
   ``auth_type = databricks-cli``.

Before this, the sync required a PAT and died with "~/.databrickscfg missing
host or token" on every SP-broker instance — every commit silently unbacked.
"""

import pytest

import utils


class _FakeMe:
    user_name = "someone@example.com"


class _FakeCurrentUser:
    def __init__(self, calls, label):
        self._calls = calls
        self._label = label

    def me(self):
        self._calls.append(self._label)
        return _FakeMe()


class _FakeClient:
    def __init__(self, calls, **kwargs):
        self.kwargs = kwargs
        label = "pat" if "token" in kwargs else f"profile:{kwargs.get('profile')}"
        self.current_user = _FakeCurrentUser(calls, label)


@pytest.fixture
def fake_sdk(monkeypatch):
    """Patch databricks.sdk.WorkspaceClient; record how it was constructed."""
    calls = []
    created = []

    def factory(**kwargs):
        client = _FakeClient(calls, **kwargs)
        created.append(client)
        return client

    import databricks.sdk

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", factory)
    return calls, created


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)


def _write_cfg(tmp_path, monkeypatch, body):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".databrickscfg").write_text(body)


def test_prefers_default_pat(tmp_path, monkeypatch, fake_sdk):
    calls, created = fake_sdk
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "omnigents-host")
    _write_cfg(
        tmp_path,
        monkeypatch,
        "[DEFAULT]\nhost = https://example.databricks.com\ntoken = dapi123\n",
    )

    env, client = utils.workspace_sync_auth()

    assert calls == ["pat"], "the PAT must be validated before a long CLI call"
    assert created[0].kwargs["auth_type"] == "pat"
    # Pinned to DEFAULT: an ambient profile must not steer the CLI elsewhere.
    assert env["DATABRICKS_CONFIG_PROFILE"] == "DEFAULT"
    assert "DATABRICKS_TOKEN" not in env
    assert "DATABRICKS_CLIENT_ID" not in env


def test_falls_back_to_broker_profile_when_no_pat(tmp_path, monkeypatch, fake_sdk):
    """The SP-broker case: profile has a host but no token."""
    calls, created = fake_sdk
    _write_cfg(
        tmp_path,
        monkeypatch,
        "[omnigents-host]\n"
        "host = https://example.databricks.com\n"
        "auth_type = databricks-cli\n",
    )

    env, client = utils.workspace_sync_auth()

    assert calls == [f"profile:{utils.SYNC_FALLBACK_PROFILE}"]
    assert env["DATABRICKS_CONFIG_PROFILE"] == utils.SYNC_FALLBACK_PROFILE


def test_falls_back_when_databrickscfg_absent(tmp_path, monkeypatch, fake_sdk):
    calls, _ = fake_sdk
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.databrickscfg at all

    env, _ = utils.workspace_sync_auth()

    assert calls == [f"profile:{utils.SYNC_FALLBACK_PROFILE}"]
    assert env["DATABRICKS_CONFIG_PROFILE"] == utils.SYNC_FALLBACK_PROFILE


def test_honours_explicit_config_profile(tmp_path, monkeypatch, fake_sdk):
    calls, _ = fake_sdk
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "some-other-host")

    env, _ = utils.workspace_sync_auth()

    assert calls == ["profile:some-other-host"]
    assert env["DATABRICKS_CONFIG_PROFILE"] == "some-other-host"


def test_partial_default_profile_is_not_treated_as_pat(tmp_path, monkeypatch, fake_sdk):
    """host without token must not be used as a PAT — it would fail mid-sync."""
    calls, _ = fake_sdk
    _write_cfg(
        tmp_path, monkeypatch, "[DEFAULT]\nhost = https://example.databricks.com\n"
    )

    _, _ = utils.workspace_sync_auth()

    assert calls == [f"profile:{utils.SYNC_FALLBACK_PROFILE}"]

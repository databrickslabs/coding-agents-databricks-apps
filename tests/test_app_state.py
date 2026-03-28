"""Tests for app_state — persistent JSON at ~/.coda/app_state.json."""

import json
import os
import time
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path):
    """Point app_state at a temp dir so tests don't touch real state."""
    state_dir = str(tmp_path / ".coda")
    state_file = os.path.join(state_dir, "app_state.json")
    with mock.patch("app_state._STATE_DIR", state_dir), \
         mock.patch("app_state._STATE_FILE", state_file):
        yield state_file


class TestAppOwner:
    def test_set_and_read_owner(self, isolated_state):
        import app_state
        app_state.set_app_owner("alice@example.com")
        state = app_state.get_state()
        assert state["app_owner"] == "alice@example.com"
        assert "owner_resolved_at" in state

    def test_owner_persisted_to_disk(self, isolated_state):
        import app_state
        app_state.set_app_owner("bob@example.com")
        with open(isolated_state) as f:
            on_disk = json.load(f)
        assert on_disk["app_owner"] == "bob@example.com"


class TestLastRotation:
    def test_set_and_read_rotation(self, isolated_state):
        import app_state
        ts = time.time()
        app_state.set_last_rotation("tid-abc", ts)
        state = app_state.get_state()
        assert state["last_token_id"] == "tid-abc"
        assert state["last_rotation_time"] == ts
        assert "last_rotation_iso" in state

    def test_get_last_rotation_time(self, isolated_state):
        import app_state
        assert app_state.get_last_rotation_time() is None
        ts = time.time()
        app_state.set_last_rotation("tid-xyz", ts)
        assert app_state.get_last_rotation_time() == ts


class TestMerge:
    def test_owner_and_rotation_coexist(self, isolated_state):
        import app_state
        app_state.set_app_owner("carol@example.com")
        app_state.set_last_rotation("tid-123", time.time())
        state = app_state.get_state()
        assert state["app_owner"] == "carol@example.com"
        assert state["last_token_id"] == "tid-123"

    def test_file_permissions(self, isolated_state):
        import app_state
        import stat
        app_state.set_app_owner("dave@example.com")
        mode = stat.S_IMODE(os.stat(isolated_state).st_mode)
        assert mode == 0o600


class TestCorruptFile:
    def test_corrupt_json_returns_empty(self, isolated_state):
        import app_state
        os.makedirs(os.path.dirname(isolated_state), exist_ok=True)
        with open(isolated_state, "w") as f:
            f.write("{bad json")
        assert app_state.get_state() == {}

    def test_missing_file_returns_empty(self, isolated_state):
        import app_state
        assert app_state.get_state() == {}

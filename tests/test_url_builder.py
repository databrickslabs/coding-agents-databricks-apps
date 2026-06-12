"""Tests for url_builder module — base URL resolution for viewer_url."""
import os
import importlib
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _reset_module():
    """Re-import url_builder fresh for each test (module-level cache)."""
    from coda_mcp import url_builder
    importlib.reload(url_builder)
    yield


def test_returns_none_when_neither_env_nor_cache():
    from coda_mcp import url_builder
    assert url_builder.build_viewer_url("pty-1") is None


def test_env_override_wins():
    from coda_mcp import url_builder
    with mock.patch.dict(os.environ, {"CODA_APP_URL": "https://override.example.com"}):
        assert url_builder.build_viewer_url("pty-1") == \
            "https://override.example.com/?session=pty-1"


def test_env_override_strips_trailing_slash():
    from coda_mcp import url_builder
    with mock.patch.dict(os.environ, {"CODA_APP_URL": "https://override.example.com/"}):
        assert url_builder.build_viewer_url("pty-1") == \
            "https://override.example.com/?session=pty-1"


def test_header_capture_used_when_no_env():
    from coda_mcp import url_builder
    url_builder.capture_from_headers("app.databricksapps.com")
    assert url_builder.build_viewer_url("pty-1") == \
        "https://app.databricksapps.com/?session=pty-1"


def test_env_overrides_header_capture():
    from coda_mcp import url_builder
    url_builder.capture_from_headers("captured.example.com")
    with mock.patch.dict(os.environ, {"CODA_APP_URL": "https://override.example.com"}):
        assert url_builder.build_viewer_url("pty-1") == \
            "https://override.example.com/?session=pty-1"


def test_header_capture_overwrites_previous():
    from coda_mcp import url_builder
    url_builder.capture_from_headers("first.example.com")
    url_builder.capture_from_headers("second.example.com")
    assert "second.example.com" in url_builder.build_viewer_url("pty-1")


def test_capture_empty_string_does_not_overwrite():
    from coda_mcp import url_builder
    url_builder.capture_from_headers("good.example.com")
    url_builder.capture_from_headers("")
    assert "good.example.com" in url_builder.build_viewer_url("pty-1")


def test_capture_none_does_not_crash():
    from coda_mcp import url_builder
    url_builder.capture_from_headers(None)
    assert url_builder.build_viewer_url("pty-1") is None


def test_capture_strips_scheme_prefix():
    from coda_mcp import url_builder
    url_builder.capture_from_headers("https://app.example.com")
    assert url_builder._app_url_cache == "app.example.com"
    assert url_builder.build_viewer_url("pty-1") == "https://app.example.com/?session=pty-1"


def test_capture_strips_http_scheme_prefix():
    from coda_mcp import url_builder
    url_builder.capture_from_headers("http://app.example.com/")
    # http stripped, trailing slash stripped
    assert url_builder._app_url_cache == "app.example.com"

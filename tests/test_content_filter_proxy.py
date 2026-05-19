"""Tests for content_filter_proxy._get_fresh_token cache invalidation.

The proxy reads ~/.databrickscfg on every forwarded request, with a cache to
avoid filesystem hits in tight request bursts. The cache must invalidate the
moment the rotator rewrites the file, otherwise the proxy serves revoked
tokens to upstream for up to TTL seconds after each rotation.
"""

import time
from unittest import mock

import pytest


@pytest.fixture
def tmp_cfg(tmp_path, monkeypatch):
    """Point the proxy at a temp .databrickscfg, with a clean cache."""
    cfg = tmp_path / ".databrickscfg"
    import content_filter_proxy as cfp
    monkeypatch.setattr(cfp, "_DATABRICKSCFG_PATH", str(cfg))
    monkeypatch.setattr(cfp, "_TOKEN_CACHE", {"token": None, "read_at": 0.0, "mtime": 0.0})
    return cfg


def _write_cfg(path, token):
    path.write_text(f"[DEFAULT]\nhost = https://example.databricks.com\ntoken = {token}\n")


class TestFreshTokenCacheInvalidation:
    def test_cache_invalidates_on_mtime_change(self, tmp_cfg):
        from content_filter_proxy import _get_fresh_token
        _write_cfg(tmp_cfg, "dapi-old")
        assert _get_fresh_token() == "dapi-old"

        # Simulate rotator rewriting the file. utime to a guaranteed-newer mtime
        # so the test isn't sensitive to filesystem mtime granularity.
        _write_cfg(tmp_cfg, "dapi-new")
        import os
        st = os.stat(tmp_cfg)
        os.utime(tmp_cfg, (st.st_atime, st.st_mtime + 10))

        assert _get_fresh_token() == "dapi-new", "must re-read after mtime change"

    def test_cache_hits_when_mtime_unchanged(self, tmp_cfg):
        from content_filter_proxy import _get_fresh_token
        _write_cfg(tmp_cfg, "dapi-stable")
        assert _get_fresh_token() == "dapi-stable"

        # Mutate the file contents WITHOUT advancing mtime (force mtime backwards).
        # If the cache ignored mtime, it'd happily keep serving "dapi-stable";
        # if it consulted mtime, it'd still serve "dapi-stable" because mtime
        # didn't advance. Either way we expect the cached value back, which
        # asserts the cache is doing its de-dup job within the TTL.
        import os
        st = os.stat(tmp_cfg)
        _write_cfg(tmp_cfg, "dapi-tampered")
        os.utime(tmp_cfg, (st.st_atime, st.st_mtime))  # restore old mtime

        assert _get_fresh_token() == "dapi-stable"

    def test_falls_back_to_cache_on_stat_error(self, tmp_cfg, monkeypatch):
        from content_filter_proxy import _get_fresh_token
        _write_cfg(tmp_cfg, "dapi-cached")
        assert _get_fresh_token() == "dapi-cached"

        # Now make os.stat fail. The cache should still return the last known token.
        def boom(_):
            raise OSError("stat broken")
        monkeypatch.setattr("content_filter_proxy.os.stat", boom)
        assert _get_fresh_token() == "dapi-cached"

    def test_returns_none_when_file_missing_and_cache_empty(self, tmp_path, monkeypatch):
        import content_filter_proxy as cfp
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(cfp, "_DATABRICKSCFG_PATH", str(missing))
        monkeypatch.setattr(cfp, "_TOKEN_CACHE", {"token": None, "read_at": 0.0, "mtime": 0.0})
        assert cfp._get_fresh_token() is None

"""Tests for /api/upload endpoint — clipboard image upload."""

import io
import os
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_test_client():
    """Create a Flask test client for the app."""
    # Patch out initialize_app to avoid side effects during testing
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
        return app_module.app.test_client(), app_module


# ---------------------------------------------------------------------------
# 1. Successful upload
# ---------------------------------------------------------------------------

class TestSuccessfulUpload:
    """Uploading a valid file should save it and return its path."""

    def test_upload_returns_path(self, tmp_path):
        """POST /api/upload with a file should return a JSON path."""
        with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            client, _ = _get_test_client()
            data = {"file": (io.BytesIO(b"fake png data"), "test.png")}
            resp = client.post("/api/upload", data=data, content_type="multipart/form-data")

        assert resp.status_code == 200
        body = resp.get_json()
        assert "path" in body
        assert body["path"].endswith(".png")

    def test_uploaded_file_exists_on_disk(self, tmp_path):
        """The file returned by /api/upload should actually exist."""
        with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            client, _ = _get_test_client()
            data = {"file": (io.BytesIO(b"fake png data"), "test.png")}
            resp = client.post("/api/upload", data=data, content_type="multipart/form-data")

        path = resp.get_json()["path"]
        assert os.path.isfile(path)

    def test_uploaded_file_content_matches(self, tmp_path):
        """The saved file should contain the exact bytes that were uploaded."""
        content = b"\x89PNG\r\n\x1a\nfake image content"
        with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            client, _ = _get_test_client()
            data = {"file": (io.BytesIO(content), "screenshot.png")}
            resp = client.post("/api/upload", data=data, content_type="multipart/form-data")

        path = resp.get_json()["path"]
        with open(path, "rb") as f:
            assert f.read() == content

    def test_upload_creates_uploads_directory(self, tmp_path):
        """The uploads/ directory should be created if it doesn't exist."""
        with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            client, _ = _get_test_client()
            data = {"file": (io.BytesIO(b"data"), "img.png")}
            client.post("/api/upload", data=data, content_type="multipart/form-data")

        assert os.path.isdir(os.path.join(str(tmp_path), "uploads"))


# ---------------------------------------------------------------------------
# 2. Filename sanitization
# ---------------------------------------------------------------------------

class TestFilenameSanitization:
    """Uploaded filenames should be sanitized to prevent path traversal."""

    def test_path_traversal_sanitized(self, tmp_path):
        """Filenames with ../ should be sanitized."""
        with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            client, _ = _get_test_client()
            data = {"file": (io.BytesIO(b"data"), "../../../etc/passwd")}
            resp = client.post("/api/upload", data=data, content_type="multipart/form-data")

        path = resp.get_json()["path"]
        # File should be inside uploads/, not in /etc/
        assert "uploads" in path
        assert "/etc/" not in path

    def test_uuid_prefix_in_filename(self, tmp_path):
        """Saved filename should have a UUID prefix for uniqueness."""
        with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            client, _ = _get_test_client()
            data = {"file": (io.BytesIO(b"data"), "test.png")}
            resp = client.post("/api/upload", data=data, content_type="multipart/form-data")

        path = resp.get_json()["path"]
        basename = os.path.basename(path)
        # Should be <8-char-hex>_test.png
        assert "_" in basename
        prefix = basename.split("_")[0]
        assert len(prefix) == 8


# ---------------------------------------------------------------------------
# 3. Error cases
# ---------------------------------------------------------------------------

class TestErrorCases:
    """Upload endpoint should reject invalid requests."""

    def test_no_file_returns_400(self):
        """POST without a file part should return 400."""
        with mock.patch.dict(os.environ, {"HOME": "/tmp/test_upload"}):
            client, _ = _get_test_client()
            resp = client.post("/api/upload", data={}, content_type="multipart/form-data")

        assert resp.status_code == 400
        assert "No file provided" in resp.get_json()["error"]

    def test_empty_filename_returns_400(self):
        """POST with an empty filename should return 400."""
        with mock.patch.dict(os.environ, {"HOME": "/tmp/test_upload"}):
            client, _ = _get_test_client()
            data = {"file": (io.BytesIO(b"data"), "")}
            resp = client.post("/api/upload", data=data, content_type="multipart/form-data")

        assert resp.status_code == 400
        assert "Empty filename" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# 4. HOME fallback
# ---------------------------------------------------------------------------

class TestHomeFallback:
    """Upload should use /app/python/source_code when HOME is unset or /."""

    def test_fallback_when_home_is_root(self, tmp_path):
        """When HOME='/', should use /app/python/source_code."""
        with mock.patch.dict(os.environ, {"HOME": "/"}), \
             mock.patch("os.makedirs") as mock_mkdirs, \
             mock.patch("werkzeug.datastructures.file_storage.FileStorage.save"):
            client, _ = _get_test_client()
            data = {"file": (io.BytesIO(b"data"), "test.png")}
            resp = client.post("/api/upload", data=data, content_type="multipart/form-data")

        # Should have called makedirs with the fallback path
        call_args = mock_mkdirs.call_args_list
        upload_dir = call_args[-1][0][0] if call_args else ""
        assert "/app/python/source_code/uploads" in upload_dir

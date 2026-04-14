"""Tests for xterm.js ClipboardAddon (OSC 52 support).

Verifies that:
- addon-clipboard.js exists in static/lib/
- The addon file contains the ClipboardAddon class
- The addon registers an OSC 52 handler
- index.html loads the addon script
- index.html initializes the addon on terminal creation
"""

import os
import re

import pytest


STATIC_LIB = os.path.join(os.path.dirname(__file__), "..", "static", "lib")
INDEX_HTML = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")


class TestClipboardAddonFile:

    def test_addon_file_exists(self):
        path = os.path.join(STATIC_LIB, "addon-clipboard.js")
        assert os.path.isfile(path), "addon-clipboard.js missing from static/lib/"

    def test_addon_exports_clipboard_addon(self):
        path = os.path.join(STATIC_LIB, "addon-clipboard.js")
        with open(path) as f:
            content = f.read()
        assert "ClipboardAddon" in content, "addon-clipboard.js does not export ClipboardAddon"

    def test_addon_handles_osc_52(self):
        path = os.path.join(STATIC_LIB, "addon-clipboard.js")
        with open(path) as f:
            content = f.read()
        # The addon should register an OSC handler for sequence 52
        assert re.search(r'registerOscHandler\S*\(52', content), (
            "addon-clipboard.js does not register an OSC 52 handler"
        )

    def test_addon_uses_navigator_clipboard(self):
        path = os.path.join(STATIC_LIB, "addon-clipboard.js")
        with open(path) as f:
            content = f.read()
        assert "navigator.clipboard" in content, "addon should use navigator.clipboard API"


class TestClipboardAddonIntegration:

    @pytest.fixture(autouse=True)
    def _load_html(self):
        self.html = open(INDEX_HTML).read()

    def test_script_tag_present(self):
        assert "addon-clipboard.js" in self.html, (
            "<script> tag for addon-clipboard.js missing from index.html"
        )

    def test_script_loaded_after_other_addons(self):
        # addon-clipboard should be loaded after the core addons
        image_pos = self.html.index("addon-image.js")
        clipboard_pos = self.html.index("addon-clipboard.js")
        assert clipboard_pos > image_pos, (
            "addon-clipboard.js should be loaded after addon-image.js"
        )

    def test_addon_initialized_in_create_pane(self):
        assert "ClipboardAddon.ClipboardAddon()" in self.html, (
            "ClipboardAddon not initialized in createPane()"
        )

    def test_addon_guarded_by_typeof_check(self):
        assert "typeof ClipboardAddon" in self.html, (
            "ClipboardAddon loading should be guarded by typeof check"
        )

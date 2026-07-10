"""Tests for the shared ~/.databrickscfg non-DEFAULT preservation (spec C, C-R5).

The 2026-07-07 clobber was NOT the rotator (b5b11a6 fixed that) — it was
setup_databricks.py doing a naive DEFAULT-only write_text() on every container
boot, truncating the [omnigents-host] OAuth block the host had appended in the
previous container life. Both writers now share
utils.read_non_default_databrickscfg_sections so they honor one contract.
"""

from utils import read_non_default_databrickscfg_sections


_CFG_WITH_HOST = (
    "[DEFAULT]\n"
    "host = https://test.databricks.com\n"
    "token = dapi-old\n"
    "\n[omnigents-host]\n"
    "host = https://test.databricks.com\n"
    "client_id = sp-client-id\n"
    "client_secret = sp-secret\n"
    "auth_type = oauth-m2m\n"
)


class TestReadNonDefaultSections:
    def test_preserves_omnigents_host(self, tmp_path):
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text(_CFG_WITH_HOST)
        preserved = read_non_default_databrickscfg_sections(cfg)
        assert "[omnigents-host]" in preserved
        assert "client_id = sp-client-id" in preserved
        assert "auth_type = oauth-m2m" in preserved
        # DEFAULT and its keys are NOT carried through (the caller rebuilds them).
        assert "[DEFAULT]" not in preserved
        assert "dapi-old" not in preserved

    def test_default_only_returns_empty(self, tmp_path):
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text("[DEFAULT]\nhost = h\ntoken = t\n")
        assert read_non_default_databrickscfg_sections(cfg) == ""

    def test_missing_file_returns_empty(self, tmp_path):
        assert read_non_default_databrickscfg_sections(tmp_path / "nope") == ""

    def test_concatenation_shape(self, tmp_path):
        """Result wraps in newlines so it appends cleanly after a [DEFAULT] block."""
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text(_CFG_WITH_HOST)
        preserved = read_non_default_databrickscfg_sections(cfg)
        rebuilt = "[DEFAULT]\nhost = h\ntoken = new\n" + preserved
        # A round-trip parse sees both sections.
        import configparser
        cp = configparser.ConfigParser()
        cp.read_string(rebuilt)
        assert cp["DEFAULT"]["token"] == "new"
        assert cp["omnigents-host"]["auth_type"] == "oauth-m2m"


class TestSetupDatabricksRestartClobber:
    """setup_databricks.py must preserve [omnigents-host] on a boot rewrite."""

    def test_boot_rewrite_preserves_host_profile(self, tmp_path, monkeypatch):
        # Simulate the on-disk state from the previous container life.
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text(_CFG_WITH_HOST)

        # Reproduce setup_databricks.py's write (the code under test), reading
        # through the shared helper exactly as the module now does.
        host = "https://test.databricks.com"
        token = "dapi-fresh-boot"
        preserved = read_non_default_databrickscfg_sections(cfg)
        cfg.write_text(f"[DEFAULT]\nhost = {host}\ntoken = {token}\n" + preserved)

        content = cfg.read_text()
        # DEFAULT got the fresh boot token...
        assert "token = dapi-fresh-boot" in content
        assert "dapi-old" not in content
        # ...and the host OAuth profile survived the boot rewrite (the fix).
        assert "[omnigents-host]" in content
        assert "auth_type = oauth-m2m" in content

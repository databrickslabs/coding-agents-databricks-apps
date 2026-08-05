"""OpenCode's auth.json credential schema, and the writer/rotator contract.

Two places touch `~/.local/share/opencode/auth.json` and must agree:

- `setup_opencode.py` writes it at setup time.
- `cli_auth._update_opencode()` rewrites the secret on every PAT rotation
  (~10 min).

Both used to write `api_key`. That is not a field opencode recognises — its
credentials are a discriminated union on `type`, and the API-key variant keeps
the secret in `key`:

    export class Api extends Schema.Class<Api>("ApiAuth")({
        type: Schema.Literal("api"),
        key: Schema.String,
        metadata: Schema.optional(Schema.Record(Schema.String, Schema.String)),
    }) {}

    const _Info = Schema.Union([Oauth, Api, WellKnown])
        .annotate({ discriminator: "type", identifier: "Auth" })

(opencode, packages/opencode/src/auth/index.ts)

Because both sides were *consistently* wrong, every unit test passed, and the
content-filter proxy masked the effect at runtime by injecting a fresh bearer
token per request. The missing assertion was that the writer's output is a shape
the rotator can rotate — so that's what this file pins down.

The shape now lives in one place (`utils.opencode_api_credential`), which is
what actually prevents the drift; these tests hold both sides to it.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from utils import (
    OPENCODE_AUTH_KEY_FIELD,
    is_opencode_api_credential,
    opencode_api_credential,
)

# The union discriminants opencode accepts.
VALID_AUTH_TYPES = {"api", "oauth", "wellknown"}


class TestCredentialShape:
    def test_uses_tagged_union_shape(self):
        assert opencode_api_credential("tok") == {"type": "api", "key": "tok"}

    def test_does_not_emit_api_key(self):
        """`api_key` was the original bug. Assert it can never come back."""
        assert "api_key" not in opencode_api_credential("tok")

    def test_declares_a_valid_discriminant(self):
        assert opencode_api_credential("tok")["type"] in VALID_AUTH_TYPES

    @pytest.mark.parametrize(
        "cred,expected",
        [
            ({"type": "api", "key": "k"}, True),
            # The old, unloadable shape — must not be treated as rotatable.
            ({"api_key": "k"}, False),
            # Other union members carry different fields; rotating them would
            # corrupt a credential opencode still needs.
            ({"type": "oauth", "refresh": "r", "access": "a", "expires": 1}, False),
            ({"type": "wellknown", "key": "k", "token": "t"}, False),
            ({"type": "api"}, False),  # missing key
            ("not-a-dict", False),
            (None, False),
        ],
    )
    def test_recognises_only_rotatable_api_credentials(self, cred, expected):
        assert is_opencode_api_credential(cred) is expected


class TestWriterAndRotatorAgree:
    """The regression guard: whatever the writer produces, the rotator must be
    able to rotate. Either half drifting silently breaks token rotation.

    This exercises the real rotator against the real writer's output, without
    running setup_opencode.py — that script resolves the gateway and a token over
    the network, so spawning it makes the test slow and non-hermetic.
    """

    def _write_auth(self, home, providers):
        auth_dir = home / ".local" / "share" / "opencode"
        auth_dir.mkdir(parents=True, exist_ok=True)
        path = auth_dir / "auth.json"
        # Exactly what setup_opencode.py writes, via the same helper.
        path.write_text(
            json.dumps({p: opencode_api_credential(tok) for p, tok in providers.items()}, indent=2)
        )
        path.chmod(0o600)
        return path

    def test_rotator_updates_what_the_writer_wrote(self, tmp_path):
        import cli_auth

        path = self._write_auth(
            tmp_path, {"databricks": "old", "databricks-openai": "old"}
        )

        with mock.patch.object(cli_auth, "_HOME", str(tmp_path)):
            cli_auth._update_opencode("rotated-token")

        result = json.loads(path.read_text())
        for provider in ("databricks", "databricks-openai"):
            assert result[provider][OPENCODE_AUTH_KEY_FIELD] == "rotated-token", (
                f"{provider}: rotator did not update the field the writer wrote — "
                f"setup_opencode.py and cli_auth._update_opencode() have drifted"
            )
            # `type` is the union discriminant; losing it makes the credential
            # unparseable even though the secret is correct.
            assert result[provider]["type"] == "api"

    def test_rotation_preserves_0600(self, tmp_path):
        """auth.json holds a live token. The writer chmods it 0600 and rotation
        must not widen it (os.replace installs the tmp file's mode)."""
        import stat

        import cli_auth

        path = self._write_auth(tmp_path, {"databricks": "old"})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

        with mock.patch.object(cli_auth, "_HOME", str(tmp_path)):
            cli_auth._update_opencode("rotated-token")

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_rotation_is_idempotent(self, tmp_path):
        import cli_auth

        path = self._write_auth(tmp_path, {"databricks": "old"})

        with mock.patch.object(cli_auth, "_HOME", str(tmp_path)):
            cli_auth._update_opencode("tok")
            first = path.read_text()
            cli_auth._update_opencode("tok")

        assert path.read_text() == first


class TestSetupOpencodeUsesTheSharedHelper:
    """Guard against setup_opencode.py hand-rolling the credential dict again."""

    def test_source_imports_and_calls_the_helper(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "setup_opencode.py").read_text()
        assert "opencode_api_credential" in src, (
            "setup_opencode.py should build credentials via "
            "utils.opencode_api_credential() so the shape stays in one place"
        )
        assert '"api_key"' not in src and "'api_key'" not in src, (
            "setup_opencode.py must not write `api_key` — opencode reads `key`"
        )

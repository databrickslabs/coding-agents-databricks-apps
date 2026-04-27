"""Update literal tokens in CLI config files on PAT rotation.

Called by pat_rotator._persist_token() every 10 minutes. Lightweight —
just swaps token values in existing files, no installs or script runs.
"""

import json
import os
import re
import logging

logger = logging.getLogger(__name__)

_HOME = os.environ.get("HOME", "/app/python/source_code")
if not _HOME or _HOME == "/":
    _HOME = "/app/python/source_code"


def update_cli_tokens(token):
    """Update the literal token in all CLI config files."""
    _update_claude(token)
    _update_codex(token)
    _update_opencode(token)
    _update_gemini(token)
    _update_hermes(token)


def _update_claude(token):
    """Update ANTHROPIC_AUTH_TOKEN in ~/.claude/settings.json."""
    path = os.path.join(_HOME, ".claude", "settings.json")
    try:
        with open(path) as f:
            settings = json.load(f)
        if "env" in settings and "ANTHROPIC_AUTH_TOKEN" in settings["env"]:
            settings["env"]["ANTHROPIC_AUTH_TOKEN"] = token
            with open(path, "w") as f:
                json.dump(settings, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass  # file doesn't exist yet — initial setup hasn't run


def _update_codex(token):
    """Update OPENAI_API_KEY in ~/.codex/.env."""
    path = os.path.join(_HOME, ".codex", ".env")
    _replace_dotenv_key(path, "OPENAI_API_KEY", token)


def _update_opencode(token):
    """Update api_key values in ~/.local/share/opencode/auth.json."""
    path = os.path.join(_HOME, ".local", "share", "opencode", "auth.json")
    try:
        with open(path) as f:
            auth = json.load(f)
        changed = False
        for provider in auth.values():
            if isinstance(provider, dict) and "api_key" in provider:
                provider["api_key"] = token
                changed = True
        if changed:
            with open(path, "w") as f:
                json.dump(auth, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


def _update_gemini(token):
    """Update GEMINI_API_KEY in ~/.gemini/.env."""
    path = os.path.join(_HOME, ".gemini", ".env")
    _replace_dotenv_key(path, "GEMINI_API_KEY", token)


def _update_hermes(token):
    """Update api_key lines in ~/.hermes/config.yaml."""
    path = os.path.join(_HOME, ".hermes", "config.yaml")
    try:
        with open(path) as f:
            content = f.read()
        new_content = re.sub(
            r'^(  api_key: ).*$',
            rf'\g<1>{token}',
            content,
            flags=re.MULTILINE
        )
        if new_content != content:
            with open(path, "w") as f:
                f.write(new_content)
    except OSError:
        pass


def _replace_dotenv_key(path, key, value):
    """Replace a KEY=value line in a dotenv file."""
    try:
        with open(path) as f:
            content = f.read()
        new_content = re.sub(
            rf'^{re.escape(key)}=.*$',
            f'{key}={value}',
            content,
            flags=re.MULTILINE
        )
        if new_content != content:
            with open(path, "w") as f:
                f.write(new_content)
    except OSError:
        pass

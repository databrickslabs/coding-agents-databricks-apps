#!/usr/bin/env python
"""Configure Databricks CLI with the user's PAT from environment."""
import os
import subprocess
from pathlib import Path

from utils import ensure_https

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

# Get credentials from environment
host = os.environ.get("DATABRICKS_HOST")
token = os.environ.get("DATABRICKS_TOKEN")

if not host or not token:
    print("Warning: DATABRICKS_HOST or DATABRICKS_TOKEN not set, skipping CLI config")
    exit(0)

host = ensure_https(host)

# Create ~/.databrickscfg with DEFAULT profile using PAT auth
databrickscfg = home / ".databrickscfg"
config_content = f"""[DEFAULT]
host = {host}
token = {token}
"""

databrickscfg.write_text(config_content)
databrickscfg.chmod(0o600)  # Restrict permissions
print(f"Databricks CLI configured: {databrickscfg}")

# Verify it works
result = subprocess.run(
    ["databricks", "current-user", "me", "--output", "json"],
    capture_output=True,
    text=True,
    env={
        **os.environ,
        # Remove OAuth vars to force PAT auth
        "DATABRICKS_CLIENT_ID": "",
        "DATABRICKS_CLIENT_SECRET": ""
    }
)

if result.returncode == 0:
    import json
    try:
        user = json.loads(result.stdout)
        email = user.get('userName', '')
        display_name = user.get('displayName', '')
        print(f"Databricks CLI authenticated as: {email}")

        # Configure git with user's email and name
        if email:
            subprocess.run(["git", "config", "--global", "user.email", email], check=False)
            print(f"Git configured with email: {email}")
        if display_name:
            subprocess.run(["git", "config", "--global", "user.name", display_name], check=False)
            print(f"Git configured with name: {display_name}")
        elif email:
            # Fall back to email prefix as name if no display name
            name_from_email = email.split('@')[0].replace('.', ' ').title()
            subprocess.run(["git", "config", "--global", "user.name", name_from_email], check=False)
            print(f"Git configured with name: {name_from_email}")
    except json.JSONDecodeError:
        print("Databricks CLI configured (couldn't parse user)")
else:
    print(f"Warning: CLI config may have issues: {result.stderr}")

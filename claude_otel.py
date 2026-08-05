import os


OTEL_SIGNALS = {
    "TRACES": ("traces", "spans"),
    "LOGS": ("logs", "logs"),
    "METRICS": ("metrics", "metrics"),
}


def claude_otel_enabled():
    return os.environ.get("CLAUDE_CODE_OTEL_ENABLED", "false").lower() == "true"


def apply_claude_otel_env(settings, token, databricks_host):
    """Merge Claude Code OTEL env vars into settings when explicitly enabled."""
    if not claude_otel_enabled():
        return False

    catalog_schema = os.environ.get("CLAUDE_CODE_OTEL_CATALOG_SCHEMA", "").strip(". ")
    if not catalog_schema:
        return False

    env = settings.setdefault("env", {})
    env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
    base_url = databricks_host.rstrip("/")

    for signal, (endpoint_suffix, table_suffix) in OTEL_SIGNALS.items():
        table = f"{catalog_schema}.claude_otel_{table_suffix}"
        env[f"OTEL_{signal}_EXPORTER"] = "otlp"
        env[f"OTEL_EXPORTER_OTLP_{signal}_PROTOCOL"] = "http/protobuf"
        env[f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT"] = (
            f"{base_url}/api/2.0/otel/v1/{endpoint_suffix}"
        )
        env[f"OTEL_EXPORTER_OTLP_{signal}_HEADERS"] = _otel_headers(token, table)

    return True


def refresh_claude_otel_token(settings, token):
    """Refresh bearer tokens in existing Claude OTEL headers."""
    env = settings.get("env", {})
    changed = False
    for signal in OTEL_SIGNALS:
        key = f"OTEL_EXPORTER_OTLP_{signal}_HEADERS"
        if key in env:
            env[key] = _replace_authorization(env[key], token)
            changed = True
    return changed


def _otel_headers(token, table):
    return (
        "content-type=application/x-protobuf,"
        f"Authorization=Bearer {token},"
        f"X-Databricks-UC-Table-Name={table}"
    )


def _replace_authorization(headers, token):
    parts = []
    replaced = False
    for part in headers.split(","):
        if part.startswith("Authorization=Bearer "):
            parts.append(f"Authorization=Bearer {token}")
            replaced = True
        else:
            parts.append(part)
    if not replaced:
        parts.append(f"Authorization=Bearer {token}")
    return ",".join(parts)

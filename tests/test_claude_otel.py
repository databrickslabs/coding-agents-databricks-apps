"""Tests for Claude Code OTEL-to-Unity-Catalog settings."""

from unittest import mock

from claude_otel import apply_claude_otel_env, refresh_claude_otel_token


def test_otel_disabled_by_default():
    settings = {"env": {"ANTHROPIC_MODEL": "keep"}}

    changed = apply_claude_otel_env(settings, "token", "https://example.cloud.databricks.com")

    assert changed is False
    assert settings == {"env": {"ANTHROPIC_MODEL": "keep"}}


def test_otel_enabled_writes_all_signal_env_vars():
    settings = {"env": {"ANTHROPIC_MODEL": "keep"}}

    with mock.patch.dict(
        "os.environ",
        {
            "CLAUDE_CODE_OTEL_ENABLED": "true",
            "CLAUDE_CODE_OTEL_CATALOG_SCHEMA": "test_catalog.default",
        },
    ):
        changed = apply_claude_otel_env(
            settings,
            "dapi_test_token",
            "https://adb-7405619319592766.6.azuredatabricks.net",
        )

    env = settings["env"]
    assert changed is True
    assert env["ANTHROPIC_MODEL"] == "keep"
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"

    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert env["OTEL_LOGS_EXPORTER"] == "otlp"
    assert env["OTEL_METRICS_EXPORTER"] == "otlp"

    assert env["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_EXPORTER_OTLP_LOGS_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_EXPORTER_OTLP_METRICS_PROTOCOL"] == "http/protobuf"

    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"].endswith("/api/2.0/otel/v1/traces")
    assert env["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"].endswith("/api/2.0/otel/v1/logs")
    assert env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"].endswith("/api/2.0/otel/v1/metrics")

    assert "Authorization=Bearer dapi_test_token" in env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"]
    assert (
        "X-Databricks-UC-Table-Name=test_catalog.default.claude_otel_spans"
        in env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"]
    )
    assert (
        "X-Databricks-UC-Table-Name=test_catalog.default.claude_otel_logs"
        in env["OTEL_EXPORTER_OTLP_LOGS_HEADERS"]
    )
    assert (
        "X-Databricks-UC-Table-Name=test_catalog.default.claude_otel_metrics"
        in env["OTEL_EXPORTER_OTLP_METRICS_HEADERS"]
    )


def test_otel_requires_catalog_schema_when_enabled():
    settings = {"env": {}}

    with mock.patch.dict("os.environ", {"CLAUDE_CODE_OTEL_ENABLED": "true"}, clear=True):
        changed = apply_claude_otel_env(settings, "token", "https://example.cloud.databricks.com")

    assert changed is False
    assert settings == {"env": {}}


def test_refresh_claude_otel_token_preserves_table_headers():
    settings = {
        "env": {
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS": (
                "content-type=application/x-protobuf,"
                "Authorization=Bearer old-token,"
                "X-Databricks-UC-Table-Name=cat.sch.claude_otel_spans"
            ),
            "OTEL_EXPORTER_OTLP_LOGS_HEADERS": (
                "content-type=application/x-protobuf,"
                "Authorization=Bearer old-token,"
                "X-Databricks-UC-Table-Name=cat.sch.claude_otel_logs"
            ),
            "OTEL_EXPORTER_OTLP_METRICS_HEADERS": (
                "content-type=application/x-protobuf,"
                "Authorization=Bearer old-token,"
                "X-Databricks-UC-Table-Name=cat.sch.claude_otel_metrics"
            ),
        }
    }

    changed = refresh_claude_otel_token(settings, "new-token")

    assert changed is True
    for headers in settings["env"].values():
        assert "Authorization=Bearer new-token" in headers
        assert "old-token" not in headers
    assert "cat.sch.claude_otel_spans" in settings["env"]["OTEL_EXPORTER_OTLP_TRACES_HEADERS"]
    assert "cat.sch.claude_otel_logs" in settings["env"]["OTEL_EXPORTER_OTLP_LOGS_HEADERS"]
    assert "cat.sch.claude_otel_metrics" in settings["env"]["OTEL_EXPORTER_OTLP_METRICS_HEADERS"]

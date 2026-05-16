"""Integration test: run the full CoDA setup pipeline in an apps-like Docker
container, then assert on the resulting filesystem + env state.

This is the codified version of the manual chrome-devtools verification —
it replaces "log in, paste PAT, open terminal, run commands, screenshot"
with a single `make integration-test` that builds a representative
container, runs the pipeline inside, and parses verify.sh output.

Token cost per run: zero. Wall time: ~3-5 minutes (npm + uv installs).

Skipped automatically if Docker isn't installed — locally and in CI both.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
IMAGE_TAG = "coda-apps-test:latest"
DOCKERFILE = REPO_ROOT / "tests" / "integration" / "Dockerfile.apps-like"
PIPELINE_SCRIPT = "/repo/tests/integration/run_pipeline.sh"


def _docker_available() -> bool:
    """True iff Docker CLI exists AND the daemon responds."""
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True, timeout=5, check=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _host_ca_bundle() -> str | None:
    """Detect a CA bundle path on the host for corporate TLS environments.

    Checked in order: env vars (REQUESTS_CA_BUNDLE, SSL_CERT_FILE,
    CURL_CA_BUNDLE), common Linux locations, common macOS Homebrew
    locations. Returns None if no bundle is found — in that case the
    container relies on its baked-in trust store (works for
    non-intercepted networks).
    """
    import os
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        path = os.environ.get(var, "").strip()
        if path and Path(path).exists():
            return path
    for candidate in (
        Path.home() / ".ssl" / "combined-ca-bundle.pem",
        Path("/etc/ssl/certs/ca-certificates.crt"),
        Path("/etc/ssl/cert.pem"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _host_pypi_index() -> str | None:
    """Detect a PyPI index URL configured on the host.

    Honours the enterprise feature's env-var contract — operators on
    corporate networks (where pypi.org is blocked) configure an internal
    proxy via UV_DEFAULT_INDEX / PIP_INDEX_URL or in pip.conf/uv.toml.
    This function detects that config and forwards it into the container,
    which is exactly how a real enterprise CoDA deployment would work.

    Returns None if no proxy is configured (test will use upstream pypi).
    """
    import configparser
    import os
    # Env vars first
    for var in ("UV_DEFAULT_INDEX", "PIP_INDEX_URL"):
        url = os.environ.get(var, "").strip()
        if url:
            return url
    # pip.conf
    for candidate in (
        Path.home() / ".pip" / "pip.conf",
        Path.home() / ".config" / "pip" / "pip.conf",
    ):
        if candidate.exists():
            cp = configparser.ConfigParser()
            try:
                cp.read(candidate)
                if cp.has_option("global", "index-url"):
                    return cp.get("global", "index-url").strip()
            except configparser.Error:
                continue
    # uv.toml
    uv_toml = Path.home() / ".config" / "uv" / "uv.toml"
    if uv_toml.exists():
        try:
            import tomllib
            with uv_toml.open("rb") as f:
                data = tomllib.load(f)
            for idx in data.get("index", []):
                if idx.get("default") and idx.get("url"):
                    return idx["url"].strip()
        except Exception:
            pass
    return None


def _pypi_reachable_from_container() -> tuple[bool, str]:
    """Quick test that some PyPI index is reachable from inside a container.

    Uses the host's configured PyPI proxy (UV_DEFAULT_INDEX / PIP_INDEX_URL /
    pip.conf / uv.toml) if one is set — this is the enterprise feature's
    own contract, so the test exercises the same path operators do. Falls
    back to public pypi.org if no proxy is configured.

    On Databricks-employee laptops, pypi.org is firewalled but
    https://pypi-proxy.dev.databricks.com/simple/ is reachable. The host's
    pip.conf/uv.toml typically points at that proxy already.

    The apps-like image (which has curl + the host CA mounted) is preferred
    so we test from the same trust context the real pipeline will use.
    """
    if not shutil.which("docker"):
        return False, "docker CLI not available"
    image_check = subprocess.run(
        ["docker", "image", "inspect", IMAGE_TAG],
        capture_output=True, timeout=5,
    )
    if image_check.returncode != 0:
        # If the apps-like image isn't built yet, use curlimages/curl.
        # That image doesn't have our CA bundle so this test may falsely
        # report "blocked" — but in that case the user will build the
        # image first and re-run.
        image = "curlimages/curl:latest"
        ca_mount: list[str] = []
        ca_env: list[str] = []
    else:
        image = IMAGE_TAG
        ca_path = _host_ca_bundle()
        ca_mount = [
            "-v", f"{ca_path}:/etc/ssl/coda-host-ca.pem:ro"
        ] if ca_path else []
        ca_env = [
            "-e", "CURL_CA_BUNDLE=/etc/ssl/coda-host-ca.pem"
        ] if ca_path else []

    # Probe whichever index the host is configured for (proxy or public)
    index_url = _host_pypi_index() or "https://pypi.org/simple/"
    probe_url = index_url.rstrip("/") + "/wheel/"

    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--entrypoint", "curl",
                *ca_mount, *ca_env,
                image,
                "-sS", "--max-time", "10",
                "-o", "/dev/null", "-w", "%{http_code}",
                probe_url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False, (
                f"curl to {probe_url} exited {result.returncode}: "
                f"{result.stderr[:200].strip()}"
            )
        status = result.stdout.strip()
        if status == "200":
            return True, f"index reachable: {index_url}"
        return False, (
            f"{probe_url} returned HTTP {status} "
            f"(likely blocked by corporate proxy)"
        )
    except subprocess.TimeoutExpired:
        return False, "index reachability check timed out"


def _integration_skip_reason() -> str | None:
    if not _docker_available():
        return "Docker daemon not available"
    reachable, why = _pypi_reachable_from_container()
    if not reachable:
        return (
            f"pypi.org not reachable from container ({why}). "
            "This test needs pypi to install requirements.txt. "
            "Run on a non-corporate network or in CI."
        )
    return None


pytestmark = pytest.mark.skipif(
    _integration_skip_reason() is not None,
    reason=_integration_skip_reason() or "",
)


@pytest.fixture(scope="module")
def apps_like_image():
    """Build the apps-like image once per test session.

    Docker layer cache makes subsequent builds nearly free; the first build
    on a fresh machine takes ~2 minutes (apt + uv install).
    """
    build = subprocess.run(
        [
            "docker", "build",
            # Pin to amd64 — install scripts download linux_amd64 binaries
            # and Databricks Apps runtime is amd64. Without this on Apple
            # Silicon hosts, the image builds as arm64 and the install
            # downloads 404.
            "--platform", "linux/amd64",
            "-f", str(DOCKERFILE),
            "-t", IMAGE_TAG,
            str(REPO_ROOT / "tests" / "integration"),  # context = the integration dir
        ],
        capture_output=True, text=True,
    )
    if build.returncode != 0:
        pytest.fail(
            f"docker build failed (rc={build.returncode}):\n"
            f"--- stdout ---\n{build.stdout[-1500:]}\n"
            f"--- stderr ---\n{build.stderr[-1500:]}"
        )
    return IMAGE_TAG


def _run_pipeline(image: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the pipeline script inside a fresh container with the repo mounted.

    Auto-forwards the host's CA bundle (if found) so the container can reach
    pypi.org / registry.npmjs.org / github.com from inside corporate-TLS-
    intercepted networks. Same mechanism the enterprise feature uses.
    """
    env_args: list[str] = []
    mount_args: list[str] = []
    ca = _host_ca_bundle()
    if ca:
        container_ca = "/etc/ssl/coda-host-ca.pem"
        mount_args.extend(["-v", f"{ca}:{container_ca}:ro"])
        env_args.extend([
            "-e", f"REQUESTS_CA_BUNDLE={container_ca}",
            "-e", f"SSL_CERT_FILE={container_ca}",
            "-e", f"CURL_CA_BUNDLE={container_ca}",
            "-e", f"NODE_EXTRA_CA_CERTS={container_ca}",
            # uv reads UV_SYSTEM_CERTS to use the system trust store.
            "-e", "UV_SYSTEM_CERTS=true",
        ])
    # Forward host's PyPI proxy if configured (Databricks-internal proxy,
    # JFrog mirror, etc.). This is the enterprise feature's contract — the
    # test pipeline will install requirements.txt through this proxy, which
    # is exactly how a CoDA customer in a firewalled env would run.
    pypi_index = _host_pypi_index()
    if pypi_index:
        env_args.extend([
            "-e", f"PIP_INDEX_URL={pypi_index}",
            # uv reads UV_DEFAULT_INDEX. We only set it if the host had it
            # — operators may want the test to default to public pypi.
            "-e", f"UV_DEFAULT_INDEX={pypi_index}",
        ])
    for k, v in (extra_env or {}).items():
        env_args.extend(["-e", f"{k}={v}"])
    return subprocess.run(
        [
            "docker", "run", "--rm",
            # Match the Dockerfile's amd64 pin so install scripts' x86_64
            # GitHub release downloads work even on Apple Silicon hosts.
            "--platform", "linux/amd64",
            "-v", f"{REPO_ROOT}:/repo:ro",  # repo read-only at /repo; pipeline copies to writable /work
            *mount_args,
            *env_args,
            image,
            "bash", PIPELINE_SCRIPT,
        ],
        capture_output=True, text=True,
        # Wall time budget: pip + npm + uv tool install hermes adds up. The
        # Hermes git fetch + build is the slowest part (~3-5 min on a cold
        # cache).
        timeout=900,
    )


# ---------------------------------------------------------------------------
# Main happy-path test
# ---------------------------------------------------------------------------


def test_pipeline_runs_and_security_fixes_hold(apps_like_image):
    """Full pipeline run in a non-enterprise (default) configuration.

    Asserts every [PASS] line we expect appears, and no [FAIL] lines appear.
    This is the "did anything regress" check — covers F-01 / F-04 / F-05 /
    F-06 / cooldown in one go.
    """
    result = _run_pipeline(apps_like_image)

    # Print captured output unconditionally so CI logs show the pipeline
    # transcript regardless of pass/fail.
    print("\n=== Pipeline stdout ===")
    print(result.stdout)
    print("\n=== Pipeline stderr ===")
    print(result.stderr)

    # The pipeline script exits with verify.sh's exit code. Non-zero means
    # at least one assertion failed in verify.sh.
    if result.returncode != 0:
        pytest.fail(
            f"Pipeline+verify exited with rc={result.returncode}. "
            f"See output above for the [FAIL] lines."
        )

    # Belt-and-braces: explicitly check for each expected PASS marker so
    # we catch the case where verify.sh exits 0 but some checks were
    # silently skipped.
    expected_passes = [
        "F-01 terminal env has no leaked credentials",
        "F-04 Claude MCP wiring",
        "F-05 Hermes config",          # either chmod 0o600 OR skipped — both acceptable
        "F-06 Hermes installed",
        "cooldown opencode stable",
        "cooldown codex stable",
        "cooldown gemini stable",
    ]
    missing = [m for m in expected_passes if m not in result.stdout]
    assert not missing, (
        f"verify.sh did not emit expected [PASS] markers: {missing}. "
        f"Output:\n{result.stdout[-3000:]}"
    )

    # And NO [FAIL] lines anywhere
    assert "[FAIL]" not in result.stdout, (
        f"verify.sh emitted [FAIL] lines:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Enterprise-mode happy path: MCP overrides actually omit servers
# ---------------------------------------------------------------------------


def test_mcp_overrides_omit_servers_when_empty(apps_like_image):
    """When DEEPWIKI_MCP_URL=`` and EXA_MCP_URL=``, the resulting
    ~/.claude.json should have NO mcpServers entries.

    This is the F-04 "documented security control actually works" check
    that the first independent review caught — without this test, a
    regression in setup_claude.py / setup_opencode.py / setup_hermes.py
    that re-hardcodes the URLs would go undetected.
    """
    result = _run_pipeline(
        apps_like_image,
        extra_env={"DEEPWIKI_MCP_URL": "", "EXA_MCP_URL": ""},
    )

    print("\n=== Pipeline stdout (enterprise MCP-override mode) ===")
    print(result.stdout[-3000:])

    if result.returncode != 0:
        pytest.fail(
            f"Pipeline failed in MCP-override mode (rc={result.returncode}). "
            f"See output above."
        )

    # The relevant verify.sh line should report MCP servers were omitted
    assert (
        "F-04 Claude MCP servers omitted when overrides empty" in result.stdout
    ), (
        f"Expected the empty-override branch of F-04 to pass. "
        f"Got:\n{result.stdout[-1500:]}"
    )


# ---------------------------------------------------------------------------
# Defense in depth: validate_mirror_env() rejects shell-injection URLs
# ---------------------------------------------------------------------------


def test_unsafe_mirror_url_rejected_at_bootstrap(apps_like_image):
    """If an operator sets GITHUB_API_BASE to a value containing shell
    metacharacters, bootstrap() should refuse to proceed.

    This is a FAST test — runs bootstrap() in isolation (no pip install, no
    install scripts) so it completes in seconds. Demonstrates the F-03
    rejection happens at the bootstrap entry point, not just in the unit
    tests of `_validate_url` directly.
    """
    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "--platform", "linux/amd64",
            "-v", f"{REPO_ROOT}:/repo:ro",
            "-e", "GITHUB_API_BASE=https://evil.com/`whoami`",
            apps_like_image,
            "bash", "-c",
            # Don't `set -e` so the python error doesn't kill the line
            # before we can capture it.
            'cd /repo && python3 -c "'
            'import sys; sys.path.insert(0, \\".\\"); '
            'import enterprise_config; '
            'enterprise_config.bootstrap()"',
        ],
        capture_output=True, text=True,
        timeout=60,
    )

    print("\n=== Bootstrap stdout ===")
    print(result.stdout)
    print("\n=== Bootstrap stderr ===")
    print(result.stderr)

    combined = result.stdout + result.stderr
    assert "UnsafeUrlError" in combined, (
        f"Expected bootstrap to raise UnsafeUrlError for unsafe GITHUB_API_BASE. "
        f"Got combined output:\n{combined[-2000:]}"
    )
    # And the bootstrap process should have exited non-zero (the exception
    # propagated to top-level, no `except` catches it in the test entry point).
    assert result.returncode != 0, (
        f"Bootstrap should have exited non-zero on rejection. "
        f"Got returncode={result.returncode}"
    )

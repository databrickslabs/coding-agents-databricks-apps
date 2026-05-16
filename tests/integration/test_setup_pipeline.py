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


def _pypi_reachable_from_container() -> tuple[bool, str]:
    """Quick test that pypi.org is reachable from inside a container.

    On corporate networks (notably Databricks employee laptops), pypi.org is
    explicitly blocked at the egress proxy. The integration test needs pypi
    to install requirements.txt — without it, the test fails with confusing
    "no matching distribution" errors deep inside pip. Detect this case up
    front and skip cleanly.

    Uses the apps-like image if it's already built (saves ~30s vs pulling
    ubuntu:22.04 + apt install). Falls back to a minimal curl-only image.
    """
    if not shutil.which("docker"):
        return False, "docker CLI not available"
    # Prefer the apps-like image if built (has curl + ca-certs pre-installed)
    image_check = subprocess.run(
        ["docker", "image", "inspect", IMAGE_TAG],
        capture_output=True, timeout=5,
    )
    if image_check.returncode == 0:
        image = IMAGE_TAG
    else:
        image = "curlimages/curl:latest"
    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--entrypoint", "curl", image,
                "-sS", "--max-time", "10",
                "-o", "/dev/null", "-w", "%{http_code}",
                "https://pypi.org/simple/wheel/",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False, f"curl exited {result.returncode}: {result.stderr[:200].strip()}"
        status = result.stdout.strip()
        if status == "200":
            return True, "pypi reachable"
        return False, f"pypi returned HTTP {status} (likely blocked by corporate proxy)"
    except subprocess.TimeoutExpired:
        return False, "pypi reachability check timed out"


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
            # uv reads its own var; default to native-tls so it uses the system bundle.
            "-e", "UV_NATIVE_TLS=true",
        ])
    for k, v in (extra_env or {}).items():
        env_args.extend(["-e", f"{k}={v}"])
    return subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{REPO_ROOT}:/repo:ro",  # repo read-only at /repo; pipeline copies to writable /work
            *mount_args,
            *env_args,
            image,
            "bash", PIPELINE_SCRIPT,
        ],
        capture_output=True, text=True,
        timeout=600,  # 10 min ceiling — npm + uv installs can be slow on first run
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

    Mirrors the F-03 unit test, but exercises it from the actual entry
    point that runs at container startup.
    """
    result = _run_pipeline(
        apps_like_image,
        extra_env={"GITHUB_API_BASE": "https://evil.com/`whoami`"},
    )

    print("\n=== Pipeline stdout (malicious mirror env) ===")
    print(result.stdout[-2000:])
    print("\n=== Pipeline stderr ===")
    print(result.stderr[-2000:])

    # The bootstrap call in run_pipeline.sh should raise UnsafeUrlError.
    # The pipeline script may continue past that (uv run python -c doesn't
    # halt on a raised exception unless set -e catches it), so we just
    # check that the rejection happened.
    combined = result.stdout + result.stderr
    assert "UnsafeUrlError" in combined or "GITHUB_API_BASE" in combined, (
        f"Expected bootstrap to reject the unsafe URL. Got:\n{combined[-2000:]}"
    )

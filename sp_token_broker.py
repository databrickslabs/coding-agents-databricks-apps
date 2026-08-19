"""Loopback-only broker for short-lived CoDA app service-principal tokens."""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import os
import secrets
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

BROKER_URL_ENV = "CODA_SP_TOKEN_BROKER_URL"
_MAX_TOKEN_BYTES = 16 * 1024
_CAPABILITY_BYTES = 32


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep a validated loopback broker request on its exact capability URL."""

    def redirect_request(self, *_args, **_kwargs):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def urlopen(url, timeout):
    """Open a broker URL without urllib's default redirect following."""
    return _NO_REDIRECT_OPENER.open(url, timeout=timeout)


def mint_sp_token(creds: dict[str, str]) -> str:
    """Mint a fresh M2M bearer without persisting the client secret."""
    from databricks.sdk.core import Config

    headers = Config(
        host=creds["host"],
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        auth_type="oauth-m2m",
    ).authenticate()
    auth = (headers or {}).get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else auth.strip()
    if not token:
        raise RuntimeError("app service principal returned no bearer token")
    return token


class _SPTokenBrokerServer(ThreadingHTTPServer):
    """HTTP server state kept out of the process environment and logs."""

    daemon_threads = True
    allow_reuse_address = False

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout_seconds)
        return request, client_address

    def process_request(self, request, client_address) -> None:
        """Reject excess connections before allocating a handler thread."""
        if not self.handler_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.handler_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        def expire_request() -> None:
            with contextlib.suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)

        deadline = threading.Timer(self.request_timeout_seconds, expire_request)
        deadline.daemon = True
        deadline.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            deadline.cancel()
            self.handler_slots.release()

    def begin_shutdown(self) -> None:
        """Fail closed immediately, before the listener finishes stopping."""
        with self.response_lock:
            self.closing.set()
            self.broker_capability = ""

    def shutdown(self) -> None:
        self.begin_shutdown()
        super().shutdown()

    def server_close(self) -> None:
        self.begin_shutdown()
        super().server_close()


def _valid_broker_url(target: str) -> bool:
    """Accept only capability URLs addressed to the IPv4 loopback listener."""
    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError:
        return False
    path_parts = parsed.path.split("/")
    return bool(
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.username is None
        and parsed.password is None
        and port is not None
        and 0 < port <= 65535
        and parsed.query == ""
        and parsed.fragment == ""
        and len(path_parts) == 3
        and path_parts[:2] == ["", "token"]
        and len(path_parts[2]) >= 32
        and all(char.isalnum() or char in "-_" for char in path_parts[2])
    )


def _decode_token_response(response) -> str | None:
    """Validate the broker response contract without echoing response bytes."""
    if getattr(response, "status", 200) != 200:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    content_type = (headers.get("Content-Type", "") or "").split(";", 1)[0]
    if content_type.strip().lower() != "text/plain":
        return None
    body = response.read(_MAX_TOKEN_BYTES + 1)
    if len(body) > _MAX_TOKEN_BYTES:
        return None
    try:
        token = body.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not token or "\r" in token or "\n" in token:
        return None
    return token


def start_sp_token_broker(
    mint_token: Callable[[], str],
    *,
    port: int = 0,
    max_concurrent_mints: int = 4,
    max_concurrent_handlers: int = 16,
    mint_timeout_seconds: float = 5.0,
    request_timeout_seconds: float = 5.0,
) -> ThreadingHTTPServer:
    """Start a capability-gated daemon server bound only to IPv4 loopback.

    Mint work is bounded by ``max_concurrent_mints``. Each request waits at most
    ``mint_timeout_seconds``; a timed-out daemon worker retains its slot until it
    actually exits, so repeated timeouts cannot create unbounded mint work.
    """
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if max_concurrent_mints < 1:
        raise ValueError("max_concurrent_mints must be positive")
    if max_concurrent_handlers < 1:
        raise ValueError("max_concurrent_handlers must be positive")
    if mint_timeout_seconds <= 0:
        raise ValueError("mint_timeout_seconds must be positive")
    if request_timeout_seconds <= 0:
        raise ValueError("request_timeout_seconds must be positive")

    class Handler(BaseHTTPRequestHandler):
        server: _SPTokenBrokerServer

        def _send(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            self.close_connection = True

        @staticmethod
        def _safe_equal_ascii(candidate: str, expected: str) -> bool:
            try:
                candidate_bytes = candidate.encode("ascii")
                expected_bytes = expected.encode("ascii")
            except (UnicodeEncodeError, AttributeError):
                return False
            return hmac.compare_digest(candidate_bytes, expected_bytes)

        def _authorized(self) -> bool:
            if self.server.closing.is_set():
                return False
            capability = self.server.broker_capability
            if not capability:
                return False
            try:
                source = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            if source != ipaddress.ip_address("127.0.0.1"):
                return False
            hosts = self.headers.get_all("Host", failobj=[])
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if len(hosts) != 1 or not self._safe_equal_ascii(hosts[0], expected_host):
                return False
            if self.headers.get("Transfer-Encoding") is not None:
                return False
            content_lengths = self.headers.get_all("Content-Length", failobj=[])
            if len(content_lengths) > 1:
                return False
            try:
                content_length = int(content_lengths[0]) if content_lengths else 0
            except ValueError:
                return False
            if content_length != 0:
                return False
            expected_path = f"/token/{capability}"
            return self._safe_equal_ascii(self.path, expected_path)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            if not self._authorized():
                self._send(404)
                return
            if not self.server.mint_slots.acquire(blocking=False):
                self._send(503)
                return

            completed = threading.Event()
            result: dict[str, object] = {}

            def run_mint() -> None:
                try:
                    result["token"] = mint_token()
                except Exception:
                    result["failed"] = True
                finally:
                    self.server.mint_slots.release()
                    completed.set()

            threading.Thread(
                target=run_mint,
                daemon=True,
                name="sp-token-mint",
            ).start()
            if not completed.wait(self.server.mint_timeout_seconds):
                self._send(503)
                return
            if self.server.closing.is_set():
                self._send(503)
                return
            token = result.get("token")
            if not isinstance(token, str):
                self._send(503)
                return
            try:
                body = token.encode("utf-8")
            except UnicodeEncodeError:
                self._send(503)
                return
            if (
                not body
                or len(body) > _MAX_TOKEN_BYTES
                or b"\r" in body
                or b"\n" in body
            ):
                self._send(503)
                return
            # Linearize token delivery with capability invalidation. Shutdown
            # takes the same lock before setting ``closing``, so there is no
            # post-check/pre-write window in which a bearer can escape.
            with self.server.response_lock:
                if self.server.closing.is_set():
                    self._send(503)
                    return
                self._send(200, body)

        def _reject_method(self) -> None:
            self._send(405)

        do_HEAD = _reject_method  # noqa: N815 - BaseHTTPRequestHandler API
        do_POST = _reject_method  # noqa: N815 - BaseHTTPRequestHandler API
        do_PUT = _reject_method  # noqa: N815 - BaseHTTPRequestHandler API
        do_PATCH = _reject_method  # noqa: N815 - BaseHTTPRequestHandler API
        do_DELETE = _reject_method  # noqa: N815 - BaseHTTPRequestHandler API
        do_OPTIONS = _reject_method  # noqa: N815 - BaseHTTPRequestHandler API

        def log_message(self, _format, *_args):
            return

    server = _SPTokenBrokerServer(("127.0.0.1", port), Handler)
    server.broker_capability = secrets.token_urlsafe(_CAPABILITY_BYTES)
    server.closing = threading.Event()
    server.response_lock = threading.Lock()
    server.mint_slots = threading.BoundedSemaphore(max_concurrent_mints)
    server.handler_slots = threading.BoundedSemaphore(max_concurrent_handlers)
    server.mint_timeout_seconds = mint_timeout_seconds
    server.request_timeout_seconds = request_timeout_seconds
    serve_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="sp-token-broker",
    )
    server.serve_thread = serve_thread
    serve_thread.start()
    return server


def stop_sp_token_broker(server: ThreadingHTTPServer | None) -> None:
    """Invalidate and close a broker without exposing its capability."""
    if server is None:
        return
    begin_shutdown = getattr(server, "begin_shutdown", None)
    if callable(begin_shutdown):
        begin_shutdown()
    server.shutdown()
    server.server_close()
    serve_thread = getattr(server, "serve_thread", None)
    if (
        isinstance(serve_thread, threading.Thread)
        and serve_thread is not threading.current_thread()
    ):
        serve_thread.join(timeout=2)


def broker_url(server: ThreadingHTTPServer) -> str:
    """Return the unguessable token URL for a running broker."""
    host, port = server.server_address[:2]
    capability = getattr(server, "broker_capability", "")
    if not capability:
        raise RuntimeError("SP token broker is closed")
    return f"http://{host}:{port}/token/{capability}"


def fetch_sp_token(url: str | None = None) -> str | None:
    """Fetch a fresh bearer from a validated loopback capability URL."""
    target = (url or os.environ.get(BROKER_URL_ENV, "")).strip()
    if not target or not _valid_broker_url(target):
        return None
    try:
        with urlopen(target, timeout=5) as response:
            return _decode_token_response(response)
    except Exception:
        return None

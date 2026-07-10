import os

bind = f"0.0.0.0:{os.environ.get('DATABRICKS_APP_PORT', '8000')}"
workers = 1          # PTY fds + sessions dict are process-local
threads = 32         # Concurrent request handling (poll + input + resize + websocket).
                     # ~1.6 threads per session at MAX_CONCURRENT_SESSIONS=20, plus
                     # slack for health/setup-status. I/O-bound work (PTY reads, WS
                     # sends) so gthreads give real concurrency despite the GIL.
worker_class = "gthread"
timeout = 60         # WebSocket connections are long-lived; balance between WS and hung-worker detection
graceful_timeout = 10  # Databricks gives 15s after SIGTERM
accesslog = "-"
errorlog = "-"
loglevel = "info"


def post_worker_init(worker):
    from app import initialize_app
    initialize_app()

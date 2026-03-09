import os

bind = f"0.0.0.0:{os.environ.get('DATABRICKS_APP_PORT', '8000')}"
workers = 1          # PTY fds + sessions dict are process-local
threads = 16         # Concurrent request handling (poll + input + resize + websocket)
worker_class = "gthread"
timeout = 60         # WebSocket connections are long-lived; balance between WS and hung-worker detection
graceful_timeout = 10  # Databricks gives 15s after SIGTERM
accesslog = "-"
errorlog = "-"
loglevel = "info"


def post_worker_init(worker):
    from app import initialize_app
    initialize_app()

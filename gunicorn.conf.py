import os

bind = f"0.0.0.0:{os.environ.get('DATABRICKS_APP_PORT', '8000')}"
workers = 1          # PTY fds + sessions dict are process-local
threads = 8          # Concurrent request handling (poll + input + resize)
worker_class = "gthread"
timeout = 30
graceful_timeout = 10  # Databricks gives 15s after SIGTERM
accesslog = "-"
errorlog = "-"
loglevel = "info"


def post_worker_init(worker):
    from app import initialize_app
    initialize_app()

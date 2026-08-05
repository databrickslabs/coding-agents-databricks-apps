import os
import faulthandler
import sys

# Dump C-level tracebacks (segfaults, aborts in native extensions under thread
# pressure) to stderr, which the Databricks Apps log captures. Without this a
# native fault kills the single worker with NO Python traceback — the exact
# "App exited unexpectedly" with an empty log we could not diagnose.
faulthandler.enable(file=sys.stderr, all_threads=True)

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


# ── Worker lifecycle breadcrumbs ─────────────────────────────────────────────
# With workers=1, losing the worker == losing the app. These hooks make every
# death path visible in the log so "exited unexpectedly" is never a mystery:
#   - worker_int : master sent the worker SIGINT/SIGQUIT (graceful/timeout)
#   - worker_abort: master killed a HUNG worker after `timeout` (SIGABRT) — the
#                   classic silent death under load when a request/thread wedges
#   - child_exit : the worker process exited; exitcode reveals OOM (-9/SIGKILL)

def worker_int(worker):
    worker.log.warning("WORKER LIFECYCLE: worker %s got SIGINT/SIGQUIT "
                       "(graceful stop or timeout)", worker.pid)


def worker_abort(worker):
    # Fires when the arbiter kills a worker that blew past `timeout`.
    import faulthandler as _fh
    worker.log.error("WORKER LIFECYCLE: worker %s ABORTED — exceeded timeout=%ss "
                     "(hung request/thread). Dumping all thread stacks:",
                     worker.pid, timeout)
    try:
        _fh.dump_traceback(file=sys.stderr, all_threads=True)
    except Exception:
        pass


def child_exit(server, worker):
    # exitcode < 0 => killed by signal N == -exitcode. -9 == SIGKILL (OOM /
    # platform reap); -6 == SIGABRT (timeout); -11 == SIGSEGV (native crash).
    code = getattr(worker, "exitcode", None)
    server.log.error("WORKER LIFECYCLE: child %s exited (exitcode=%s%s)",
                     worker.pid, code,
                     " == killed by SIGKILL/OOM" if code == -9 else
                     " == killed by SIGSEGV (native crash)" if code == -11 else
                     " == killed by SIGABRT (timeout)" if code == -6 else "")

#!/usr/bin/env python3
"""
start-worker-io.py

Safe startup wrapper for the IO Celery worker.
- Ensures environment defaults for tracing and PYTHONPATH
- Creates REPORT_PATH to avoid race conditions when workers write reports
- Attempts to import SESSION_MAKER_IMPORT early (best-effort) so failures surface at startup
- Warns if SOCKETIO_MESSAGE_QUEUE is not configured (workers emit socket events)
- Applies eventlet monkeypatch with guarded error handling when WORKER_POOL=eventlet
- Starts Celery using celery.__main__.main (reads sys.argv)
"""

# Decide pool strategy early so we can conditionally monkeypatch before importing modules
import os
import sys

# Decide pool from explicit CLI args first, then fall back to env WORKER_POOL
_work_pool = ""
# inspect argv for explicit --pool=VALUE tokens (docker compose command args are visible here)
for i, a in enumerate(sys.argv[1:], start=1):
    if not a:
        continue
    if a.startswith("--pool="):
        try:
            _work_pool = a.split("=", 1)[1].lower()
        except Exception:
            _work_pool = a[len("--pool="):].lower()
        break
    # handle the two-token form: --pool <value>
    if a == "--pool" and i < len(sys.argv) - 1:
        try:
            _work_pool = sys.argv[i + 1].lower()
        except Exception:
            _work_pool = ""
        break
# fallback to environment if CLI did not specify pool
if not _work_pool:
    _work_pool = os.environ.get("WORKER_POOL", "").lower()
_WORKER_POOL = _work_pool

if _WORKER_POOL == "eventlet":
    try:
        import eventlet
        try:
            eventlet.monkey_patch()
            # Use simple stdout message to make startup logs obvious in container output
            sys.stdout.write("eventlet monkeypatched (in-process)\n")
            sys.stdout.flush()

            # --- Minimal targeted change: replace os.write with eventlet's green os.write ---
            # This prevents Celery safe_say -> os.write(sys.__stdout__) from triggering
            # eventlet's "do not call blocking functions from the mainloop" RuntimeError.
            try:
                from eventlet.green import os as _green_os
                import os as _orig_os
                _orig_os.write = _green_os.write  # type: ignore
                sys.stdout.write("patched os.write to eventlet.green.os.write\n")
                sys.stdout.flush()
            except Exception:
                # If patch fails, keep going; we still have monkey_patch applied
                sys.stderr.write("failed to patch os.write to eventlet.green.os.write (non-fatal)\n")
                try:
                    import traceback as _tb
                    _tb.print_exc(file=sys.stderr)
                except Exception:
                    pass
            # --- end targeted patch ---

        except Exception:
            # If monkey_patch itself fails, write to stderr but continue so we can surface later errors.
            import traceback as _traceback, sys as _sys
            _sys.stderr.write("eventlet.monkey_patch() failed:\n")
            _traceback.print_exc(file=_sys.stderr)
    except Exception:
        # If eventlet is not available, write a clear message and continue.
        try:
            import traceback as _traceback, sys as _sys
            _sys.stderr.write("eventlet import failed; worker may not run with eventlet pool\n")
            _traceback.print_exc(file=_sys.stderr)
        except Exception:
            pass

# Now safe to import the remaining standard modules and configure logging.
import logging
import importlib

# Basic logging to stdout/stderr for container logs
logging.basicConfig(level=os.environ.get("STARTUP_LOG_LEVEL", "INFO"))
logger = logging.getLogger("start-worker-io")

# Safety: keep ddtrace disabled for this process unless explicitly enabled
os.environ.setdefault("DD_APM_ENABLED", "false")
os.environ.setdefault("DD_TRACE_ENABLED", "false")
os.environ.setdefault("DD_TRACE_EVENTLET_ENABLED", "false")

# Ensure PYTHONPATH points to project root so imports like "tasks" and "api.*" resolve
os.environ.setdefault("PYTHONPATH", os.environ.get("PROJECT_ROOT", "/app"))

# Ensure REPORT_PATH exists so workers can write/read report files reliably
report_path = os.environ.get("REPORT_PATH", "/app/instance/reports")
try:
    os.makedirs(report_path, exist_ok=True)
    logger.info("Ensured REPORT_PATH exists: %s", report_path)
except Exception:
    logger.exception("Failed ensuring REPORT_PATH exists: %s", report_path)

# Best-effort import of SESSION_MAKER_IMPORT if provided so issues show at startup
sess_import_path = os.environ.get("SESSION_MAKER_IMPORT")
if sess_import_path:
    try:
        module_path, attr = sess_import_path.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        SESSION_MAKER = getattr(mod, attr)
        logger.info("Imported SESSION_MAKER from %s", sess_import_path)
    except Exception:
        logger.exception("Failed to import SESSION_MAKER_IMPORT=%s (non-fatal)", sess_import_path)
        SESSION_MAKER = None
else:
    SESSION_MAKER = None

# Warn if SOCKETIO_MESSAGE_QUEUE not configured; workers may emit socket events via message queue
if not os.environ.get("SOCKETIO_MESSAGE_QUEUE"):
    logger.warning("SOCKETIO_MESSAGE_QUEUE is not set; Socket.IO emits from workers may not reach web clients")

# Prepare argv for Celery. If no args passed, provide sensible defaults.
if len(sys.argv) == 1:
    # Mirror previous defaults: IO worker with eventlet pool and named queue
    # Keep existing default behavior but respect WORKER_POOL when launched without explicit args
    if _WORKER_POOL == "eventlet":
        sys.argv = ["celery", "-A", "tasks", "worker", "--loglevel=info", "--concurrency=4", "-Q", "io", "--pool=eventlet"]
    else:
        sys.argv = ["celery", "-A", "tasks", "worker", "--loglevel=info", "--concurrency=4", "-Q", "io"]
    logger.info("No args provided; using default Celery argv: %s", sys.argv)

# Import and run celery main (it reads sys.argv)
try:
    from celery.__main__ import main as celery_main
    celery_main()
except Exception:
    logger.exception("Failed to start Celery worker")
    # Ensure process exits non-zero on failure so orchestrators notice
    sys.exit(2)

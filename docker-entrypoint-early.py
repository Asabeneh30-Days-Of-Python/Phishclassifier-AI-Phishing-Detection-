# docker-entrypoint-early.py
# Early bootstrap executed by start-worker-wait.sh. Runs before Celery starts.
import sys

# Apply eventlet monkeypatch early (start-worker-wait.sh already decided WORKER_POOL)
try:
    import eventlet
    eventlet.monkey_patch()
    sys.stdout.write("eventlet monkeypatched (in-process)\n")
    sys.stdout.flush()
except Exception:
    try:
        import traceback as _tb
        sys.stderr.write("eventlet import/monkey_patch failed (non-fatal)\n")
        _tb.print_exc(file=sys.stderr)
    except Exception:
        pass

# Patch os.write to eventlet green implementation to avoid RuntimeError
# when Celery calls safe_say -> os.write(...) from inside eventlet hub.
try:
    from eventlet.green import os as _green_os
    import os as _orig_os
    _orig_os.write = _green_os.write  # type: ignore
    sys.stdout.write("patched os.write to eventlet.green.os.write\n")
    sys.stdout.flush()
except Exception:
    try:
        import traceback as _tb
        sys.stderr.write("failed to patch os.write to eventlet.green.os.write (non-fatal)\n")
        _tb.print_exc(file=sys.stderr)
    except Exception:
        pass

# Exec the real Python bootstrap in the same process so patched behavior applies
import runpy
runpy.run_path('/app/start-worker-io.py', run_name='__main__')

# tasks.py

#tasks.py

"""
Celery tasks module (root-level).

This file configures Celery and provides the project tasks used by workers and
celery-beat. It is written to be robust when imported both inside and outside
a Flask app context (workers, CLI, tests). Key features:

- Configures Celery broker and result backend from environment.
- Lazy Flask app access for tasks that need app_context.
- Optional integration with socketio for emitting task events from workers.
- Best-effort TaskAudit / TaskLog persistence that works with a callable
  SESSION_MAKER or falls back to Flask-SQLAlchemy session.
- Wrapper decorator that updates TaskAudit rows, appends logs and emits socket
  events during task lifecycle.
- Tasks:
  - reports.generate_report_task
  - reports.purge_old_reports
  - classify_email_task (coordinator) with sync option
  - classify_email_task_heavy (heavy worker)
  - retrain_model_task (stub)
- Exposes BEAT_SCHEDULE snippet to merge with project configuration.
"""
import os
import json
import traceback
import logging
import tempfile
import shutil
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from celery import Celery, current_task

logger = logging.getLogger("phishclassifier")

# ---------------------------------------------------------------------------
# Shared debug logger that writes to file inside container for end-to-end traces
# ---------------------------------------------------------------------------
# Writes to /app/logs/app-debug.log by default. Set APP_LOG_DIR to override.
LOG_DIR = os.environ.get("APP_LOG_DIR", "/app/logs")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    LOG_DIR = "/tmp"
APP_DEBUG_LOG_PATH = os.path.join(LOG_DIR, "app-debug.log")

app_debug_logger = logging.getLogger("phishclassifier.debug")
if not app_debug_logger.handlers:
    try:
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(APP_DEBUG_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s"))
        app_debug_logger.addHandler(handler)
        app_debug_logger.setLevel(logging.DEBUG)
        app_debug_logger.propagate = False
    except Exception:
        # fallback to standard logger if file handler cannot be created
        app_debug_logger = logging.getLogger("phishclassifier.debug.fallback")
# ---------------------------------------------------------------------------

# Celery configuration
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)

celery = Celery("phishclassifier", broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND)
celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
)

# Try to import create_app but avoid calling it at import time in workers to
# prevent expensive side-effects; _get_app will call create_app lazily when needed.
_app = None
_create_app_callable = None
try:
    from api.app import create_app  # type: ignore

    _create_app_callable = create_app
except Exception:
    _create_app_callable = None


def _get_app():
    """
    Lazily obtain a Flask app instance for runtime use. Return None if it can't
    be created.
    """
    global _app, _create_app_callable
    if _app is not None:
        return _app
    try:
        if _create_app_callable is not None:
            _app = _create_app_callable()
            return _app
        # Fallback: try importing create_app (if not available earlier)
        try:
            from api.app import create_app as _ca  # type: ignore

            _create_app_callable = _ca
            _app = _create_app_callable()
            return _app
        except Exception:
            return None
    except Exception:
        return None


# Merge optional beat schedule if provided at project root
try:
    import celerybeat_schedule  # optional module at project root

    existing = getattr(celery.conf, "beat_schedule", {}) or {}
    merged = {**existing, **celerybeat_schedule.BEAT_SCHEDULE}
    celery.conf.beat_schedule = merged
except Exception:
    pass

# SocketIO (optional) for emitting task updates/logs from workers
try:
    from api.app import socketio  # type: ignore
except Exception:
    socketio = None

# Optional TaskAudit models
try:
    from models.task_audit import TaskAudit, TaskLog  # type: ignore
except Exception:
    TaskAudit = None
    TaskLog = None

# SESSION_MAKER resolution:
# If env var SESSION_MAKER_IMPORT provided, import it. Otherwise fallback to
# returning Flask-SQLAlchemy session via api.database.db.session.
SESSION_MAKER = None
try:
    sess_path = os.environ.get("SESSION_MAKER_IMPORT")
    if sess_path:
        module_path, attr = sess_path.rsplit(".", 1)
        m = __import__(module_path, fromlist=[attr])
        SESSION_MAKER = getattr(m, attr)
except Exception:
    SESSION_MAKER = None


def _session_maker_fallback():
    """
    Fallback session maker: returns a callable that returns a Flask-SQLAlchemy
    session when api.database.db is importable, otherwise returns a callable
    that returns None.

    This fallback function is marked with an attribute so callers can detect
    that it returns the shared Flask-SQLAlchemy session (and therefore should
    not attempt to close it).
    """
    try:
        from api.database import db as _db  # type: ignore

        def _make():
            return _db.session

        # Mark as shared/scoped so callers can decide not to close(). 
        setattr(_make, "_is_shared_flask_session", True)
        return _make
    except Exception:
        return lambda: None


# Ensure we have a callable that returns a session (or None)
if not callable(SESSION_MAKER):
    SESSION_MAKER = _session_maker_fallback()


def _acquire_session() -> Tuple[Optional[Any], bool]:
    """
    Helper to consistently acquire a DB session from SESSION_MAKER.
    Returns (session, created_locally_boolean). created_locally is True when
    we should attempt to close() the session after use.

    The function detects whether SESSION_MAKER is the fallback that returns the
    shared Flask-SQLAlchemy session (in which case created_locally=False) or a
    per-call session factory (created_locally=True).
    """
    try:
        if not callable(SESSION_MAKER):
            sess = SESSION_MAKER
            # unknown; be conservative and mark not created locally
            return (sess, False) if sess is not None else (None, False)

        # If the SESSION_MAKER function has a marker indicating it's the fallback,
        # treat returned session as shared (do not close).
        is_fallback = getattr(SESSION_MAKER, "_is_shared_flask_session", False)
        try:
            sess = SESSION_MAKER()
        except Exception:
            return None, False
        if sess is None:
            return None, False
        return (sess, not is_fallback)
    except Exception:
        return None, False


def _safe_close_session(session):
    """
    Close session if it appears to be a regular session with a close method.
    Avoid closing scoped sessions managed by app frameworks unless safe.
    """
    try:
        close_fn = getattr(session, "close", None)
        if callable(close_fn):
            close_fn()
    except Exception:
        pass


SOCKET_NAMESPACE = "/reports"


def _emit_socket_event(event: str, payload: Dict[str, Any], namespace: str = SOCKET_NAMESPACE):
    try:
        if socketio:
            socketio.emit(event, payload, namespace=namespace)
    except Exception:
        # Never fail the task due to socket emission issues
        logger.debug("socket emit failed", exc_info=True)


def _get_report_dir_for_tasks() -> str:
    """
    Resolve a directory for per-task logfile writes for workers.
    Use REPORT_PATH env or fallback; ensure directory exists.
    """
    report_dir = os.environ.get("REPORT_PATH", "/tmp/phish_reports")
    try:
        os.makedirs(report_dir, exist_ok=True)
    except Exception:
        try:
            logger.debug("Could not ensure report_dir exists: %s", report_dir, exc_info=True)
        except Exception:
            pass
    return report_dir


def _durable_append_line(path: str, line: str) -> None:
    """
    Best-effort durable append: open file in append mode, write, flush and fsync.
    Does not raise; errors are logged/ignored to preserve best-effort semantics.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
    except Exception:
        try:
            logger.debug("durable append failed for %s", path, exc_info=True)
        except Exception:
            pass


def _append_to_task_logfile(audit_id: str, ts_iso: str, level: str, message: str):
    """
    Append a plain-text line to REPORT_PATH/<audit_id>.log for durable per-task logs.
    This is intentionally minimal and best-effort: failures are swallowed.
    """
    try:
        if not audit_id:
            return
        report_dir = _get_report_dir_for_tasks()
        fname = os.path.join(report_dir, f"{audit_id}.log")
        line = f"[{ts_iso}] {level} {message}\n"
        try:
            _durable_append_line(fname, line)
            try:
                os.chmod(fname, 0o600)
            except Exception:
                pass
        except Exception:
            try:
                logger.debug("Failed to append to logfile %s", fname, exc_info=True)
            except Exception:
                pass
    except Exception:
        try:
            logger.exception("append_to_task_logfile failed")
        except Exception:
            pass


def _append_to_persistence_log(persistence_id, ts_iso, level, message):
    """
    Append a plain-text line to REPORT_PATH/<persistence_id>.log for durable per-persistence logs.
    Best-effort and idempotent.
    """
    try:
        if persistence_id is None:
            return
        report_dir = _get_report_dir_for_tasks()
        fname = os.path.join(report_dir, f"{persistence_id}.log")
        line = f"[{ts_iso}] {level} {message}\n"
        try:
            _durable_append_line(fname, line)
            try:
                os.chmod(fname, 0o600)
            except Exception:
                pass
        except Exception:
            try:
                logger.debug("Failed to append to persistence logfile %s", fname, exc_info=True)
            except Exception:
                pass
    except Exception:
        try:
            logger.exception("append_to_persistence_log failed")
        except Exception:
            pass


def _persist_task_audit_event(session_maker, task_audit_id: Optional[str], level: str, message: str, meta: Optional[Dict[str, Any]] = None):
    """
    Best-effort: append a TaskLog row linked to TaskAudit.
    - session_maker: callable returning a session
    - task_audit_id: optional audit id to use as a lookup
    """
    try:
        if TaskLog is None or TaskAudit is None:
            # Still append to per-task logfile if possible using celery task id
            try:
                tid = getattr(getattr(current_task, "request", None), "id", None)
                ts_iso = datetime.utcnow().isoformat()
                if tid:
                    meta_str = ""
                    try:
                        if meta is not None:
                            meta_str = " " + json.dumps(meta, default=str)
                    except Exception:
                        meta_str = ""
                    _append_to_task_logfile(tid, ts_iso, level, f"{message}{meta_str}")
            except Exception:
                pass
            return
        # Prefer running under Flask app context if available
        app = _get_app()
        if app is not None:
            with app.app_context():
                db_session, created_locally = _acquire_session()
                if not db_session:
                    return
                try:
                    ta = None
                    if task_audit_id:
                        ta = db_session.query(TaskAudit).filter_by(id=task_audit_id).first()
                    if ta is None:
                        # try to match by celery task id
                        tid = getattr(getattr(current_task, "request", None), "id", None) if current_task else None
                        if tid:
                            ta = db_session.query(TaskAudit).filter_by(task_id=tid).first()
                    if ta:
                        tl = TaskLog(task_audit_id=ta.id, level=level, message=message, meta=meta)
                        db_session.add(tl)
                        try:
                            db_session.commit()
                        except Exception:
                            try:
                                db_session.flush()
                            except Exception:
                                pass
                        # emit socket event
                        try:
                            ts_iso = getattr(tl, "ts", None).isoformat() if getattr(tl, "ts", None) else datetime.utcnow().isoformat()
                            _emit_socket_event(
                                "task:log",
                                {
                                    "task_id": ta.task_id or ta.id,
                                    "audit_id": ta.id,
                                    "ts": ts_iso,
                                    "level": tl.level,
                                    "message": tl.message,
                                    "meta": tl.meta,
                                },
                            )
                            # Also append to per-task logfile for durable download/fallback
                            _append_to_task_logfile(str(ta.id), ts_iso, tl.level, tl.message)
                        except Exception:
                            pass
                finally:
                    # Close only if acquired a per-call session
                    if created_locally:
                        _safe_close_session(db_session)
        else:
            # No Flask app context: best-effort with session_maker directly
            db_session = None
            try:
                db_session = session_maker() if callable(session_maker) else session_maker
            except Exception:
                db_session = None
            if not db_session:
                return
            try:
                ta = None
                if task_audit_id:
                    ta = db_session.query(TaskAudit).filter_by(id=task_audit_id).first()
                if ta is None:
                    tid = getattr(getattr(current_task, "request", None), "id", None) if current_task else None
                    if tid:
                        ta = db_session.query(TaskAudit).filter_by(task_id=tid).first()
                if ta:
                    tl = TaskLog(task_audit_id=ta.id, level=level, message=message, meta=meta)
                    db_session.add(tl)
                    try:
                        db_session.commit()
                    except Exception:
                        try:
                            db_session.flush()
                        except Exception:
                            pass
                    try:
                        ts_iso = getattr(tl, "ts", None).isoformat() if getattr(tl, "ts", None) else datetime.utcnow().isoformat()
                        _emit_socket_event(
                            "task:log",
                            {
                                "task_id": ta.task_id or ta.id,
                                "audit_id": ta.id,
                                "ts": ts_iso,
                                "level": tl.level,
                                "message": tl.message,
                                "meta": tl.meta,
                            },
                        )
                        # Also append to per-task logfile for durable download/fallback
                        _append_to_task_logfile(str(ta.id), ts_iso, tl.level, tl.message)
                    except Exception:
                        pass
            finally:
                # best-effort close if session maker produced a per-call session
                created = True
                try:
                    created = not getattr(SESSION_MAKER, "_is_shared_flask_session", False)
                except Exception:
                    created = True
                if created:
                    _safe_close_session(db_session)
    except Exception:
        # avoid failing tasks due to audit/log side-effects
        try:
            logger.exception("task_audit_log_failed")
        except Exception:
            pass


def _ensure_task_audit_row(session_maker, *, audit_id: Optional[str], task_id: Optional[str], task_type: str, user: Optional[str], payload: Optional[Dict[str, Any]]):
    """
    Ensure a TaskAudit row exists for the given audit_id or task_id. Returns the
    TaskAudit.id (possibly the DB-generated id).
    This function is best-effort; failures will be logged and a None may be returned.
    """
    try:
        if TaskAudit is None:
            return audit_id
        app = _get_app()
        if app is not None:
            with app.app_context():
                db_session, created_locally = _acquire_session()
                if not db_session:
                    return audit_id
                try:
                    ta = None
                    if audit_id:
                        ta = db_session.query(TaskAudit).filter_by(id=audit_id).first()
                    if ta is None and task_id:
                        ta = db_session.query(TaskAudit).filter_by(task_id=task_id).first()
                    if ta is None:
                        # omit id when audit_id falsy to let DB generate primary key
                        if audit_id:
                            ta = TaskAudit(id=audit_id, task_id=task_id, task_type=task_type, user=user, payload=payload or {}, status="pending")
                        else:
                            ta = TaskAudit(task_id=task_id, task_type=task_type, user=user, payload=payload or {}, status="pending")
                        db_session.add(ta)
                        try:
                            db_session.commit()
                        except Exception:
                            try:
                                db_session.flush()
                            except Exception:
                                pass
                    else:
                        changed = False
                        if ta.task_type != task_type:
                            ta.task_type = task_type
                            changed = True
                        if user and ta.user != user:
                            ta.user = user
                            changed = True
                        if payload and (ta.payload != payload):
                            ta.payload = payload
                            changed = True
                        if changed:
                            db_session.add(ta)
                            try:
                                db_session.commit()
                            except Exception:
                                try:
                                    db_session.flush()
                                except Exception:
                                    pass
                    return ta.id
                finally:
                    if created_locally:
                        _safe_close_session(db_session)
        else:
            # No Flask app context: best-effort without app context
            db_session = None
            try:
                db_session = session_maker() if callable(session_maker) else session_maker
            except Exception:
                db_session = None
            if not db_session:
                return audit_id
            try:
                ta = None
                if audit_id:
                    ta = db_session.query(TaskAudit).filter_by(id=audit_id).first()
                if ta is None and task_id:
                    ta = db_session.query(TaskAudit).filter_by(task_id=task_id).first()
                if ta is None:
                    if audit_id:
                        ta = TaskAudit(id=audit_id, task_id=task_id, task_type=task_type, user=user, payload=payload or {}, status="pending")
                    else:
                        ta = TaskAudit(task_id=task_id, task_type=task_type, user=user, payload=payload or {}, status="pending")
                    db_session.add(ta)
                    try:
                        db_session.commit()
                    except Exception:
                        try:
                            db_session.flush()
                        except Exception:
                            pass
                else:
                    changed = False
                    if ta.task_type != task_type:
                        ta.task_type = task_type
                        changed = True
                    if user and ta.user != user:
                        ta.user = user
                        changed = True
                    if payload and (ta.payload != payload):
                        ta.payload = payload
                        changed = True
                    if changed:
                        db_session.add(ta)
                        try:
                            db_session.commit()
                        except Exception:
                            try:
                                db_session.flush()
                            except Exception:
                                pass
                return ta.id
            finally:
                # best-effort close if session maker produced a per-call session
                created = True
                try:
                    created = not getattr(SESSION_MAKER, "_is_shared_flask_session", False)
                except Exception:
                    created = True
                if created:
                    _safe_close_session(db_session)
    except Exception:
        try:
            logger.exception("ensure_task_audit_row failed")
        except Exception:
            pass
        return audit_id


def _emit_task_created(audit_id: Optional[str], payload: Dict[str, Any]):
    """
    Emit a compact task:created event via socketio when available.
    Payload should be a small dict containing task_id, task_type, user, summary.
    """
    try:
        p = dict(payload)
        if "created_at" not in p:
            p["created_at"] = datetime.utcnow().isoformat()
        if audit_id:
            p["audit_id"] = audit_id
        _emit_socket_event("task:created", p)
    except Exception:
        pass


def _emit_task_update(audit_id: Optional[str], payload: Dict[str, Any]):
    try:
        p = dict(payload)
        if audit_id:
            p["audit_id"] = audit_id
        _emit_socket_event("task:updated", p)
    except Exception:
        pass


def _update_task_status(audit_id: Optional[str], status: str, started_at=None, finished_at=None, result_meta=None):
    """
    Update TaskAudit row fields (best-effort) and emit status update.
    """
    try:
        if TaskAudit is not None and audit_id:
            app = _get_app()
            if app is not None:
                with app.app_context():
                    db_session, created_locally = _acquire_session()
                    if db_session:
                        try:
                            ta = db_session.query(TaskAudit).filter_by(id=audit_id).first()
                            if ta:
                                ta.status = status
                                if started_at:
                                    ta.started_at = started_at
                                if finished_at:
                                    ta.finished_at = finished_at
                                if result_meta is not None:
                                    try:
                                        if isinstance(result_meta, str):
                                            rm = json.loads(result_meta)
                                        else:
                                            rm = result_meta
                                    except Exception:
                                        rm = result_meta if isinstance(result_meta, dict) else {}
                                    ta.result_meta = rm
                                    try:
                                        pid = rm.get("persistence_id")
                                        if pid is not None and str(pid).isdigit():
                                            ta.persistence_id = int(pid)
                                    except Exception:
                                        pass
                                db_session.add(ta)
                                try:
                                    db_session.commit()
                                except Exception:
                                    try:
                                        db_session.flush()
                                    except Exception:
                                        pass
                        finally:
                            if created_locally:
                                _safe_close_session(db_session)
            else:
                # No Flask app context: best-effort without app context using SESSION_MAKER
                db_session = None
                try:
                    db_session = SESSION_MAKER() if callable(SESSION_MAKER) else SESSION_MAKER
                except Exception:
                    db_session = None
                if db_session:
                    try:
                        ta = db_session.query(TaskAudit).filter_by(id=audit_id).first()
                        if ta:
                            ta.status = status
                            if started_at:
                                ta.started_at = started_at
                            if finished_at:
                                ta.finished_at = finished_at
                            if result_meta is not None:
                                try:
                                    if isinstance(result_meta, str):
                                        rm = json.loads(result_meta)
                                    else:
                                        rm = result_meta
                                except Exception:
                                    rm = result_meta if isinstance(result_meta, dict) else {}
                                ta.result_meta = rm
                                try:
                                    pid = rm.get("persistence_id")
                                    if pid is not None and str(pid).isdigit():
                                        ta.persistence_id = int(pid)
                                except Exception:
                                    pass
                            db_session.add(ta)
                            try:
                                db_session.commit()
                            except Exception:
                                try:
                                    db_session.flush()
                                except Exception:
                                    pass
                    except Exception:
                        try:
                            # defensive: ensure any DB errors are logged and do not leave the function in a broken state
                            logger.exception("tasks._update_task_status: fallback DB session block failed")
                            try:
                                db_session.rollback()
                            except Exception:
                                pass
                        except Exception:
                            pass
                    finally:
                        # Best-effort close if session maker produced a per-call session
                        created = True
                        try:
                            created = not getattr(SESSION_MAKER, "_is_shared_flask_session", False)
                        except Exception:
                            created = True
                        if created:
                            _safe_close_session(db_session)
    except Exception:
        pass

    # Emit to connected socket clients
    try:
        out = {
            "status": status,
            "started_at": (started_at.isoformat() if started_at else None),
            "finished_at": (finished_at.isoformat() if finished_at else None),
            "result_meta": result_meta,
        }
        _emit_task_update(audit_id, out)
    except Exception:
        pass


def _safe_append_log(audit_id: Optional[str], level: str, message: str, meta: Optional[Dict[str, Any]] = None):
    """
    Append to TaskLog (if available) and also append to per-task logfile using
    the Celery task id so the saved log file mirrors worker_cpu console output.

    - If TaskAudit exists and audit_id resolves, the DB TaskLog is created.
    - Regardless, we append a line to REPORT_PATH/<task_id>.log where task_id is
    the Celery request id (preferred) or the audit_id when available.
    - If meta is provided, it is JSON-serialized on the file line for readability.
    """
    try:
        _persist_task_audit_event(SESSION_MAKER, audit_id, level, message, meta)
    except Exception:
        try:
            logger.exception("persist log failed")
        except Exception:
            pass
    # Always also append to per-task logfile using celery request id when possible
    try:
        tid = getattr(getattr(current_task, "request", None), "id", None)
    except Exception:
        tid = None
    if not_tid := (tid is None):
        pass
    if not tid and audit_id:
        tid = str(audit_id)
    try:
        ts_iso = datetime.utcnow().isoformat()
        meta_str = ""
        try:
            if meta is not None:
                meta_str = " " + json.dumps(meta, default=str)
        except Exception:
            meta_str = ""
        _append_to_task_logfile(tid, ts_iso, level, f"{message}{meta_str}")
    except Exception:
        try:
            logger.exception("append to per-task logfile failed")
        except Exception:
            pass


def _get_current_task_id():
    try:
        return getattr(getattr(current_task, "request", None), "id", None)
    except Exception:
        return None


def _derive_payload_for_audit(task_type: str, args, kwargs) -> Optional[Dict[str, Any]]:
    """
    Heuristic payload extraction so TaskAudit rows get informative payloads
    even when tasks use positional args.

    - For report tasks: first positional arg (filters) if present
    - For classify tasks: include email_id and a small content preview
    - Otherwise, prefer kwargs.get('payload') if present
    """
    try:
        if kwargs and "payload" in kwargs:
            return kwargs.get("payload")
        if task_type == "report":
            if args and len(args) >= 1:
                return {"filters": args[0]}
            return None
        if task_type == "classify":
            # signature: classify_email_task(email_id, content)
            email_id = args[0] if args and len(args) >= 1 else kwargs.get("email_id")
            content = args[1] if args and len(args) >= 2 else kwargs.get("content")
            try:
                preview = (str(content)[:300]) if content is not None else None
            except Exception:
                preview = None
            return {"email_id": email_id, "content_preview": preview}
        # generic fallback: try to include first arg summaries
        if args:
            try:
                return {"args": [str(a)[:200] for a in args]}
            except Exception:
                return None
        return None
    except Exception:
        return None


def _safe_wrap_task_execution(audit_id: Optional[str], task_type: str):
    """
    Decorator factory that wraps task execution to update TaskAudit status and logs.
    """

    def deco(f):
        def wrapped(*args, **kwargs):
            tid = _get_current_task_id()
            aid = audit_id

            # Derive a sensible payload for audit rows (captures positional args adequately)
            derived_payload = _derive_payload_for_audit(task_type, args, kwargs)

            # Attempt to obtain an app and run the whole lifecycle inside app_context when possible
            app = _get_app()
            if app is not None:
                # Use application context for pre/post updates and for the task body
                with app.app_context():
                    try:
                        aid = _ensure_task_audit_row(SESSION_MAKER, audit_id=aid, task_id=tid, task_type=task_type, user=None, payload=derived_payload)
                        _update_task_status(aid, "running", started_at=datetime.utcnow())
                        _safe_append_log(aid, "INFO", f"Task {task_type} started (celery id={tid})", meta={"payload": derived_payload})
                    except Exception:
                        logger.debug("pre-run audit setup failed", exc_info=True)
                    try:
                        res = f(*args, **kwargs)
                        try:
                            _update_task_status(aid, "succeeded", finished_at=datetime.utcnow(), result_meta=(res if isinstance(res, dict) else {"result": str(res)}))
                            _safe_append_log(aid, "INFO", f"Task {task_type} succeeded")
                        except Exception:
                            logger.debug("post-run success update failed", exc_info=True)
                        return res
                    except Exception as e:
                        tb = traceback.format_exc()
                        try:
                            _update_task_status(aid, "failed", finished_at=datetime.utcnow(), result_meta={"error": str(e)})
                            _safe_append_log(aid, "ERROR", f"Task {task_type} failed: {str(e)}", meta={"trace": tb})
                        except Exception:
                            logger.debug("post-run failure update failed", exc_info=True)
                        raise
            else:
                # Fallback path: keep current best-effort behavior when no app can be created
                try:
                    aid = _ensure_task_audit_row(SESSION_MAKER, audit_id=aid, task_id=tid, task_type=task_type, user=None, payload=derived_payload)
                    _update_task_status(aid, "running", started_at=datetime.utcnow())
                    _safe_append_log(aid, "INFO", f"Task {task_type} started (celery id={tid})", meta={"payload": derived_payload})
                except Exception:
                    logger.debug("pre-run audit setup failed (no app)", exc_info=True)
                try:
                    res = f(*args, **kwargs)
                    try:
                        _update_task_status(aid, "succeeded", finished_at=datetime.utcnow(), result_meta=(res if isinstance(res, dict) else {"result": str(res)}))
                        _safe_append_log(aid, "INFO", f"Task {task_type} succeeded")
                    except Exception:
                        logger.debug("post-run success update failed (no app)", exc_info=True)
                    return res
                except Exception as e:
                    tb = traceback.format_exc()
                    try:
                        _update_task_status(aid, "failed", finished_at=datetime.utcnow(), result_meta={"error": str(e)})
                        _safe_append_log(aid, "ERROR", f"Task {task_type} failed: {str(e)}", meta={"trace": tb})
                    except Exception:
                        logger.debug("post-run failure update failed (no app)", exc_info=True)
                    raise

        wrapped.__name__ = f.__name__
        return wrapped
    return deco


def _get_report_dir() -> str:
    # Prefer configured REPORT_PATH in env; API helper _report_dir in api.reports will
    # handle directory creation when run inside Flask; tasks use env fallback.
    return os.environ.get("REPORT_PATH", "/tmp/phish_reports")


def _tail_log_file(log_path: str, max_lines: int = 200) -> str:
    """
    Safely return last max_lines of the log file as a single string.
    Best-effort: swallow failures.
    """
    try:
        if not os.path.exists(log_path):
            return ""
        # read in reverse efficiently for medium files
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            size = 0
            block = 1024
            data = bytearray()
            while end > 0 and size < max_lines * 200:  # heuristic limit
                read_size = min(block, end)
                fh.seek(end - read_size)
                chunk = fh.read(read_size)
                data[0:0] = chunk
                end -= read_size
                size += read_size
                if len(data) > 1024 * 1024:
                    break
            text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            return "\n".join(lines[-max_lines:])
    except Exception:
        try:
            logger.debug("tail_log_file failed for %s", log_path, exc_info=True)
        except Exception:
            pass
        return ""


def _build_full_body_artifact(report_dir: str, persistence_id: str, body_content: str, tid: str, persistence_meta: Optional[Dict[str, Any]], prediction_summary: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """
    Assemble and atomically write:
    - {persistence_id}.body.txt (raw body)
    - {persistence_id}.full.body.txt (canonical artifact with metadata, logs, prediction summary, raw body)

    Returns dict: {"filename": "<basename>", "full_filename": "<basename>", "path": "<abs full path>"}
    """
    raw_filename = os.path.join(report_dir, f"{persistence_id}.body.txt")
    full_filename = os.path.join(report_dir, f"{persistence_id}.full.body.txt")
    temp_raw = None
    temp_full = None
    try:
        # Build metadata
        meta = {
            "persistence_id": int(persistence_id) if str(persistence_id).isdigit() else persistence_id,
            "celery_id": tid if tid else None,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "user": None,
            "source": "classify_email_task",
            "task_type": "classify",
            "payload_preview": {"email_id": (persistence_meta.get("sender") if isinstance(persistence_meta, dict) else None), "content_preview": (str(body_content)[:400] if body_content else "")},
        }

        # Tail logs
        log_lines = ""
        try:
            log_fname = os.path.join(report_dir, f"{tid}.log") if tid else None
            if not log_fname or not os.path.exists(log_fname):
                if persistence_id:
                    alt = os.path.join(report_dir, f"{persistence_id}.log")
                    if os.path.exists(alt):
                        log_fname = alt
            if log_fname and os.path.exists(log_fname):
                log_lines = _tail_log_file(log_fname, max_lines=500)
        except Exception:
            log_lines = ""

        pred_json = json.dumps(prediction_summary, default=str) if prediction_summary is not None else "{}"

        parts = []
        parts.append("=== PHISHCLASSIFIER BODY ARTIFACT v1")
        parts.append("metadata: " + json.dumps(meta, default=str))
        parts.append("--- LOGS BEGIN ---")
        parts.append(log_lines if log_lines else "[no per-task logfile available]")
        parts.append("--- LOGS END ---")
        parts.append("--- PREDICTION SUMMARY (machine readable) ---")
        parts.append(pred_json)
        parts.append("--- RAW BODY START ---")
        parts.append(body_content or "")
        parts.append("--- RAW BODY END ---")
        artifact_text = "\n".join(parts)

        # Atomic write for raw
        fd_raw, temp_raw = tempfile.mkstemp(dir=report_dir, prefix=f".{persistence_id}.body.raw.", text=True)
        os.close(fd_raw)
        with open(temp_raw, "w", encoding="utf-8") as fh:
            fh.write(body_content or "")
        try:
            os.replace(temp_raw, raw_filename)
        except Exception:
            shutil.move(temp_raw, raw_filename)
        temp_raw = None
        try:
            os.chmod(raw_filename, 0o600)
        except Exception:
            try:
                logger.warning("chmod failed on %s; retrying once", raw_filename, exc_info=True)
                try:
                    os.chmod(raw_filename, 0o600)
                except Exception:
                    logger.error("chmod retry failed on %s", raw_filename, exc_info=True)
            except Exception:
                pass

        # best-effort: normalize ownership to configured REPORT_OWNER_UID/GID (fallback to 1000:1000)
        try:
            owner_uid = int(os.environ.get("REPORT_OWNER_UID", "1000"))
            owner_gid = int(os.environ.get("REPORT_OWNER_GID", "1000"))
            try:
                os.chown(raw_filename, owner_uid, owner_gid)
            except Exception:
                cur_owner = "unknown"
                try:
                    st = os.stat(raw_filename)
                    cur_owner = f"{st.st_uid}:{st.st_gid}"
                except Exception:
                    pass
                logger.warning("chown failed for %s (intended %d:%d) current_owner=%s", raw_filename, owner_uid, owner_gid, cur_owner, exc_info=True)
        except Exception:
            logger.exception("unexpected error during chown attempt for %s", raw_filename)

        # Atomic write for full artifact
        fd_full, temp_full = tempfile.mkstemp(dir=report_dir, prefix=f".{persistence_id}.body.full.", text=True)
        os.close(fd_full)
        with open(temp_full, "w", encoding="utf-8") as fh:
            fh.write(artifact_text)
        try:
            os.replace(temp_full, full_filename)
        except Exception:
            shutil.move(temp_full, full_filename)
        temp_full = None
        try:
            os.chmod(full_filename, 0o600)
        except Exception:
            try:
                logger.warning("chmod failed on %s; retrying once", full_filename, exc_info=True)
                try:
                    os.chmod(full_filename, 0o600)
                except Exception:
                    logger.error("chmod retry failed on %s", full_filename, exc_info=True)
            except Exception:
                pass

        # best-effort: normalize ownership to configured REPORT_OWNER_UID/GID (fallback to 1000:1000)
        try:
            owner_uid = int(os.environ.get("REPORT_OWNER_UID", "1000"))
            owner_gid = int(os.environ.get("REPORT_OWNER_GID", "1000"))
            try:
                os.chown(full_filename, owner_uid, owner_gid)
            except Exception:
                cur_owner = "unknown"
                try:
                    st = os.stat(full_filename)
                    cur_owner = f"{st.st_uid}:{st.st_gid}"
                except Exception:
                    pass
                logger.warning("chown failed for %s (intended %d:%d) current_owner=%s", full_filename, owner_uid, owner_gid, cur_owner, exc_info=True)
        except Exception:
            logger.exception("unexpected error during chown attempt for %s", full_filename)

        return {"filename": os.path.basename(raw_filename), "full_filename": os.path.basename(full_filename), "path": full_filename}
    except Exception:
        try:
            logger.exception("build_full_body_artifact failed")
        except Exception:
            pass
        try:
            if temp_raw and os.path.exists(temp_raw):
                os.remove(temp_raw)
        except Exception:
            pass
        try:
            if temp_full and os.path.exists(temp_full):
                os.remove(temp_full)
        except Exception:
            pass
        # Fallback: attempt to write raw body
        try:
            with open(raw_filename, "w", encoding="utf-8") as fh:
                fh.write(body_content or "")
            try:
                os.chmod(raw_filename, 0o600)
            except Exception:
                try:
                    logger.warning("chmod failed on %s; retrying once", raw_filename, exc_info=True)
                    try:
                        os.chmod(raw_filename, 0o600)
                    except Exception:
                        logger.error("chmod retry failed on %s", raw_filename, exc_info=True)
                except Exception:
                    pass

            # best-effort: normalize ownership for fallback raw file
            try:
                owner_uid = int(os.environ.get("REPORT_OWNER_UID", "1000"))
                owner_gid = int(os.environ.get("REPORT_OWNER_GID", "1000"))
                try:
                    os.chown(raw_filename, owner_uid, owner_gid)
                except Exception:
                    cur_owner = "unknown"
                    try:
                        st = os.stat(raw_filename)
                        cur_owner = f"{st.st_uid}:{st.st_gid}"
                    except Exception:
                        pass
                    logger.warning("chown failed for %s (intended %d:%d) current_owner=%s", raw_filename, owner_uid, owner_gid, cur_owner, exc_info=True)
            except Exception:
                logger.exception("unexpected error during chown attempt for %s", raw_filename)

            return {"filename": os.path.basename(raw_filename), "full_filename": None, "path": raw_filename}
        except Exception:
            return {"filename": None, "full_filename": None, "path": ""}


# Try to preload model once at module import time so prefork children can inherit it.
# This uses the provided preload_model.get_model helper. Failures are logged but do not stop import.
try:
    from preload_model import get_model  # type: ignore

    try:
        # call once to populate preload_model._model in the module; this is blocking
        # at import time but ensures prefork children inherit a warmed model when forked.
        get_model()
        logger.info("preloaded classification model at module import")
    except Exception:
        logger.debug("preload get_model failed; model will be loaded lazily", exc_info=True)
except Exception:
    # preload helper not present; fallback to lazy load inside tasks
    pass


@celery.task(name="reports.generate_report_task")
@_safe_wrap_task_execution(audit_id=None, task_type="report")
def generate_report_task(filters: Optional[Dict[str, Any]] = None, audit_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Celery task to create a quarantine CSV in REPORT_PATH.
    Returns {"filename": "<name>", "path": "<abs path>"}
    """
    try:
        # Ensure TaskAudit row is present and linked (best-effort)
        try:
            if audit_id:
                _ensure_task_audit_row(SESSION_MAKER, audit_id=audit_id, task_id=_get_current_task_id(), task_type="report", user=None, payload=filters)
        except Exception:
            pass

        # Lazy import to avoid circular import
        from api.reports import _generate_and_write_report  # type: ignore

        app = _get_app()
        if app is not None:
            with app.app_context():
                path = _generate_and_write_report(filters or {})
        else:
            # Fallback: call without app context (best-effort)
            path = _generate_and_write_report(filters or {})

        filename = os.path.basename(path)
        try:
            logger.info("generate_report_task: wrote %s", path)
        except Exception:
            try:
                celery.log.info("generate_report_task: wrote %s", path)
            except Exception:
                pass
        return {"filename": filename, "path": path}
    except Exception as e:
        try:
            logger.exception("generate_report_task failed: %s", e)
        except Exception:
            try:
                celery.log.exception("generate_report_task failed: %s", e)
            except Exception:
                pass
        raise


@celery.task(name="reports.purge_old_reports")
def purge_old_reports(days: int = 30) -> Dict[str, Any]:
    """
    Delete reports older than `days` from REPORT_PATH. Returns list of removed filenames.
    """
    report_dir = _get_report_dir()
    now = datetime.utcnow().timestamp()
    threshold = now - (days * 86400)
    removed = []
    try:
        if not os.path.isdir(report_dir):
            try:
                logger.warning("purge_old_reports: report_dir not found: %s", report_dir)
            except Exception:
                try:
                    celery.log.warning("purge_old_reports: report_dir not found: %s", report_dir)
                except Exception:
                    pass
            return {"removed": removed}
        for fn in os.listdir(report_dir):
            p = os.path.join(report_dir, fn)
            try:
                # Only consider files older than the threshold
                if not os.path.isfile(p) or os.path.getmtime(p) >= threshold:
                    continue
                # Restrict deletions to known artifact patterns to avoid accidental removes
                if fn.endswith("log") or fn.endswith("body.txt") or fn.endswith("csv") or fn.endswith("json") or fn.endswith("jpg") or fn.endswith("png"):
                    try:
                        os.remove(p)
                        removed.append(fn)
                    except Exception:
                        try:
                            logger.exception("purge_old_reports: failed to remove %s", p)
                        except Exception:
                            try:
                                celery.log.exception("purge_old_reports: failed to remove %s", p)
                            except Exception:
                                pass
            except Exception:
                try:
                    logger.exception("purge_old_reports: failed on %s", p)
                except Exception:
                    try:
                        celery.log.exception("purge_old_reports: failed on %s", p)
                    except Exception:
                        pass
        try:
            logger.info("purge_old_reports: removed %d files", len(removed))
        except Exception:
            try:
                celery.log.info("purge_old_reports: removed %d files", len(removed))
            except Exception:
                pass
    except Exception:
        try:
            logger.exception("purge_old_reports: directory scan failed")
        except Exception:
            try:
                celery.log.exception("purge_old_reports: directory scan failed")
            except Exception:
                pass
    return {"removed": removed}

# Coordinator (lightweight) task - runs on worker_io
@celery.task(name="classify_email_task")
def classify_email_task(email_id: Optional[str], content: Optional[str] = None, sync: bool = True, audit_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Coordinator task:
    - Accepts optional audit_id provided by caller (UI/web).
    - If sync=True: run heavy classification inline (preserves old synchronous behavior).
    - If sync=False: create/ensure TaskAudit and delegate to classify_email_task_heavy on cpu queue,
      forwarding the (preferential) audit_id so the heavy worker can update TaskAudit with persistence_id.
    Returns either final artifact/persistence (sync) or delegated_task_id and audit_id.
    """
    try:
        # Derive payload for audit
        derived_payload = _derive_payload_for_audit("classify", (email_id, content), {})
        # Obtain celery request id when available
        try:
            tid = getattr(getattr(current_task, "request", None), "id", None)
        except Exception:
            tid = None

        # Prefer caller-supplied audit_id if present; otherwise create/ensure one here.
        aid = audit_id
        try:
            if not aid:
                # First try to let _ensure_task_audit_row pick an existing id by task_id
                aid = _ensure_task_audit_row(SESSION_MAKER, audit_id=None, task_id=tid, task_type="classify", user=None, payload=derived_payload)
            if not aid:
                # fallback deterministic manual id to ensure subsequent heavy work can update TaskAudit
                manual_id = f"manual-{int(datetime.utcnow().timestamp())}-{uuid.uuid4().hex[:8]}"
                try:
                    aid = _ensure_task_audit_row(SESSION_MAKER, audit_id=manual_id, task_id=tid, task_type="classify", user=None, payload=derived_payload)
                except Exception:
                    aid = manual_id

            # mark delegated/running now that we have an audit id (best-effort)
            try:
                _update_task_status(aid, "delegated" if not sync else "running", started_at=datetime.utcnow())
                _safe_append_log(aid, "INFO", f"Coordinator invoked classify (celery id={tid})", meta={"payload": derived_payload})
            except Exception:
                logger.debug("coordinator: failed to mark TaskAudit running/delegated", exc_info=True)
        except Exception:
            logger.debug("coordinator: failed to create/mark TaskAudit", exc_info=True)

        if sync:
            # Run heavy path inline to preserve previous synchronous artifact/update semantics
            try:
                # Forward the chosen audit id to the heavy worker function so it can update TaskAudit deterministically
                return classify_email_task_heavy(email_id, content, audit_id=aid)
            except Exception:
                # Ensure audit row is updated on failure
                try:
                    _update_task_status(aid, "failed", finished_at=datetime.utcnow(), result_meta={"error": "inline heavy execution failed"})
                    _safe_append_log(aid, "ERROR", "inline heavy execution failed")
                except Exception:
                    pass
                raise
        else:
            # Delegate heavy work to cpu queue (explicit routing)
            try:
                # pass audit id through kwargs so heavy task can bind it deterministically
                res = classify_email_task_heavy.apply_async((email_id, content), queue="cpu", kwargs={"audit_id": aid})
                return {"status": "delegated", "delegated_task_id": res.id, "audit_id": aid}
            except Exception as e:
                logger.exception("coordinator: failed to delegate classify to cpu: %s", e)
                try:
                    if aid:
                        _update_task_status(aid, "failed", finished_at=datetime.utcnow(), result_meta={"error": str(e)})
                        _safe_append_log(aid, "ERROR", f"delegation failed: {str(e)}")
                except Exception:
                    pass
                raise
    except Exception as e:
        logger.exception("classify_email_task (coordinator) failed: %s", e)
        raise


# Heavy CPU-bound task - runs on worker_cpu
@celery.task(name="classify_email_task_heavy")
@_safe_wrap_task_execution(audit_id=None, task_type="classify")
def classify_email_task_heavy(email_id: Optional[str], content: Optional[str] = None, audit_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Heavy classification worker moved from original classify_email_task.
    Performs classification, persistence, artifact writing and TaskAudit updates.
    Accepts optional audit_id passed from coordinator so TaskAudit rows can be updated reliably.

    Specified behavior: prefer numeric persistence id for canonical artifact filenames when available; otherwise use the heavy task's celery id.
    Heavy worker will deterministically update TaskAudit using provided audit_id when present; only if audit_id is absent the previous heuristics run.
    """
    try:
        from api.classifier import classify_text  # type: ignore
        from api.persistence import save_prediction, persist_result  # type: ignore
        import json as _json

        # determine persistent logfile id (prefer celery task id)
        try:
            tid = getattr(getattr(current_task, "request", None), "id", None)
        except Exception:
            tid = None

        def _log_line(level: str, message: str, meta: Optional[Dict[str, Any]] = None):
            ts = datetime.utcnow().isoformat()
            try:
                meta_str = ""
                if meta is not None:
                    meta_str = " " + _json.dumps(meta, default=str)
            except Exception:
                meta_str = ""
            # append to DB TaskLog + per-task file via existing helper
            try:
                _safe_append_log(audit_id, level, message, meta=meta)
            except Exception:
                try:
                    # fallback to direct file append if safe_append_log fails
                    if tid:
                        _append_to_task_logfile(tid, ts, level, f"{message}{meta_str}")
                except Exception:
                    pass

        # If an audit_id was passed from coordinator, prefer to mark it as running immediately.
        if audit_id:
            try:
                _update_task_status(audit_id, "running", started_at=datetime.utcnow())
                _safe_append_log(audit_id, "INFO", f"classify_email_task_heavy started (celery id={tid})", meta={"email_id": str(email_id)[:200]})
            except Exception:
                logger.debug("failed to mark provided audit_id running", exc_info=True)

        # If content omitted, try to derive it safely
        if content is None:
            try:
                if isinstance(email_id, dict):
                    for k in ("content", "body", "message", "text"):
                        if k in email_id and email_id.get(k) is not None:
                            content = email_id.get(k)
                            break
                if content is None:
                    content = ""
                    try:
                        _log_line("WARNING", f"classify_email_task_heavy invoked without content; proceeding with empty content for email_id={str(email_id)[:200]}")
                    except Exception:
                        pass
            except Exception:
                content = ""
                try:
                    _log_line("WARNING", "classify_email_task_heavy: failed to derive content; proceeding with empty content")
                except Exception:
                    pass

        # --- Mandatory: write class-level temporary body artifact for this tid (best-effort) ---
        try:
            try:
                tid = getattr(getattr(current_task, "request", None), "id", None)
            except Exception:
                tid = None
            if not_tid := (tid is None):
                pass
            if not tid:
                # fallback stable-ish name to avoid collisions if no celery id
                tid = f"unknown-{int(datetime.utcnow().timestamp() * 1000)}"

            report_dir = _get_report_dir_for_tasks()
            try:
                os.makedirs(report_dir, exist_ok=True)
            except Exception:
                pass

            # Write a temporary tid-named file for traceability; we'll prefer
            # writing final artifacts using the heavy task id so UI and files match.
            body_fname = os.path.join(report_dir, f"{tid}.body.txt")
            try:
                with open(body_fname, "w", encoding="utf-8") as fh:
                    fh.write(content if content is not None else "")
                try:
                    _safe_append_log(audit_id, "INFO", f"body written to {body_fname}", meta={"body_path": body_fname, "length": (len(content) if content is not None else 0)})
                except Exception:
                    try:
                        ts_iso = datetime.utcnow().isoformat()
                        _append_to_task_logfile(tid, ts_iso, "INFO", f"body written to {body_fname}")
                    except Exception:
                        pass
            except Exception:
                try:
                    _safe_append_log(audit_id, "WARNING", "failed to write body file", meta={"intended_path": body_fname})
                except Exception:
                    try:
                        ts_iso = datetime.utcnow().isoformat()
                        _append_to_task_logfile(tid, ts_iso, "WARNING", f"failed to write body file {body_fname}")
                    except Exception:
                        pass
        except Exception:
            try:
                _safe_append_log(audit_id, "WARNING", "unexpected error while attempting to write body artifact")
            except Exception:
                pass
        # --- end mandatory body write ---

        # Emit an early log indicating classification is beginning (detailed)
        try:
            _log_line("INFO", "classification invoked", meta={"email_id": str(email_id)[:200], "content_preview": (str(content)[:400] if content else "")})
        except Exception:
            logger.debug("initial append log failed", exc_info=True)

        # Explicit task lifecycle logs for visibility (TASK-START)
        try:
            tid_log = tid or getattr(getattr(current_task, "request", None), "id", "unknown")
            logger.info("TASK-START %s classify_email_task_heavy", tid_log)
            _safe_append_log(audit_id, "INFO", f"TASK-START {tid_log} classify_email_task_heavy")
        except Exception:
            pass

        # Run classifier; capture and log intermediate debug when available
        try:
            # If preload helper exists, attempt to use preloaded model in that module.
            # classifier.classify_text should use any globally available model if implemented that way.
            result = classify_text(content)
            try:
                _log_line("DEBUG", "classify_text returned", meta={"result_preview": (str(result)[:200] if result is not None else None)})
            except Exception:
                pass
        except Exception as e:
            tb = traceback.format_exc()
            _log_line("ERROR", "classify_text raised exception", meta={"error": str(e), "trace": tb})
            raise

        pred_label = result.get("prediction") or result.get("label") or result.get("prediction_result")
        risk = result.get("risk_score") or result.get("score")
        try:
            risk_val = float(risk) if risk is not None else None
        except Exception:
            risk_val = None

        # thresholds resolution
        threshold_to_use = None
        try:
            if result.get("threshold") is not None:
                threshold_to_use = float(result.get("threshold"))
            elif result.get("threshold_percent") is not None:
                threshold_to_use = float(result.get("threshold_percent")) / 100.0
        except Exception:
            threshold_to_use = None

        if threshold_to_use is None:
            try:
                root = os.environ.get("PROJECT_ROOT", "/app")
                tpath = os.path.join(root, "thresholds.json")
                if os.path.exists(tpath):
                    with open(tpath, "r", encoding="utf-8") as fh:
                        j = _json.load(fh)
                    file_thresh = j.get("F1.0", {}).get("WeightedGlobal")
                    if file_thresh is not None:
                        threshold_to_use = float(file_thresh)
            except Exception:
                threshold_to_use = None

        if threshold_to_use is None:
            threshold_to_use = float(os.environ.get("QUARANTINE_RISK_THRESHOLD", 0.5))

        is_quarantined = False
        lab = (pred_label or "")
        lab = lab.lower().strip() if isinstance(lab, str) else ""
        if lab and any(tok in lab for tok in ("phish", "phishing", "quarantine", "quarantined", "spam")):
            is_quarantined = True
        elif risk_val is not None and risk_val >= threshold_to_use:
            is_quarantined = True

        sender_val = "unknown@missing"
        try:
            if isinstance(email_id, dict):
                sender_val = (email_id.get("sender") or "unknown@missing").strip()
            elif isinstance(email_id, str) and "@" in email_id:
                sender_val = email_id
        except Exception:
            sender_val = "unknown@missing"

        # prefer link_analysis present at top-level, otherwise inside attacker_insights
        link_analysis = result.get("link_analysis")
        if not link_analysis:
            attacker = result.get("attacker_insights") or {}
            link_analysis = attacker.get("link_analysis")

        # Emit intermediate diagnostic log with prediction summary (detailed)
        try:
            _log_line("INFO", "classification result", meta={"prediction": pred_label, "risk_score": risk_val, "is_quarantined": is_quarantined, "threshold_used": threshold_to_use})
        except Exception:
            logger.debug("append log (result) failed", exc_info=True)

        # Persist prediction and log the persistence detail
        try:
            # Pass task_audit_id so save_prediction can attach audit rows if desired
            persistence = save_prediction(
                sender=sender_val,
                subject=result.get("subject"),
                body=content,
                received_at=datetime.utcnow(),
                risk_score=risk_val,
                status="quarantined" if is_quarantined else "scored",
                feedback=None,
                prediction=pred_label,
                technical_explanation=result.get("technical_explanation"),
                attacker_insights=result.get("attacker_insights"),
                link_analysis=link_analysis,
                model_version=result.get("model_version"),
                attachments_meta=result.get("attachments_meta"),
                create_audit=False,
                task_audit_id=audit_id,
            )
            try:
                _log_line("INFO", "persistence complete", meta={"persistence": persistence})
            except Exception:
                pass

            # If persistence provides an authoritative id, build canonical artifacts and write normalized TaskAudit.result_meta
            try:
                pid = None
                if isinstance(persistence, dict):
                    pid = persistence.get("id") or persistence.get("persistence_id") or persistence.get("audit_id")
                # prefer numeric persistence id for canonical artifact filenames when available; fallback to celery tid
                pid_str = str(pid) if pid is not None else None
                if pid_str and pid_str.isdigit():
                    artifact_id = pid_str
                else:
                    artifact_id = tid

                if artifact_id:
                    report_dir = _get_report_dir_for_tasks()

                    # Build prediction_summary used in artifact
                    prediction_summary = {
                        "persistence_id": pid,
                        "prediction": pred_label,
                        "risk_score": risk_val,
                        "threshold_used": threshold_to_use,
                        "is_quarantined": is_quarantined,
                        "model_version": result.get("model_version"),
                        "link_analysis": link_analysis,
                    }

                    # Use atomic writer helper to write raw + full artifacts using artifact_id (prefer numeric pid)
                    try:
                        artifact_info = _build_full_body_artifact(report_dir, artifact_id, content if content is not None else "", tid, {"sender": sender_val}, prediction_summary)
                        if artifact_info and artifact_info.get("filename"):
                            try:
                                _safe_append_log(audit_id, "INFO", f"full body artifact written to {artifact_info.get('path')}", meta={"artifact": artifact_info})
                            except Exception:
                                try:
                                    ts_iso = datetime.utcnow().isoformat()
                                    _append_to_task_logfile(tid, ts_iso, "INFO", f"full body artifact written to {artifact_info.get('path')}")
                                except Exception:
                                    pass

                        # --- START: small focused update with size caps ---
                        # Read full artifact and log (bounded) and include them in normalized_meta so UI can render without an extra query.
                        preview_text = ""
                        full_text = ""
                        log_text = ""
                        try:
                            full_fname = artifact_info.get("path") if artifact_info else None
                            if full_fname and os.path.exists(full_fname):
                                try:
                                    with open(full_fname, "r", encoding="utf-8", errors="replace") as fh:
                                        raw_full = fh.read()
                                    # enforce caps
                                    FULL_BODY_CAP = 50_000  # 50 KB
                                    full_text = raw_full[:FULL_BODY_CAP]
                                    preview_text = full_text[:2000]
                                except Exception:
                                    full_text = ""
                                    preview_text = ""
                            # log filename based on artifact_id
                            log_fname = os.path.join(report_dir, f"{artifact_id}.log")
                            if not os.path.exists(log_fname) and pid_str:
                                alt = os.path.join(report_dir, f"{pid_str}.log")
                                if os.path.exists(alt):
                                    log_fname = alt
                            if log_fname and os.path.exists(log_fname):
                                try:
                                    # reuse tail helper for bounded log capture (cap lines, then bytes)
                                    log_capture = _tail_log_file(log_fname, max_lines=500)
                                    # enforce byte cap on log_text
                                    LOG_CAP = 20_000  # 20 KB
                                    if len(log_capture) > LOG_CAP:
                                        log_text = log_capture[-LOG_CAP:]
                                    else:
                                        log_text = log_capture
                                except Exception:
                                    try:
                                        with open(log_fname, "r", encoding="utf-8", errors="replace") as lf:
                                            raw_log = lf.read()
                                        LOG_CAP = 20_000
                                        log_text = raw_log[-LOG_CAP:] if len(raw_log) > LOG_CAP else raw_log
                                    except Exception:
                                        log_text = ""
                        except Exception:
                            full_text = ""
                            preview_text = ""
                            log_text = ""

                        normalized_meta = {
                            "persistence_id": int(pid) if pid is not None and str(pid).isdigit() else (int(pid) if isinstance(pid, int) else None),
                            "filename": artifact_info.get("filename") if artifact_info else f"{artifact_id}.body.txt",
                            "full_filename": artifact_info.get("full_filename") if artifact_info else f"{artifact_id}.full.body.txt",
                            "log_filename": f"{artifact_id}.log",
                            "preview": preview_text,
                            "full_body": full_text if full_text else None,
                            "log_text": log_text if log_text else None,
                        }
                        # --- END: small focused update with size caps ---

                        # Persist per-persistence log entry (mirror tid log) using persistence numeric id if present
                        try:
                            ts_iso_local = datetime.utcnow().isoformat()
                            if pid_str:
                                _append_to_persistence_log(pid_str, ts_iso_local, "INFO", "artifact_written")
                            else:
                                _append_to_persistence_log(artifact_id, ts_iso_local, "INFO", "artifact_written")
                        except Exception:
                            pass

                        # Attempt to update TaskAudit.result_meta. Prefer to use the supplied audit_id
                        try:
                            if TaskAudit is not None:
                                if audit_id:
                                    try:
                                        _update_task_status(audit_id, "succeeded", finished_at=datetime.utcnow(), result_meta=normalized_meta)
                                    except Exception:
                                        # best-effort fallback to emit event if DB update fails
                                        try:
                                            _emit_task_update(audit_id, {"status": "succeeded", "result_meta": normalized_meta})
                                        except Exception:
                                            pass
                                else:
                                    # No explicit audit_id provided; fall back to heuristics (task_id then persistence scan)
                                    audit_to_update = None
                                    tid_lookup = getattr(getattr(current_task, "request", None), "id", None)
                                    ta_id = None
                                    db_session, created = _acquire_session()
                                    if db_session:
                                        try:
                                            if tid_lookup:
                                                ta = db_session.query(TaskAudit).filter_by(task_id=tid_lookup).order_by(TaskAudit.updated_at.desc()).first()
                                                if ta:
                                                    ta_id = ta.id
                                            # fallback: scan recent TaskAudit rows to find one mentioning this persistence id
                                            if ta_id is None and pid is not None:
                                                recent = db_session.query(TaskAudit).order_by(TaskAudit.updated_at.desc()).limit(50).all()
                                                for candidate in recent:
                                                    rm = candidate.result_meta
                                                    try:
                                                        if isinstance(rm, dict) and rm.get("persistence_id") == int(pid):
                                                            ta_id = candidate.id
                                                            break
                                                        if isinstance(rm, str):
                                                            try:
                                                                parsed = json.loads(rm)
                                                                if isinstance(parsed, dict) and parsed.get("persistence_id") == int(pid):
                                                                    ta_id = candidate.id
                                                                    break
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        pass
                                        finally:
                                            if created:
                                                _safe_close_session(db_session)
                                    audit_to_update = ta_id
                                    if audit_to_update:
                                        try:
                                            _update_task_status(audit_to_update, "succeeded", finished_at=datetime.utcnow(), result_meta=normalized_meta)
                                        except Exception:
                                            pass
                                    else:
                                        try:
                                            _emit_task_update(None, {"status": "succeeded", "result_meta": normalized_meta, "persistence_id": (int(pid) if pid is not None and str(pid).isdigit() else None), "artifact_id": artifact_id})
                                        except Exception:
                                            pass
                        except Exception:
                            pass

                    except Exception:
                        try:
                            _safe_append_log(audit_id, "WARNING", "failed to build/write full body artifact", meta={"pid": pid_str, "artifact_id": artifact_id})
                        except Exception:
                            pass

            except Exception:
                try:
                    _safe_append_log(audit_id, "WARNING", "unexpected error while attempting to rename body artifact to persistence id")
                except Exception:
                    pass

        except Exception as e:
            tb = traceback.format_exc()
            _log_line("ERROR", "persistence failed", meta={"error": str(e), "trace": tb})
            raise

        # Final success line
        try:
            _log_line("INFO", "classification finished", meta={"persistence_id": persistence.get("id") if isinstance(persistence, dict) else None})
        except Exception:
            pass

        # Explicit end lifecycle log
        try:
            logger.info("TASK-END %s classify_email_task_heavy", tid_log)
            _safe_append_log(audit_id, "INFO", f"TASK-END {tid_log} classify_email_task_heavy")
        except Exception:
            pass

        # Ensure returned value contains normalized artifact meta for callers
        final_meta = None
        try:
            if isinstance(persistence, dict):
                pid = persistence.get("id") or persistence.get("persistence_id")
                if pid:
                    final_meta = {
                        "persistence_id": int(pid) if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()) else pid,
                        # filenames follow artifact_id preference (we returned artifact filenames above)
                        "filename": f"{artifact_id}.body.txt",
                        "full_filename": f"{artifact_id}.full.body.txt",
                        "log_filename": f"{artifact_id}.log",
                    }
        except Exception:
            final_meta = None

        # Specified update: persist result_meta on the TaskAudit row (if present)
        try:
            # If audit_id present we already attempted update above; prefer that path.
            if TaskAudit is not None and final_meta is not None:
                if audit_id:
                    # update already attempted above; nothing further required here (best-effort)
                    pass
                else:
                    ta_id = None
                    db_session, created_locally = _acquire_session()
                    if db_session:
                        try:
                            tid_lookup = getattr(getattr(current_task, "request", None), "id", None)
                            if tid_lookup:
                                ta = db_session.query(TaskAudit).filter_by(task_id=tid_lookup).order_by(TaskAudit.updated_at.desc()).first()
                                if ta:
                                    ta_id = ta.id
                            # fallback: try to match by persistence id stored earlier
                            if ta_id is None and isinstance(final_meta.get("persistence_id"), int):
                                recent = db_session.query(TaskAudit).order_by(TaskAudit.updated_at.desc()).limit(50).all()
                                for candidate in recent:
                                    rm = candidate.result_meta
                                    try:
                                        if isinstance(rm, dict) and rm.get("persistence_id") == final_meta.get("persistence_id"):
                                            ta_id = candidate.id
                                            break
                                        if isinstance(rm, str):
                                            try:
                                                parsed = json.loads(rm)
                                                if isinstance(parsed, dict) and parsed.get("persistence_id") == final_meta.get("persistence_id"):
                                                    ta_id = candidate.id
                                                    break
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                            if ta_id:
                                # Use existing helper which will also emit socket event
                                _update_task_status(ta_id, "succeeded", finished_at=datetime.utcnow(), result_meta=final_meta)
                        finally:
                            if created_locally:
                                _safe_close_session(db_session)
        except Exception:
            # never fail the task due to this persistence step
            pass

        return {"status": "ok", "persistence": persistence, "artifact": final_meta}
    except Exception as e:
        try:
            tb = traceback.format_exc()
            # Ensure the per-task logfile receives the error and full traceback
            try:
                tid = getattr(getattr(current_task, "request", None), "id", None)
            except Exception:
                tid = None
            ts_iso = datetime.utcnow().isoformat()
            try:
                if tid:
                    _append_to_task_logfile(tid, ts_iso, "ERROR", f"classify_email_task_heavy failed: {str(e)}")
                    _append_to_task_logfile(tid, ts_iso, "ERROR", tb)
            except Exception:
                pass
        except Exception:
            pass
        try:
            logger.exception("classify_email_task_heavy failed: %s", e)
        except Exception:
            try:
                celery.log.exception("classify_email_task_heavy failed: %s", e)
            except Exception:
                pass
        raise


@celery.task(name="retrain_model_task")
@_safe_wrap_task_execution(audit_id=None, task_type="train")
def retrain_model_task(dataset_list: Optional[list] = None, *args, **kwargs) -> Dict[str, Any]:
    """
    Placeholder retrain task. Keeps trainer blueprint importable. Replace body
    with actual training orchestration when ready.
    """
    aid = None
    try:
        # create/ensure TaskAudit row for this training job (best-effort)
        try:
            tid = _get_current_task_id()
            aid = _ensure_task_audit_row(SESSION_MAKER, audit_id=None, task_id=tid, task_type="train", user=None, payload={"datasets": dataset_list})
            try:
                _update_task_status(aid, "running", started_at=datetime.utcnow())
                _safe_append_log(aid, "INFO", f"retrain_model_task started (celery id={tid})")
            except Exception:
                pass
        except Exception:
            aid = None

        try:
            logger.info("retrain_model_task: invoked datasets=%s", dataset_list)
        except Exception:
            try:
                celery.log.info("retrain_model_task: invoked datasets=%s", dataset_list)
            except Exception:
                pass

        # Best-effort DB update to mark job progress when TrainingJob model is present.
        try:
            app = _get_app()
            if app is not None:
                with app.app_context():
                    from api.database import db as _db  # type: ignore
                    from models.training_job import TrainingJob  # type: ignore

                    db_session = _db.session
                    job_id = getattr(getattr(current_task, "request", None), "id", None)
                    if job_id:
                        job = db_session.query(TrainingJob).get(job_id)
                        if job:
                            job.status = "running"
                            db_session.add(job)
                            db_session.commit()
                            # Simulate immediate success outcome for the stub
                            job.status = "succeeded"
                            db_session.add(job)
                            db_session.commit()
            else:
                from api.database import db as _db  # type: ignore
                from models.training_job import TrainingJob  # type: ignore

                db_session = _db.session
                job_id = getattr(getattr(current_task, "request", None), "id", None)
                if job_id:
                    job = db_session.query(TrainingJob).get(job_id)
                    if job:
                        job.status = "running"
                        db_session.add(job)
                        db_session.commit()
                        job.status = "succeeded"
                        db_session.add(job)
                        db_session.commit()
        except Exception:
            try:
                logger.debug("retrain_model_task: DB update skipped or failed", exc_info=True)
            except Exception:
                try:
                    celery.log.debug("retrain_model_task: DB update skipped or failed", exc_info=True)
                except Exception:
                    pass

        # mark audit succeeded and append final log
        try:
            _update_task_status(aid, "succeeded", finished_at=datetime.utcnow(), result_meta={"status": "ok"})
            _safe_append_log(aid, "INFO", "retrain_model_task succeeded")
        except Exception:
            pass

        return {"status": "ok", "detail": "stubbed retrain task executed", "datasets": dataset_list}
    except Exception as e:
        tb = traceback.format_exc()
        try:
            _update_task_status(aid if aid else None, "failed", finished_at=datetime.utcnow(), result_meta={"error": str(e)})
            _safe_append_log(aid if aid else None, "ERROR", f"retrain_model_task failed: {str(e)}", meta={"trace": tb})
        except Exception:
            pass
        try:
            logger.exception("retrain_model_task failed: %s", e)
        except Exception:
            try:
                celery.log.exception("retrain_model_task failed: %s", e)
            except Exception:
                pass
        raise


# Celery beat schedule snippet (optional)
BEAT_SCHEDULE = {
    "purge-old-reports-daily": {
        "task": "reports.purge_old_reports",
        "schedule": 24 * 60 * 60,
        "args": (30,),
        "options": {"queue": "maintenance"},
    }
}

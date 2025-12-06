# api/audit_helpers.py
"""
Helpers to update TaskAudit reliably and idempotently.

Exposes:
- update_task_audit_with_retry(session_maker, audit_id, status, result_meta, persistence_id=None, retries=3, backoff=0.05)

This module attempts:
- a safe transactional update via provided session_maker (preferred)
- if session creation unavailable, falls back to Flask app context + api.database.db
- is idempotent and tolerant to missing rows (optionally creates a deterministic manual TaskAudit row)
"""
import time
import json
import logging
from datetime import datetime
from typing import Any, Optional, Dict

logger = logging.getLogger(__name__)


def update_task_audit_with_retry(session_maker, audit_id: Optional[str], status: str, result_meta: Optional[Dict[str, Any]] = None, persistence_id: Optional[int] = None, retries: int = 3, backoff: float = 0.05) -> bool:
    """
    Try to update the TaskAudit row with id=audit_id, setting status, result_meta and persistence_id.
    Returns True on success, False otherwise.

    Behavior:
    - If audit_id is falsy, returns False.
    - Retries on transient DB errors a few times.
    - If TaskAudit row does not exist, attempts to create a minimal row (id=audit_id) when possible.
    - Does not raise; logs failures.
    """
    if not audit_id:
        return False

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            # Prefer using provided session_maker if callable
            sess = None
            created_local = False
            try:
                if callable(session_maker):
                    sess = session_maker()
                    # If the session maker has a marker for shared Flask session, we won't close it
                    created_local = not getattr(session_maker, "_is_shared_flask_session", False)
                else:
                    sess = session_maker
            except Exception:
                sess = None

            if sess is None:
                # Try Flask app context fallback lazily to avoid import cycles
                try:
                    from flask import current_app
                    app = current_app._get_current_object() if hasattr(current_app, "_get_current_object") else current_app
                except Exception:
                    app = None

                if app is None:
                    # no session and no app context available; wait and retry
                    raise RuntimeError("no DB session available to update TaskAudit")
                try:
                    with app.app_context():
                        from api.database import db  # type: ignore
                        from models.task_audit import TaskAudit  # type: ignore

                        ta = db.session.query(TaskAudit).filter_by(id=audit_id).first()
                        if ta is None:
                            # create minimal deterministic row
                            ta = TaskAudit(id=audit_id, task_id=None, task_type="unknown", user=None, payload={}, status=status)
                            db.session.add(ta)
                        if result_meta is not None:
                            ta.result_meta = result_meta
                        if persistence_id is not None:
                            try:
                                ta.persistence_id = int(persistence_id)
                            except Exception:
                                pass
                        ta.status = status
                        ta.updated_at = datetime.utcnow()
                        db.session.add(ta)
                        db.session.commit()
                        return True
                except Exception as e:
                    last_exc = e
                    logger.debug("audit_helpers: Flask DB fallback attempt %d failed for audit_id=%s", attempt, audit_id, exc_info=True)
                    time.sleep(backoff * attempt)
                    continue

            # Using sess directly (SQLAlchemy session)
            try:
                # Import model lazily to avoid cycles
                from models.task_audit import TaskAudit  # type: ignore

                ta = sess.query(TaskAudit).filter_by(id=audit_id).first()
                if ta is None:
                    # create one deterministically if possible
                    ta = TaskAudit(id=audit_id, task_id=None, task_type="unknown", user=None, payload={}, status=status)
                    sess.add(ta)
                if result_meta is not None:
                    ta.result_meta = result_meta
                if persistence_id is not None:
                    try:
                        ta.persistence_id = int(persistence_id)
                    except Exception:
                        pass
                ta.status = status
                ta.updated_at = datetime.utcnow()
                sess.add(ta)
                try:
                    sess.commit()
                except Exception:
                    try:
                        sess.flush()
                        sess.commit()
                    except Exception:
                        sess.rollback()
                        raise
                return True
            finally:
                if created_local:
                    try:
                        sess.close()
                    except Exception:
                        pass
        except Exception as exc:
            last_exc = exc
            logger.debug("audit_helpers: attempt %d failed for audit_id=%s: %s", attempt, audit_id, exc, exc_info=True)
            time.sleep(backoff * attempt)

    logger.exception("audit_helpers: failed to update TaskAudit after %d attempts for audit_id=%s; last error: %s", retries, audit_id, last_exc)
    return False

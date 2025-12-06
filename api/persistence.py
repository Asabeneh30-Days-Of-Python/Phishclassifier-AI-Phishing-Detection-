# api/persistence.py

"""
Centralized persistence helpers for saving predictions and related audit records.

Provides a single, deterministic codepath to persist QuarantinedEmail rows used
by synchronous and asynchronous classification flows, CLI tasks, and tests.

Responsibilities:
- Normalize sender/subject/body
- Validate and normalize feedback values
- Use DB transaction semantics and return structured result
- Emit monitoring metrics via api.monitoring
- Create FeedbackAudit rows when feedback changes (if requested)
- Best-effort attach result_meta to TaskAudit when task_audit_id provided
"""
from typing import Any, Dict, Optional
from datetime import datetime
import json
import logging
import os
import tempfile
import time
import shutil
import traceback

from api.database import db
from api.feedback_utils import normalize_feedback  # ensure this exists in your project
from models.quarantined_email import QuarantinedEmail, FeedbackAudit
import monitoring

logger = logging.getLogger(__name__)

from flask import current_app

# Optional SQLAlchemy engine-based helper for persist_result atomic DB update
try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    DB_ENGINE_AVAILABLE = True
except Exception:
    DB_ENGINE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Persist_result diagnostic logger (writes to file inside container)
# ---------------------------------------------------------------------------
LOG_DIR = os.environ.get("APP_LOG_DIR", "/app/logs")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    # If we cannot create the directory, fall back to /tmp
    LOG_DIR = "/tmp"
PERSIST_LOG_PATH = os.path.join(LOG_DIR, "persist_result.log")

persist_logger = logging.getLogger("phishclassifier.persist_result")
if not persist_logger.handlers:
    try:
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(PERSIST_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s")
        handler.setFormatter(fmt)
        persist_logger.addHandler(handler)
        persist_logger.setLevel(logging.DEBUG)
        persist_logger.propagate = False
    except Exception:
        # If file handler cannot be created, fall back to module logger
        persist_logger = logger


def _normalize_sender(raw: Any) -> str:
    try:
        if not raw:
            return "unknown@missing"
        if isinstance(raw, str):
            v = raw.strip()
            return v if v else "unknown@missing"
        if isinstance(raw, dict):
            return (raw.get("sender") or raw.get("from") or "unknown@missing").strip()
    except Exception:
        logger.debug("_normalize_sender: fallback to unknown", exc_info=True)
    return "unknown@missing"


def _normalize_subject(raw: Any, max_len: int = 1024) -> str:
    try:
        if not raw:
            return ""
        s = str(raw)
        if len(s) > max_len:
            return s[:max_len]
        return s
    except Exception:
        logger.debug("_normalize_subject: fallback empty", exc_info=True)
        return ""


def _normalize_body(raw: Any) -> str:
    try:
        if raw is None:
            return ""
        return str(raw)
    except Exception:
        logger.debug("_normalize_body: fallback empty", exc_info=True)
        return ""


def durable_write_text(path: str, content: str, mode: int = 0o600) -> None:
    """
    Atomic durable write: write to a temp file, flush+fsync, chmod, then os.replace.
    Attempts to chown to WEB_UID/WEB_GID only if running as root and env vars present.
    Enforces final file mode.
    """
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Write, flush and fsync to ensure durability before rename
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content or "")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                logger.debug("durable_write_text: fsync failed or unavailable for %s", tmp, exc_info=True)
        fd = None  # fd closed by fdopen context
    except Exception:
        # fallback to simpler open/write if os.open path fails for environment quirks
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(content or "")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except Exception:
                    logger.debug("durable_write_text: fsync failed or unavailable for %s", tmp, exc_info=True)
        except Exception:
            # re-raise after logging
            logger.exception("durable_write_text: write failed for %s", tmp)
            raise

    # set mode on tmp before atomic replace
    try:
        os.chmod(tmp, mode)
    except Exception:
        logger.debug("durable_write_text: chmod tmp failed for %s", tmp, exc_info=True)

    # Atomic replace
    os.replace(tmp, path)

    # best-effort chown if running as root and WEB_UID/WEB_GID provided
    try:
        web_uid = int(os.environ.get("WEB_UID", "-1"))
        web_gid = int(os.environ.get("WEB_GID", "-1"))
        if hasattr(os, "geteuid") and os.geteuid() == 0 and web_uid >= 0 and web_gid >= 0:
            try:
                os.chown(path, web_uid, web_gid)
            except Exception:
                logger.debug("durable_write_text: chown failed for %s", path, exc_info=True)
    except Exception:
        pass

    # enforce final mode
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def save_prediction(
    *,
    sender: Optional[Any] = None,
    subject: Optional[Any] = None,
    body: Optional[Any] = None,
    received_at: Optional[datetime] = None,
    risk_score: Optional[float] = None,
    status: Optional[str] = None,
    feedback: Optional[str] = None,
    prediction: Optional[str] = None,
    technical_explanation: Optional[Any] = None,
    attacker_insights: Optional[Any] = None,
    link_analysis: Optional[Any] = None,
    model_version: Optional[str] = None,
    attachments_meta: Optional[Any] = None,
    create_audit: bool = False,
    user_id: Optional[int] = None,
    task_audit_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Persist a QuarantinedEmail row in a safe, normalized way.

    Returns a dict:
      {
        "status": "ok"|"failed",
        "id": <int|null>,
        "error": <str|null>,
        "created_at": <iso|null>,
        "artifact_hint": {"filename": "<id>.body.txt", "full_filename": "<id>.full.body.txt", "persistence_id": <id>}
      }
    Notes:
      - This function is best-effort with respect to attaching result_meta to a TaskAudit.
      - It will not raise on DB linkage failures; callers should inspect returned dict.
    """
    result: Dict[str, Any] = {"status": "failed", "id": None, "error": None, "created_at": None}
    try:
        sender_val = _normalize_sender(sender)
        subject_val = _normalize_subject(subject)
        body_val = _normalize_body(body)
        rec_at = received_at or datetime.utcnow()

        # Normalize risk_score to float or None
        try:
            rscore = float(risk_score) if risk_score is not None else None
        except Exception:
            rscore = None

        # Normalize feedback to allowed set
        try:
            fb = normalize_feedback(feedback)
        except Exception:
            fb = "none"

        q = QuarantinedEmail(
            sender=sender_val,
            subject=subject_val,
            body=body_val,
            received_at=rec_at,
            risk_score=rscore,
            status=status or ("quarantined" if (rscore is not None and rscore >= 0.5) else "scored"),
            prediction=prediction,
            technical_explanation=json.dumps(technical_explanation or {}, default=str),
            attacker_insights=json.dumps(attacker_insights or {}, default=str),
            link_analysis=json.dumps(link_analysis or [], default=str),
            model_version=model_version,
            attachments_meta=json.dumps(attachments_meta or {}, default=str),
        )

        db.session.add(q)
        db.session.flush()  # assign id

        # If feedback provided and different, create an audit row
        if create_audit and fb is not None:
            try:
                old_val = None
                fa = FeedbackAudit(quarantine_id=q.id, user_id=user_id, old_value=old_val, new_value=fb)
                db.session.add(fa)
            except Exception:
                logger.exception("save_prediction: failed to create FeedbackAudit; continuing")

        # assign feedback using model setter (normalizes)
        try:
            q.feedback = fb
        except Exception:
            try:
                q._feedback = fb or "none"
            except Exception:
                pass

        # --- Specified update: durable short .body.txt write BEFORE commit ---
        try:
            try:
                short_basename = f"{q.id}.body.txt"
                # canonicalize report_dir to instance_path + "reports" (fallback to REPORT_PATH or /tmp/phish_reports)
                instance_path = getattr(current_app, "instance_path", None)
                report_env = os.environ.get("REPORT_PATH")
                if instance_path:
                    report_dir = os.path.join(instance_path, "reports")
                elif report_env:
                    report_dir = report_env
                else:
                    report_dir = "/tmp/phish_reports"
                os.makedirs(report_dir, exist_ok=True)

                short_path = os.path.join(report_dir, short_basename)
                durable_write_text(short_path, body_val, mode=0o600)
            except Exception as dw_exc:
                # rollback the DB session and return a failure result so callers don't get a DB row with missing artifact
                try:
                    db.session.rollback()
                except Exception:
                    logger.exception("save_prediction: rollback failed after durable write error", exc_info=True)
                logger.exception("save_prediction: durable write of short body failed: %s", dw_exc)
                result["error"] = f"durable write failed: {dw_exc}"
                return result
        except Exception:
            # defensive - if anything unexpected happens here, rollback and return
            try:
                db.session.rollback()
            except Exception:
                logger.exception("save_prediction: rollback failed in durable write unexpected branch")
            result["error"] = "durable write unexpected failure"
            return result
        # --- end durable short write ---

        db.session.commit()
        db.session.refresh(q)

        # --- START INSERTED SNIPPET ---
        # Ensure TaskAudit exists when caller provided a task_audit_id but no row exists.
        if task_audit_id:
            try:
                from models.task_audit import TaskAudit  # type: ignore
                existing = db.session.query(TaskAudit).filter_by(id=task_audit_id).first()
                if not existing:
                    try:
                        ta = TaskAudit(
                            id=task_audit_id,
                            task_id=None,
                            task_type="classification",
                            user=None,
                            payload={},
                            status="pending",
                            persistence_id=q.id,
                            result_meta={"persistence_id": q.id, "filename": f"{q.id}.body.txt", "full_filename": f"{q.id}.full.body.txt", "log_filename": f"{q.id}.log"},
                        )
                        db.session.add(ta)
                        try:
                            db.session.commit()
                        except Exception:
                            try:
                                db.session.rollback()
                            except Exception:
                                logger.exception("save_prediction: rollback failed while creating TaskAudit")
                    except Exception:
                        logger.debug("save_prediction: failed to create TaskAudit row", exc_info=True)
            except Exception:
                logger.debug("save_prediction: TaskAudit existence check skipped (import/db error)", exc_info=True)
        # --- END INSERTED SNIPPET ---

        # metrics
        try:
            monitoring.record_email(q.risk_score if q.risk_score is not None else 0.0)
        except Exception:
            logger.debug("monitoring.record_email failed", exc_info=True)

        result.update(
            {
                "status": "ok",
                "id": q.id,
                "created_at": q.created_at.isoformat() if getattr(q, "created_at", None) else None,
            }
        )

        # Provide an artifact_hint so callers can find expected filenames
        try:
            hint = {"filename": f"{q.id}.body.txt", "full_filename": f"{q.id}.full.body.txt", "persistence_id": q.id}
            result["artifact_hint"] = hint
        except Exception:
            pass

        logger.info("save_prediction: persisted id=%s sender=%s score=%s status=%s", q.id, q.sender, q.risk_score, q.status)

        # Best-effort: if a task_audit_id was provided, try to attach result metadata to TaskAudit
        if task_audit_id:
            try:
                # Import locally to avoid import cycles and to keep this operation best-effort
                from tasks import _update_task_status  # type: ignore

                try:
                    # Write a normalized result_meta dict (not string)
                    result_meta = {
                        "persistence_id": q.id,
                        "filename": f"{q.id}.body.txt",
                        "full_filename": f"{q.id}.full.body.txt",
                    }
                    _update_task_status(
                        task_audit_id,
                        "succeeded",
                        finished_at=datetime.utcnow(),
                        result_meta=result_meta,
                    )
                except Exception:
                    logger.debug("save_prediction: failed to update TaskAudit with result_meta", exc_info=True)
            except Exception:
                # If import or update fails, do not raise; just log
                logger.debug("save_prediction: task_audit linkage skipped (import or update failed)", exc_info=True)

        # Best-effort: atomically write full body + log and update TaskAudit/result_meta via persist_result
        try:
            try:
                # import locally to avoid import cycles; persist_result is best-effort and idempotent
                from api.persistence import persist_result  # type: ignore
            except Exception:
                # local import may fail if module aliasing differs; fall back to calling function in same module
                persist_result = globals().get("persist_result")

            if callable(persist_result):
                try:
                    persist_result(
                        persistence_id=q.id,
                        body_text=None,  # short body already written before commit
                        full_text=body_val,
                        log_text=f"persisted id={q.id} sender={q.sender}\n",
                        audit_id=task_audit_id,
                        filename=f"{q.id}.body.txt",
                        full_filename=f"{q.id}.full.body.txt",
                        log_filename=f"{q.id}.log",
                    )
                except Exception:
                    logger.exception("save_prediction: persist_result failed for id=%s", q.id)
            else:
                # fallback if persist_result not found: write artifacts directly
                try:
                    # canonicalize report_dir to instance_path + "reports" (fallback to REPORT_PATH or /tmp/phish_reports)
                    instance_path = getattr(current_app, "instance_path", None)
                    report_env = os.environ.get("REPORT_PATH")
                    if instance_path:
                        report_dir = os.path.join(instance_path, "reports")
                    elif report_env:
                        report_dir = report_env
                    else:
                        report_dir = "/tmp/phish_reports"
                    os.makedirs(report_dir, exist_ok=True)

                    durable_write_text(os.path.join(report_dir, f"{q.id}.full.body.txt"), body_val, mode=0o600)
                    durable_write_text(os.path.join(report_dir, f"{q.id}.log"), f"persisted id={q.id} sender={q.sender}\n", mode=0o600)
                except Exception:
                    logger.exception("save_prediction: fallback direct writes failed for id=%s", q.id)
        except Exception:
            logger.debug("save_prediction: unexpected failure in post-commit artifact persist", exc_info=True)

    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            logger.exception("save_prediction: rollback failed")
        logger.exception("save_prediction: persist failed: %s", exc)
        result["error"] = str(exc)
    return result


# --------------------------------------------------------------------------------
# NEW: persist_result helper (atomic artifact writes + reliable TaskAudit result_meta update)
# --------------------------------------------------------------------------------
# Added to ensure the same codepath can be used by synchronous and asynchronous
# flows to atomically write artifacts and update TaskAudit.result_meta so the UI
# always sees canonical artifacts without manual DB edits.
#
# Minimal dependencies: uses durable_write_text for atomic file writes and the
# existing _update_task_status helper when available. If SQLAlchemy engine is
# available via DATABASE_URL, it uses a short retry loop for DB updates; if not,
# it falls back to using the Flask app DB session via api.database.db.
#
# Usage:
#   persist_result(persistence_id=int, body_text=str, full_text=str, log_text=str, audit_id=<optional>)
#
# Returns the normalized result_meta dict committed.
def persist_result(
    persistence_id: int,
    body_text: Optional[str] = None,
    full_text: Optional[str] = None,
    log_text: Optional[str] = None,
    audit_id: Optional[str] = None,
    filename: Optional[str] = None,
    full_filename: Optional[str] = None,
    log_filename: Optional[str] = None,
    retry_attempts: int = 3,
    retry_backoff: float = 0.1,
) -> Dict[str, Any]:
    """
    Atomically write artifacts and update task_audit.result_meta for persistence_id.
    Returns the result_meta dict committed to DB.

    - Writes files to REPORT_PATH/{persistence_id}.* with atomic temp+replace.
    - Updates task_audit.result_meta JSON with keys: filename, full_filename, log_filename, persistence_id
    - Idempotent: re-runs will not clobber existing correct files/result_meta.
    """
    # canonicalize report_dir to instance_path + "reports" (fallback to REPORT_PATH or /tmp/phish_reports)
    instance_path = getattr(current_app, "instance_path", None)
    report_env = os.environ.get("REPORT_PATH")
    if instance_path:
        report_dir = os.path.join(instance_path, "reports")
    elif report_env:
        report_dir = report_env
    else:
        report_dir = "/tmp/phish_reports"
    os.makedirs(report_dir, exist_ok=True)

    if filename is None:
        filename = f"{persistence_id}.body.txt"
    if full_filename is None:
        full_filename = f"{persistence_id}.full.body.txt"
    if log_filename is None:
        log_filename = f"{persistence_id}.log"

    body_path = os.path.join(report_dir, filename)
    full_path = os.path.join(report_dir, full_filename)
    log_path = os.path.join(report_dir, log_filename)

    persist_logger.info("persist_result START: persistence_id=%s audit_id=%s filename=%s full_filename=%s log_filename=%s",
                        str(persistence_id), str(audit_id), filename, full_filename, log_filename)

    # Step 1: write files atomically (only those provided)
    try:
        if body_text is not None:
            persist_logger.debug("writing body_path=%s", body_path)
            durable_write_text(body_path, body_text, mode=0o600)
            persist_logger.debug("wrote body_path=%s", body_path)
        if full_text is not None:
            persist_logger.debug("writing full_path=%s", full_path)
            durable_write_text(full_path, full_text, mode=0o600)
            persist_logger.debug("wrote full_path=%s", full_path)
        if log_text is not None:
            persist_logger.debug("writing log_path=%s", log_path)
            durable_write_text(log_path, log_text, mode=0o600)
            persist_logger.debug("wrote log_path=%s", log_path)
    except Exception:
        persist_logger.exception("persist_result: artifact write failed for persistence_id=%s", persistence_id)
        raise

    # Build normalized result_meta
    result_meta = {
        "persistence_id": int(persistence_id) if isinstance(persistence_id, (int, str)) and str(persistence_id).isdigit() else persistence_id,
        "filename": filename,
        "full_filename": full_filename,
        "log_filename": log_filename,
    }

    persist_logger.debug("result_meta constructed: %s", json.dumps(result_meta))

    # --- START: attempt to locate TaskAudit when audit_id not provided ---
    # If caller didn't provide an audit_id, try to find a recent TaskAudit that
    # should be associated with this persistence_id and update it. This makes
    # synchronous flows (or callers that didn't pass audit_id) automatically
    # surface artifacts in the UI without manual DB edits.
    try:
        candidate_audit_id = None
        try:
            # Attempt to use Flask app DB session to find a candidate TaskAudit
            app = current_app._get_current_object() if hasattr(current_app, "_get_current_object") else current_app
            with app.app_context():
                from models.task_audit import TaskAudit  # type: ignore
                # Try direct match by persistence_id first
                try:
                    pid_val = int(result_meta.get("persistence_id")) if result_meta.get("persistence_id") is not None and str(result_meta.get("persistence_id")).isdigit() else None
                except Exception:
                    pid_val = None
                if pid_val is not None:
                    persist_logger.debug("querying TaskAudit by persistence_id=%s", pid_val)
                    q_candidates = db.session.query(TaskAudit).filter(TaskAudit.persistence_id == pid_val).order_by(TaskAudit.updated_at.desc()).limit(5).all()
                    if q_candidates:
                        candidate_audit_id = q_candidates[0].id
                        persist_logger.debug("found candidate TaskAudit by persistence_id: %s", candidate_audit_id)
                # Fallback: scan recent TaskAudit rows and inspect result_meta payloads
                if not candidate_audit_id:
                    persist_logger.debug("scanning recent TaskAudit rows for candidate match")
                    recent = db.session.query(TaskAudit).order_by(TaskAudit.updated_at.desc()).limit(50).all()
                    for cand in recent:
                        try:
                            rm = cand.result_meta
                            if isinstance(rm, dict) and rm.get("persistence_id") == result_meta.get("persistence_id"):
                                candidate_audit_id = cand.id
                                persist_logger.debug("found candidate via dict result_meta: %s", candidate_audit_id)
                                break
                            if isinstance(rm, str):
                                try:
                                    parsed = json.loads(rm)
                                    if isinstance(parsed, dict) and parsed.get("persistence_id") == result_meta.get("persistence_id"):
                                        candidate_audit_id = cand.id
                                        persist_logger.debug("found candidate via json-string result_meta: %s", candidate_audit_id)
                                        break
                                except Exception:
                                    pass
                        except Exception:
                            pass
        except Exception as e:
            persist_logger.exception("persist_result: error while locating candidate TaskAudit: %s", e)
            candidate_audit_id = None

        if candidate_audit_id:
            audit_id = candidate_audit_id
            persist_logger.debug("audit_id set from candidate: %s", audit_id)
    except Exception:
        # best-effort: never fail persist_result if this matching attempt errors
        persist_logger.exception("persist_result: candidate TaskAudit matching outer error")
        pass
    # --- END: attempt to locate TaskAudit when audit_id not provided ---

    # Best-effort: if we still have no audit_id, create a deterministic TaskAudit row so UI can link to artifacts
    if audit_id is None:
        try:
            app = current_app._get_current_object() if hasattr(current_app, "_get_current_object") else current_app
            with app.app_context():
                try:
                    from models.task_audit import TaskAudit  # type: ignore
                    # deterministic id so repeated runs are idempotent
                    candidate_id = f"persistence-{persistence_id}"
                    existing = db.session.query(TaskAudit).filter_by(id=candidate_id).first()
                    if not existing:
                        ta = TaskAudit(
                            id=candidate_id,
                            task_id=None,
                            task_type="classification",
                            user=None,
                            payload={},
                            status="succeeded",
                            persistence_id=int(persistence_id) if isinstance(persistence_id, (int, str)) and str(persistence_id).isdigit() else persistence_id,
                            result_meta=result_meta,
                        )
                        db.session.add(ta)
                        try:
                            db.session.commit()
                            audit_id = candidate_id
                            db_updated = True
                            updated = True
                            persist_logger.info("persist_result: created TaskAudit id=%s for persistence_id=%s", candidate_id, persistence_id)
                        except Exception:
                            try:
                                db.session.rollback()
                            except Exception:
                                pass
                            persist_logger.exception("persist_result: failed to commit created TaskAudit id=%s", candidate_id)
                    else:
                        # existing found — ensure result_meta merged
                        try:
                            rm = existing.result_meta or {}
                            if isinstance(rm, str):
                                try:
                                    rm = json.loads(rm)
                                except Exception:
                                    rm = {}
                            if isinstance(rm, dict):
                                rm.update(result_meta)
                                existing.result_meta = rm
                            existing.status = "succeeded"
                            existing.persistence_id = int(persistence_id) if isinstance(persistence_id, (int, str)) and str(persistence_id).isdigit() else persistence_id
                            db.session.add(existing)
                            db.session.commit()
                            audit_id = existing.id
                            db_updated = True
                            updated = True
                            persist_logger.info("persist_result: merged result_meta into existing TaskAudit id=%s", existing.id)
                        except Exception:
                            try:
                                db.session.rollback()
                            except Exception:
                                pass
                            persist_logger.exception("persist_result: failed to merge result_meta into existing TaskAudit id=%s", existing.id if existing else "<none>")
                except Exception:
                    persist_logger.debug("persist_result: TaskAudit creation skipped (import/db error)", exc_info=True)
        except Exception:
            persist_logger.exception("persist_result: outer error attempting TaskAudit auto-creation")

    # Step 2: update TaskAudit.result_meta using _update_task_status helper when available
    # Best-effort: try to import and call _update_task_status (it will handle sessions & emit sockets)
    updated = False
    try:
        from tasks import _update_task_status  # type: ignore

        try:
            if audit_id:
                persist_logger.debug("calling _update_task_status for audit_id=%s", audit_id)
                _update_task_status(audit_id, "succeeded", finished_at=datetime.utcnow(), result_meta=result_meta)
                updated = True
                persist_logger.info("_update_task_status succeeded for audit_id=%s", audit_id)
        except Exception:
            persist_logger.exception("persist_result: _update_task_status helper failed; will fallback to direct DB update")
            updated = False
    except Exception:
        persist_logger.debug("persist_result: _update_task_status not importable or unavailable", exc_info=True)
        updated = False

    # If helper unavailable or audit_id not provided, attempt to directly update TaskAudit
    if not updated and audit_id is not None:
        persist_logger.debug("attempting direct DB update for audit_id=%s", audit_id)
        # Try two approaches: SQLAlchemy engine if available via DATABASE_URL, else Flask DB session from api.database.db
        db_updated = False
        if DB_ENGINE_AVAILABLE:
            db_url = os.environ.get("DATABASE_URL") or os.environ.get("SQLALCHEMY_DATABASE_URI")
            if db_url:
                try:
                    engine = create_engine(db_url, future=True, pool_pre_ping=True)
                    last_exc = None
                    for attempt in range(1, retry_attempts + 1):
                        try:
                            with engine.begin() as conn:
                                # Loads existing result_meta if present, merges keys, and updates
                                sel = conn.execute(text("SELECT result_meta FROM task_audit WHERE id = :id"), {"id": audit_id})
                                row = sel.fetchone()
                                new_meta = dict(result_meta)
                                if row and row[0]:
                                    try:
                                        existing = json.loads(row[0]) if isinstance(row[0], str) else (row[0] if isinstance(row[0], dict) else {})
                                        if isinstance(existing, dict):
                                            existing.update(new_meta)
                                            new_meta = existing
                                    except Exception:
                                        pass
                                conn.execute(text("UPDATE task_audit SET result_meta = :rm, updated_at = CURRENT_TIMESTAMP, status = :status WHERE id = :id"), {"rm": json.dumps(new_meta), "status": "succeeded", "id": audit_id})
                            db_updated = True
                            persist_logger.info("SQLAlchemy engine update succeeded for audit_id=%s", audit_id)
                            break
                        except OperationalError as oe:
                            last_exc = oe
                            persist_logger.exception("persist_result: OperationalError on engine update attempt %s for audit_id=%s: %s", attempt, audit_id, oe)
                            time.sleep(retry_backoff * attempt)
                    if not db_updated and last_exc:
                        raise last_exc
                except Exception:
                    persist_logger.exception("persist_result: direct SQLAlchemy update failed for audit_id=%s", audit_id)
                    db_updated = False

        if not db_updated:
            # Fallback: try Flask DB session
            try:
                app = current_app._get_current_object() if hasattr(current_app, "_get_current_object") else current_app
                # ensure we have app context to use db
                with app.app_context():
                    try:
                        # Use TaskAudit model if present in project (import lazily)
                        from models.task_audit import TaskAudit  # type: ignore

                        persist_logger.debug("querying TaskAudit via Flask DB session id=%s", audit_id)
                        ta = db.session.query(TaskAudit).filter_by(id=audit_id).first()
                        if ta:
                            persist_logger.debug("found TaskAudit id=%s; merging result_meta", ta.id)
                            rm = result_meta
                            # merge with existing if possible
                            try:
                                existing = ta.result_meta if isinstance(ta.result_meta, dict) else (json.loads(ta.result_meta) if isinstance(ta.result_meta, str) else {})
                                if isinstance(existing, dict):
                                    existing.update(rm)
                                    rm = existing
                            except Exception:
                                persist_logger.exception("persist_result: error parsing existing result_meta for TaskAudit id=%s", ta.id)
                                pass
                            ta.result_meta = rm
                            try:
                                pid = rm.get("persistence_id")
                                if pid is not None and str(pid).isdigit():
                                    ta.persistence_id = int(pid)
                            except Exception:
                                persist_logger.exception("persist_result: failed to set ta.persistence_id for TaskAudit id=%s", ta.id)
                                pass
                            ta.status = "succeeded"
                            ta.updated_at = datetime.utcnow()
                            db.session.add(ta)
                            try:
                                db.session.commit()
                                db_updated = True
                                persist_logger.info("Flask DB session update committed for TaskAudit id=%s", ta.id)
                            except Exception:
                                persist_logger.exception("persist_result: commit failed for TaskAudit id=%s; attempting flush+commit", ta.id)
                                try:
                                    db.session.flush()
                                    db.session.commit()
                                    db_updated = True
                                    persist_logger.info("Flask DB session flush+commit succeeded for TaskAudit id=%s", ta.id)
                                except Exception:
                                    db.session.rollback()
                                    persist_logger.exception("persist_result: flush+commit failed for TaskAudit id=%s", ta.id)
                                    db_updated = False
                        else:
                            persist_logger.warning("persist_result: no TaskAudit found with id=%s", audit_id)
                    except Exception:
                        persist_logger.exception("persist_result: direct Flask DB update failed for audit_id=%s", audit_id)
                        db_updated = False
            except Exception:
                persist_logger.exception("persist_result: Flask DB fallback outer failed for audit_id=%s", audit_id)
                db_updated = False

        if db_updated:
            updated = True

    # If no audit_id provided or update not possible, emit a socket event with result_meta so UI can be notified
    if not updated and (audit_id is None or not db_updated):
        try:
            persist_logger.debug("persist_result: falling back to emit socket update for audit_id=%s", audit_id)
            # best-effort: emit update (tasks._emit_task_update will handle socket availability)
            try:
                from tasks import _emit_task_update  # type: ignore

                _emit_task_update(audit_id, {"status": "succeeded", "result_meta": result_meta, "persistence_id": result_meta.get("persistence_id")})
                persist_logger.debug("persist_result: _emit_task_update called for audit_id=%s", audit_id)
            except Exception:
                persist_logger.exception("persist_result: _emit_task_update not available or failed for audit_id=%s", audit_id)
                logger.debug("persist_result: _emit_task_update not available", exc_info=True)
        except Exception:
            persist_logger.exception("persist_result: exception in emit fallback for audit_id=%s", audit_id)
            pass

    try:
        try:
            persist_logger.info(
                "persist_result FINISH: persistence_id=%s audit_id=%s updated=%s db_updated=%s",
                str(persistence_id),
                str(audit_id),
                str(updated) if 'updated' in locals() else "False",
                str(db_updated) if 'db_updated' in locals() else "False",
            )
        except Exception:
            try:
                persist_logger.warning("persist_result FINISH (logging failed) persistence_id=%s audit_id=%s", str(persistence_id), str(audit_id))
            except Exception:
                # swallow any logging error to avoid masking persistence outcome
                pass
    except Exception:
        # ensure no unexpected exception escapes from final logging
        pass

    return result_meta

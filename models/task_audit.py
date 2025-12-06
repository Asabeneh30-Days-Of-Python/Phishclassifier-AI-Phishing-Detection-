# models/task_audit.py
from datetime import datetime
import uuid
from typing import Any, Dict, Optional, List

from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON

from api.database import db
import os
import logging

# Module-level logger configuration (logger file generation update)
LOG_DIR = os.environ.get("APP_LOG_DIR", "/app/logs")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    LOG_DIR = "/tmp"
TASK_AUDIT_LOG_PATH = os.path.join(LOG_DIR, "task_audit.log")

logger = logging.getLogger(__name__)
try:
    from logging.handlers import RotatingFileHandler

    _ta_handler = RotatingFileHandler(TASK_AUDIT_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3)
    _ta_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s"))
    # Avoid adding duplicate handlers if module is re-imported
    if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == getattr(_ta_handler, "baseFilename", None) for h in logger.handlers):
        logger.addHandler(_ta_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
except Exception:
    try:
        logger.debug("task_audit log file handler could not be created; using default logging")
    except Exception:
        pass


def _now():
    return datetime.utcnow()


class TaskAudit(db.Model):
    __tablename__ = "task_audit"

    # Use UUID string primary key to avoid collisions between workers and UI optimistic rows.
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = db.Column(db.String(128), index=True, nullable=True)
    # Relaxed constraint: application-level default ensures 'classification' when unset
    task_type = db.Column(db.String(50), nullable=True, default="classification")
    user = db.Column(db.String(256), nullable=True)

    # payload and result_meta are JSON blobs stored in SQLite JSON column
    payload = db.Column(SQLITE_JSON, nullable=True)
    result_meta = db.Column(SQLITE_JSON, nullable=True)

    # NEW: explicit persistence_id column to map task_audit rows to persisted report files
    # This mirrors what the UI and reports code expect for mapping artifacts to cards.
    persistence_id = db.Column(db.Integer, index=True, nullable=True)

    status = db.Column(db.String(32), nullable=False, default="pending")
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_now, onupdate=_now, index=True)

    logs = db.relationship(
        "TaskLog",
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def to_summary(self) -> Dict[str, Any]:
        """
        Summary view returned to the UI. Includes persistence_id so frontend can resolve files.
        """
        return {
            "id": self.id,
            "task_id": self.task_id,
            "task_type": self.task_type or "classification",
            "user": self.user,
            "status": self.status,
            "persistence_id": self.persistence_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "result_meta": self.result_meta,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def to_detail(self, last_n_logs: int = 200) -> Dict[str, Any]:
        """
        Detail view includes payload and recent logs (reversed chronological).
        """
        q = self.logs.order_by(TaskLog.ts.desc()).limit(last_n_logs).all()
        logs = [l.to_dict() for l in reversed(q)]
        return {
            **self.to_summary(),
            "payload": self.payload,
            "last_logs": logs,
        }

    def add_log(self, level: str, message: str, meta: Optional[Dict[str, Any]] = None) -> Optional["TaskLog"]:
        """
        Convenience helper to append a TaskLog row and commit it (best-effort).
        Returns the created TaskLog or None on failure.
        """
        try:
            tl = TaskLog(task_audit_id=self.id, level=level, message=message, meta=meta)
            db.session.add(tl)
            try:
                db.session.commit()
            except Exception:
                try:
                    db.session.flush()
                except Exception:
                    pass
            try:
                logger.info("TaskAudit.add_log id=%s level=%s message=%s", self.id, level, message)
            except Exception:
                pass
            return tl
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            try:
                logger.exception("TaskAudit.add_log failed for id=%s", self.id)
            except Exception:
                pass
            return None

    def update_status(
        self,
        status: str,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
        result_meta: Optional[Dict[str, Any]] = None,
        persistence_id: Optional[int] = None,
    ) -> bool:
        """
        Convenience helper to update TaskAudit status fields and commit (best-effort).
        Ensures a sensible default task_type before commit so DB rows always have a usable type at app level.
        Optionally accepts persistence_id to ensure the mapping is recorded atomically with status updates.
        Returns True on success, False otherwise.
        """
        try:
            changed = False
            if status is not None and self.status != status:
                self.status = status
                changed = True
            if started_at is not None:
                self.started_at = started_at
                changed = True
            if finished_at is not None:
                self.finished_at = finished_at
                changed = True
            if result_meta is not None:
                self.result_meta = result_meta
                changed = True
            if persistence_id is not None and self.persistence_id != persistence_id:
                self.persistence_id = persistence_id
                changed = True
            if changed:
                # ensure a sensible default task_type is present before commit
                if not self.task_type:
                    self.task_type = "classification"
                db.session.add(self)
                try:
                    db.session.commit()
                except Exception:
                    try:
                        db.session.flush()
                    except Exception:
                        pass
            try:
                logger.info("TaskAudit.update_status id=%s status=%s persistence_id=%s", self.id, status, persistence_id)
            except Exception:
                pass
            return True
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            try:
                logger.exception("TaskAudit.update_status failed for id=%s", self.id)
            except Exception:
                pass
            return False


class TaskLog(db.Model):
    __tablename__ = "task_log"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    task_audit_id = db.Column(
        db.String(36),
        db.ForeignKey("task_audit.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ts = db.Column(db.DateTime, nullable=False, default=_now, index=True)
    level = db.Column(db.String(16), nullable=False, default="INFO")
    message = db.Column(db.Text, nullable=False)
    meta = db.Column(SQLITE_JSON, nullable=True)

    task = db.relationship("TaskAudit", back_populates="logs")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_audit_id": self.task_audit_id,
            "ts": self.ts.isoformat(),
            "level": self.level,
            "message": self.message,
            "meta": self.meta,
        }

    def __repr__(self) -> str:
        return f"<TaskLog id={self.id} audit_id={self.task_audit_id} level={self.level} ts={self.ts.isoformat()}>"

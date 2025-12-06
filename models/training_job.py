# models/training_job.py

from datetime import datetime
from typing import Optional, Sequence, Dict, Any

from api.database import db


class TrainingJob(db.Model):
    __tablename__ = "training_job"

    id         = db.Column(db.String,   primary_key=True)
    task_id    = db.Column(db.String(128), nullable=True, index=True)
    status     = db.Column(db.String,   nullable=False, default="pending")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
    started_at = db.Column(db.DateTime, nullable=True, index=True)
    finished_at= db.Column(db.DateTime, nullable=True, index=True)
    datasets   = db.Column(db.JSON,     nullable=False, default=list)

    def __init__(
        self,
        id: str,
        datasets: Optional[Sequence[str]] = None,
        status: str = "pending",
        created_at: Optional[datetime] = None,
        task_id: Optional[str] = None
    ):
        """
        Explicit initializer so we can pass `datasets` as a kwarg
        and optionally override created_at for tests.
        """
        super().__init__()

        self.id         = id
        self.task_id    = task_id
        self.status     = status
        self.created_at = created_at or datetime.utcnow()
        # Ensure datasets is always a list
        self.datasets   = list(datasets) if datasets is not None else []

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize the TrainingJob for JSON responses or logging.
        """
        return {
            "id": self.id,
            "task_id": self.task_id,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if getattr(self, "updated_at", None) else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "datasets": list(self.datasets) if self.datasets is not None else [],
        }

    def update_status(self, status: str, started_at: Optional[datetime] = None, finished_at: Optional[datetime] = None, commit: bool = True) -> bool:
        """
        Convenience helper to update status and timing fields. Best-effort commit when requested.
        Returns True on success, False on failure.
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
            if changed:
                db.session.add(self)
                if commit:
                    try:
                        db.session.commit()
                    except Exception:
                        try:
                            db.session.flush()
                        except Exception:
                            pass
            return True
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            return False

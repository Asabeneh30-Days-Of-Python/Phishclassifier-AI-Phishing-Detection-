# models/quarantined_email.py
from api.database import db
from api.feedback_utils import normalize_feedback
from datetime import datetime
import json
from typing import Any, Dict, Optional

class QuarantinedEmail(db.Model):
    __tablename__ = "quarantined_emails"

    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(512))
    subject = db.Column(db.String(1024))
    body = db.Column(db.Text)
    received_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    risk_score = db.Column(db.Float, nullable=True, index=True)
    status = db.Column(db.String(50), nullable=True)

    # Underlying DB column keeps the name "feedback"
    _feedback = db.Column("feedback", db.String(50), nullable=False, default="none", index=True)

    # Added fields required by worker and API persistence
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
    prediction = db.Column(db.String(100), nullable=True, index=True)
    technical_explanation = db.Column(db.Text, nullable=True)
    attacker_insights = db.Column(db.Text, nullable=True)
    link_analysis = db.Column(db.Text, nullable=True)
    model_version = db.Column(db.String(64), nullable=True)
    attachments_meta = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f"<QuarantinedEmail id={self.id} sender={self.sender!r}>"

    @property
    def feedback(self) -> str:
        try:
            return self._feedback or "none"
        except Exception:
            return "none"

    @feedback.setter
    def feedback(self, value) -> None:
        self._feedback = normalize_feedback(value)

    def get_attachments_meta(self) -> Optional[Any]:
        """
        Parse attachments_meta JSON stored as text and return Python object, or None on failure.
        """
        try:
            if not self.attachments_meta:
                return None
            return json.loads(self.attachments_meta)
        except Exception:
            return None

    def to_dict(self) -> Dict[str, Any]:
        """
        Return a serialization suitable for JSON responses and logging.
        """
        return {
            "id": self.id,
            "sender": self.sender or "",
            "subject": self.subject or "",
            "body": self.body or "",
            "received_at": self.received_at.isoformat() if getattr(self, "received_at", None) else None,
            "risk_score": self.risk_score,
            "status": self.status,
            "feedback": self.feedback,
            "created_at": self.created_at.isoformat() if getattr(self, "created_at", None) else None,
            "updated_at": self.updated_at.isoformat() if getattr(self, "updated_at", None) else None,
            "prediction": self.prediction,
            "technical_explanation": self.technical_explanation,
            "attacker_insights": self.attacker_insights,
            "link_analysis": self.link_analysis,
            "model_version": self.model_version,
            "attachments_meta": self.get_attachments_meta(),
        }


class FeedbackAudit(db.Model):
    __tablename__ = "feedback_audit"

    id = db.Column(db.Integer, primary_key=True)
    quarantine_id = db.Column(db.Integer, db.ForeignKey("quarantined_emails.id"), nullable=False)
    user_id = db.Column(db.Integer, nullable=True)
    old_value = db.Column(db.String(50), nullable=True)
    new_value = db.Column(db.String(50), nullable=False)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<FeedbackAudit qid={self.quarantine_id} {self.old_value}->{self.new_value}>"

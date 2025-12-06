# models/prediction.py
from datetime import datetime
from api.database import db

class Prediction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(255))
    body = db.Column(db.Text)
    prediction_result = db.Column(db.String(50))
    risk_score = db.Column(db.Float, index=True)
    model_version = db.Column(db.String(64), nullable=True, index=True)
    received_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp(), index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "subject": self.subject or "",
            "body": self.body or "",
            "prediction_result": self.prediction_result,
            "risk_score": float(self.risk_score) if self.risk_score is not None else None,
            "model_version": self.model_version,
            "received_at": self.received_at.isoformat() if getattr(self, "received_at", None) else None,
            "created_at": self.created_at.isoformat() if getattr(self, "created_at", None) else None,
        }

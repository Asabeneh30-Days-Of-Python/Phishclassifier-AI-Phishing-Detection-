from datetime import datetime
from api.database import db

class FeedbackMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    feedback_id = db.Column(db.Integer, db.ForeignKey('feedback.id'), nullable=False)
    sender = db.Column(db.String(20))  # For example, 'user' or 'admin'
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

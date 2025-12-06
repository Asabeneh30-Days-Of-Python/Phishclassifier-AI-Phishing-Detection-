# models/feedback.py

from api.database import db
from datetime import datetime
from models.user import User

class Feedback(db.Model):
    __tablename__ = 'feedback'

    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(
                        db.Integer,
                        db.ForeignKey('user.id'),
                        nullable=False
                    )
    content        = db.Column(db.Text, nullable=False)
    predicted_label= db.Column(db.String(50), nullable=True)
    correct_label  = db.Column(db.String(50), nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    admin_response = db.Column(db.Text, nullable=True)

    # link back to the authoring User
    user           = db.relationship(
                        'User',
                        backref=db.backref('feedback_items', lazy='dynamic'),
                        lazy='joined'
                    )

    # if you still need threaded messages
    messages       = db.relationship(
                        'FeedbackMessage',
                        backref='feedback',
                        lazy=True
                    )

    def __init__(self, user_id: int, content: str) -> None:
        self.user_id = user_id
        self.content = content
        self.created_at = datetime.utcnow()

    def __repr__(self) -> str:
        return f"<Feedback {self.id} by User {self.user_id}>"

# promote_admin.py
import os
from flask import Flask
from api.database import db           # <-- single shared db instance
from models.user  import User

# 1. Spin up a minimal Flask app
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL',
    'sqlite:///phishclassifier.db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 2. Register the shared db instance with this app
db.init_app(app)

# 3. Perform your one-off promotion inside the app context
with app.app_context():
    username = "Charles25"
    user = User.query.filter_by(username=username).first()
    if not user:
        print(f"User '{username}' not found.")
    else:
        user.is_admin = True
        db.session.commit()
        print(f"{username} is now admin: {user.is_admin}")

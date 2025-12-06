# models/create_db.py

import os
import sys

# ensure 'api' package is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from api.app      import create_app
from api.database import db, init_db
import models.models  # import all your Flask-SQLAlchemy models

# 1) Bootstrap your Flask app
app = create_app()

# 2) Initialize the already-registered db extension
db.init_app(app)

# 3) Inside the app context create ALL tables
with app.app_context():
    init_db()
    print("Database tables created successfully!")

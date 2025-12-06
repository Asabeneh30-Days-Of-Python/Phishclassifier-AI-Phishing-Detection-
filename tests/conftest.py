import sys
import os

# Add the project root to sys.path so that the 'api' package can be found.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from api.app import create_app
from api.database import db  # Adjust if necessary to match your project structure

@pytest.fixture(scope='session')
def app():
    """
    Create and configure a new app instance for tests.
    This fixture creates the application once per test session.
    """
    app = create_app()
    app.config['TESTING'] = True
    # Configure an in-memory SQLite database for testing.
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    
    with app.app_context():
        db.create_all()  # Create all tables.
        yield app
        db.drop_all()  # Clean up when tests are done.

@pytest.fixture(scope='session')
def client(app):
    """
    Returns a test client for the app.
    """
    return app.test_client()

@pytest.fixture
def db_session(app):
    """
    Provides a database session for tests.
    After each test, the session is rolled back to avoid side-effects.
    """
    session = db.session
    yield session
    session.rollback()
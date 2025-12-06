# tests/test_endpoints.py
import sys
import os
import json
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from api.app import create_app

@pytest.fixture
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_home_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'text/html' in response.content_type

def test_classify_requires_content(client):
    response = client.post("/api/classify", json={})
    assert response.status_code == 400
    data = response.get_json()
    assert data and "error" in data

def test_enqueue_classify_endpoint(client):
    payload = {"content": "This is a test email that mentions phish content."}
    response = client.post("/api/enqueue_classify", data=json.dumps(payload), content_type="application/json")
    assert response.status_code in (200, 202, 503)

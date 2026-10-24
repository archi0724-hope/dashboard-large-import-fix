from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


def test_public_liveness_does_not_open_the_organizer_store():
    with patch('app.main.st', side_effect=AssertionError('liveness opened storage')):
        response = TestClient(app).get('/healthz')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}

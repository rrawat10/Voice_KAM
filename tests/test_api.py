from fastapi.testclient import TestClient
from backend.main import app, rate_limits, store
from backend.data import excel_template


def test_data_preview_upload_and_replacement():
    rate_limits.clear()
    with TestClient(app) as client:
        assert client.get('/api/session').status_code == 401
        response = client.post('/api/sessions/mock')
        assert response.status_code == 200
        assert 'HttpOnly' in response.headers['set-cookie']
        assert response.json()['dataset']['metrics'][0]['current'] == 135000
        assert response.json()['summary'] == ''
        assert 'periods' not in response.json()
        assert 'pending_question' not in response.json()
        assert client.put('/api/session/periods', json={'value': 'comparable'}).status_code == 404
        old_id = client.cookies.get('kam_session')
        assert client.post('/api/sessions/upload', files={'file': ('data.xlsx', excel_template())}).status_code == 200
        assert client.get('/api/session').json()['dataset']['source'] == 'uploaded spreadsheet'
        assert old_id not in store.memory
        cleared = client.delete('/api/session')
        assert cleared.status_code == 200
        assert client.get('/api/session').status_code == 401


def test_invalid_upload_does_not_replace_existing_data():
    rate_limits.clear()
    with TestClient(app) as client:
        client.post('/api/sessions/mock')
        response = client.post('/api/sessions/upload', files={'file': ('bad.xlsx', b'broken')})
        assert response.status_code == 422
        assert client.get('/api/session').json()['dataset']['source'] == 'assignment mock data'


def test_cross_origin_mutation_rejected():
    with TestClient(app) as client:
        assert client.post('/api/sessions/mock', headers={'Origin': 'https://unrelated.example'}).status_code == 403


def test_another_browser_does_not_inherit_session():
    rate_limits.clear()
    with TestClient(app) as first, TestClient(app) as second:
        first.post('/api/sessions/mock')
        assert second.get('/api/session').status_code == 401


def test_voice_missing_key_is_clear(monkeypatch):
    from backend.main import settings
    monkeypatch.setattr(settings, 'gemini_api_key', '')
    rate_limits.clear()
    with TestClient(app) as client:
        client.post('/api/sessions/mock')
        with client.websocket_connect('/api/voice') as ws:
            message = ws.receive_json()
            assert message['type'] == 'error'
            assert 'GEMINI_API_KEY' in message['message']


def test_returning_browser_is_notified_of_expiry():
    rate_limits.clear()
    with TestClient(app) as client:
        client.post('/api/sessions/mock')
        sid = client.cookies.get('kam_session')
        store.memory[sid]['expires_at'] = 0
        response = client.get('/api/session')
        assert response.status_code == 410
        assert 'expired' in response.json()['detail']

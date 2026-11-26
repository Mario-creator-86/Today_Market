import pytest
from fastapi.testclient import TestClient

from backend.config import get_settings
from backend.database import utcnow
from backend.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/api.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    monkeypatch.setenv('ADMIN_TOKEN', 'test-admin-token')
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()

@pytest.mark.parametrize('path', ['/health', '/health/ready', '/api/dashboard', '/api/assets', '/api/assets/BTC',
    '/api/events', '/api/calendar', '/api/fx', '/api/sources', '/api/settings', '/api/models',
    '/api/providers', '/api/performance', '/api/brief', '/api/alerts'])
def test_core_reads(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text

def test_admin_writes_require_token_and_import_stays_unverified(client):
    body = {'source_id':'fed', 'title':'Test official release', 'content':'User-supplied content',
            'url':'https://federalreserve.gov/test', 'published_at':utcnow().isoformat()}
    assert client.post('/api/admin/documents', json=body).status_code == 401
    response = client.post('/api/admin/documents', json=body, headers={'X-Admin-Token':'test-admin-token'})
    assert response.status_code == 200, response.text
    event = response.json()
    assert event['verification_status'] == 'UNCONFIRMED'
    assert client.get('/api/events/'+event['id']).status_code == 200
    assert all(a['decision']['direction']=='WAIT' for a in client.get('/api/assets').json())

def test_missing_model_cannot_be_assigned(client):
    response = client.put('/api/admin/roles/auditor', json={'primary':'invented'},
                          headers={'X-Admin-Token':'test-admin-token'})
    assert response.status_code == 422

def test_websocket_initial_snapshot(client):
    with client.websocket_connect('/ws') as ws:
        message = ws.receive_json()
        assert message['type'] == 'snapshot'
        assert message['data']['assets'][0]['symbol'] == 'BTC'

def test_no_secret_in_public_settings(client):
    assert 'test-admin-token' not in client.get('/api/settings').text

def test_ollama_probe_is_authenticated_and_fails_without_model(client):
    assert client.post('/api/admin/ollama/test').status_code==401
    response=client.post('/api/admin/ollama/test', headers={'X-Admin-Token':'test-admin-token'})
    assert response.status_code==200
    assert response.json()['success'] is False

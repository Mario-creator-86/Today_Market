import pytest
from fastapi.testclient import TestClient

from backend.config import get_settings
from backend.main import create_app

LOCAL = {'X-Local-Admin': '1'}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/local.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    monkeypatch.setenv('ADMIN_AUTH', 'local')
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def client(app, host='127.0.0.1'):
    return TestClient(app, base_url=f'http://{host}:8000', client=('127.0.0.1', 50123))


def test_local_mode_needs_no_password_on_this_computer(app):
    with client(app) as c:
        assert c.get('/api/settings').json()['admin_mode'] == 'local'
        assert c.get('/api/admin/provider-settings', headers=LOCAL).status_code == 200
        assert c.post('/api/admin/recalculate', headers=LOCAL).status_code == 200


@pytest.mark.parametrize('headers, reason', [
    ({}, 'no custom header: a cross-site simple request cannot add one without a CORS preflight'),
    (dict(LOCAL, Origin='https://evil.example'), 'foreign Origin'),
    (dict(LOCAL, Host='evil.example:8000'), 'DNS rebinding keeps the attacker host name'),
])
def test_local_mode_refuses_requests_that_are_not_the_owner_on_this_computer(app, headers, reason):
    with client(app) as c:
        assert c.get('/api/admin/provider-settings', headers=headers).status_code == 403, reason


def test_local_mode_refuses_other_machines(app):
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('192.168.1.20', 50123)) as c:
        assert c.get('/api/admin/provider-settings', headers=LOCAL).status_code == 403


def test_the_browser_may_send_the_local_header_from_the_ui_origin_only(app):
    with client(app) as c:
        ok = c.options('/api/admin/provider-settings', headers={
            'Origin': 'http://127.0.0.1:3000', 'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'x-local-admin'})
        assert ok.status_code == 200 and 'x-local-admin' in ok.headers['access-control-allow-headers'].lower()
        evil = c.options('/api/admin/provider-settings', headers={
            'Origin': 'https://evil.example', 'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'x-local-admin'})
        assert 'access-control-allow-origin' not in evil.headers


def test_token_mode_ignores_the_local_header(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/token.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    monkeypatch.setenv('ADMIN_AUTH', 'token')
    monkeypatch.setenv('ADMIN_TOKEN', 'server-token-123')
    get_settings.cache_clear()
    with client(create_app()) as c:
        assert c.get('/api/admin/provider-settings', headers=LOCAL).status_code == 401
        assert c.get('/api/admin/provider-settings', headers={'X-Admin-Token': 'server-token-123'}).status_code == 200
        assert c.get('/api/admin/provider-settings', headers={'X-Admin-Token': 'é-non-ascii'.encode('latin-1')}).status_code == 401
    get_settings.cache_clear()

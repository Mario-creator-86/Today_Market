from fastapi.testclient import TestClient


def test_health_and_no_fabricated_data(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/test.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.main import create_app
    with TestClient(create_app()) as client:
        assert client.get('/health').json()['status'] == 'ok'
        data = client.get('/api/dashboard').json()
        assert data['assets']
        assert all(a['quote'] is None for a in data['assets'])
        assert client.post('/api/admin/recalculate').status_code == 503


def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/ops.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.main import create_app
    return TestClient(create_app())


def test_request_ids_metrics_and_websocket_fanout(tmp_path, monkeypatch):
    with app_client(tmp_path, monkeypatch) as client:
        echoed = client.get('/health', headers={'X-Request-ID': 'trace-12345678'})
        assert echoed.headers['x-request-id'] == 'trace-12345678'
        assert client.get('/health', headers={'X-Request-ID': 'bad id!'}).headers['x-request-id'] != 'bad id!'
        text = client.get('/metrics')
        assert text.status_code == 200 and text.headers['content-type'].startswith('text/plain')
        assert 'hub_provider_up{provider="binance"' in text.text
        data = client.get('/metrics/json').json()
        assert {p['name'] for p in data['providers']} >= {'binance', 'massive'} and data['ai_models'] == []
        assert client.get('/health/ready').json()['event_bus'] == 'memory'
        with client.websocket_connect('/ws') as socket:
            message = socket.receive_json()
            assert message['type'] == 'snapshot' and message['data']['assets']


def test_env_example_lists_every_settings_field():
    from pathlib import Path

    from backend.config import Settings
    from backend.notifications import NotificationSettings
    from backend.telemetry import TelemetrySettings
    from backend.workers import WorkerSettings
    listed = {line.split('=', 1)[0] for line in (Path(__file__).parent.parent / '.env.example').read_text().splitlines()
              if '=' in line and not line.startswith('#')}
    fields = {name.upper() for cls in (Settings, NotificationSettings, TelemetrySettings, WorkerSettings)
              for name in cls.model_fields}
    assert fields - listed == set()


def test_timestamps_are_stored_as_utc_even_on_sqlite(tmp_path):
    from datetime import datetime, timedelta, timezone

    from backend.database import Document, Store, Tick
    store = Store(f'sqlite:///{tmp_path}/utc.db')
    store.initialize()
    bucharest = timezone(timedelta(hours=3))
    published = datetime(2026, 9, 26, 22, 35, 59, tzinfo=bucharest)
    with store.session() as session:
        session.add(Document(id='d', fingerprint='f', source_id='fed', published_at=published, title='t', url='u', content='c'))
        session.add(Tick(symbol='BTC', price=1.0, timestamp=published, ingested_at=published, provider='test', payload={}))
        session.commit()
    with store.session() as session:
        stored = session.get(Document, 'd').published_at
        tick = session.query(Tick).one()
    assert stored == published and stored.utcoffset() == timedelta(0) and stored.hour == 19
    assert tick.timestamp == published and tick.timestamp.tzinfo is not None


def test_alembic_revisions_upgrade_and_downgrade(tmp_path, monkeypatch):
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    from backend.config import ROOT, get_settings
    url = f'sqlite:///{tmp_path}/migrated.db'
    monkeypatch.setenv('DATABASE_URL', url)
    get_settings.cache_clear()
    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'migrations'))

    def tables() -> set[str]:
        engine = sa.create_engine(url)
        try:
            return set(sa.inspect(engine).get_table_names())
        finally:
            engine.dispose()
    command.upgrade(config, 'head')
    assert {'notification_deliveries', 'push_subscriptions', 'shadow_decisions', 'ohlc_bars', 'recalc_triggers'} <= tables()
    command.downgrade(config, '0001')
    assert 'ohlc_bars' not in tables() and 'events' in tables()
    get_settings.cache_clear()

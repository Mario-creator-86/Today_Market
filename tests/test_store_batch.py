import asyncio

import pytest

from backend.config import Settings
from backend.database import Store
from backend.runtime import Runtime
from backend.service import HubService


@pytest.fixture
def store(tmp_path):
    result = Store(f'sqlite:///{tmp_path}/hub.db')
    result.initialize()
    return result


def test_put_many_inserts_and_updates_in_one_transaction(store):
    store.put('derivative_metrics', 'A', {'v': 1})
    commits = []
    from sqlalchemy import event
    event.listen(store.engine, 'commit', lambda conn: commits.append(1))
    store.put_many('derivative_metrics', {'A': {'v': 2}, 'B': {'v': 3}})
    assert len(commits) == 1
    assert store.get('derivative_metrics', 'A') == {'v': 2} and store.get('derivative_metrics', 'B') == {'v': 3}
    store.put_many('derivative_metrics', {})
    assert len(commits) == 1


@pytest.mark.asyncio
async def test_derivatives_batch_is_written_with_one_commit_per_cycle(store):
    hub = HubService(store, Settings(enable_ingestion=False))
    rows = [{'instrument_name': f'BTC-27DEC26-{k}-C', 'open_interest': 1.0} for k in range(500)]

    async def metrics(currency, kind='option'):
        await asyncio.sleep(0)
        return rows if currency == 'BTC' and kind == 'option' else []

    hub.providers['deribit'].metrics = metrics
    commits = []
    from sqlalchemy import event
    event.listen(store.engine, 'commit', lambda conn: commits.append(1))
    await Runtime(hub).derivatives()
    stored = store.list('derivative_metrics', 1000)
    assert len(stored) == 500 and {r['currency'] for r in stored} == {'BTC'}
    # One batch per currency with data plus provider-health persistence; never one commit per instrument.
    assert len(commits) <= 3


def test_sqlite_enforces_foreign_keys(tmp_path):
    from sqlalchemy import text
    store = Store(f'sqlite:///{tmp_path}/fk.db')
    store.initialize()
    with store.engine.connect() as connection:
        assert connection.execute(text('PRAGMA foreign_keys')).scalar() == 1

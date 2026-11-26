from datetime import timedelta

import pytest

from backend.config import Settings
from backend.database import Store, utcnow
from backend.schemas import DocumentInput, Quote
from backend.service import HubService


@pytest.fixture
def hub(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/hub.db')
    store.initialize()
    result = HubService(store, Settings(enable_ingestion=False))
    result.initialize()
    return result

def quote(**changes):
    data = dict(symbol='BTC', price=100, timestamp=utcnow(), provider='binance', bid=99.9, ask=100.1)
    data.update(changes)
    return Quote(**data)

def test_quote_order_and_future_validation(hub):
    hub.ingest_quote(quote())
    assert not hub.ingest_quote(quote(timestamp=utcnow()-timedelta(minutes=5), price=50))
    with pytest.raises(ValueError):
        hub.ingest_quote(quote(timestamp=utcnow()+timedelta(hours=1)))
    assert hub.assets()[0]['quote']['price'] == 100

def test_official_event_persists_deduplicates_and_exposes_evidence(hub):
    doc = DocumentInput(source_id='fed', title='Federal Reserve statement about policy', content='Test evidence',
                        url='https://www.federalreserve.gov/newsevents/test.htm', published_at=utcnow())
    first = hub.ingest_document(doc, trusted_transport=True)
    second = hub.ingest_document(doc, trusted_transport=True)
    assert first['id'] == second['id']
    assert first['verification_status'] == 'CONFIRMED'
    assert len(hub.events()) == 1
    assert len(hub.event(first['id'])['evidence']) == 1

def test_spoofed_official_source_rejected(hub):
    with pytest.raises(ValueError, match='domain'):
        hub.ingest_document(DocumentInput(source_id='fed', title='Fake central bank news', content='',
                            url='https://attacker.example/news', published_at=utcnow()))

@pytest.mark.asyncio
async def test_missing_ai_and_evidence_force_wait(hub):
    hub.ingest_quote(quote())
    decisions = await hub.recalculate()
    btc = next(d for d in decisions if d['symbol']=='BTC')
    assert btc['direction'] == 'WAIT' and btc['category'].startswith('WAIT')
    # Nothing measured: a neutral WAIT whose missing inputs are explicit, never an actionable or audited setup.
    assert btc['audit_status'] != 'PASS' and {'event', 'macro'} <= set(btc['components_missing'])
    assert btc['historical_probability']['status'] == 'NOT ENOUGH DATA'
    stale = next(d for d in decisions if d['symbol'] == 'NASDAQ')
    assert stale['category'] == 'WAIT / DATA INSUFFICIENT' and stale['reasons']
    assert hub.dashboard()['last_analysis']

def test_stale_quote_status_recomputed_at_read(hub):
    hub.ingest_quote(quote(timestamp=utcnow()-timedelta(seconds=60)))
    assert hub.assets()[0]['quote']['freshness_status'] == 'STALE'


def test_a_republished_close_fills_missing_provenance_without_changing_values(hub):
    from backend.database import Tick
    stamp = utcnow().replace(microsecond=0) - timedelta(days=1)
    bare = Quote(symbol='US2Y', price=4.81, timestamp=stamp, provider='treasury', daily_close=True)
    assert hub.ingest_quote(bare)
    richer = bare.model_copy(update={'previous_close': 4.87, 'change_bp': -6.0, 'change_basis': 'previous_close',
                                     'data_class': 'DAILY_CLOSE'})
    assert not hub.ingest_quote(richer)  # not a new observation
    assert not hub.ingest_quote(richer.model_copy(update={'price': 4.9}))  # a different value never overwrites
    with hub.store.session() as session:
        rows = session.query(Tick).filter(Tick.symbol == 'US2Y').all()
    assert len(rows) == 1 and rows[0].price == 4.81
    assert rows[0].payload['previous_close'] == 4.87 and rows[0].payload['change_bp'] == -6.0

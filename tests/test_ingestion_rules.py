"""Ingestion rules: stub ticks never hide real observations (newest wins within each kind), and every new event says
where its topic came from (topic_basis). Settings never read the real .env; nothing touches the network."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.config import Settings
from backend.database import Store, Tick, utcnow
from backend.schemas import DocumentInput, Quote
from backend.service import HubService


@pytest.fixture
def hub(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/ingestion.db')
    store.initialize()
    result = HubService(store, Settings(_env_file=None, enable_ingestion=False))
    result.initialize()
    return result


def stored(hub, symbol):
    with hub.store.session() as session:
        return [(t.provider, t.price) for t in session.scalars(select(Tick).where(Tick.symbol == symbol)
                                                               .order_by(Tick.timestamp))]


def close(price, days_ago, **changes):
    data = dict(symbol='SP500', price=price, timestamp=utcnow().replace(microsecond=0) - timedelta(days=days_ago),
                provider='fred', daily_close=True, data_class='DAILY_CLOSE', attribution='Source: S&P DJI via FRED')
    return Quote(**dict(data, **changes))


def stub(price, seconds_ago=0):
    return Quote(symbol='SP500', price=price, timestamp=utcnow() - timedelta(seconds=seconds_ago), provider='massive-stub',
                 stub=True)


def test_a_stub_tick_stamped_now_never_hides_a_real_close(hub):
    assert hub.ingest_quote(stub(5000.0, seconds_ago=5))  # PROVIDER_MODE=stub: development data every cycle
    assert hub.ingest_quote(close(7743.41, days_ago=1))  # the real FRED close is older, yet it is stored
    assert not hub.ingest_quote(close(7700.0, days_ago=2))  # still newest wins among real ticks
    assert not hub.ingest_quote(close(7743.41, days_ago=1))  # the same observation again: not a new tick
    assert hub.ingest_quote(stub(5001.0))  # stubs keep their own sequence and never edit a real tick
    assert not hub.ingest_quote(stub(4999.0, seconds_ago=60))
    assert stored(hub, 'SP500') == [('fred', 7743.41), ('massive-stub', 5000.0), ('massive-stub', 5001.0)]
    assert hub.ingest_quote(close(7801.0, days_ago=0.5))  # a later real close still lands
    assert ('fred', 7801.0) in stored(hub, 'SP500')


def doc(source_id, title, url, content=''):
    return DocumentInput(source_id=source_id, title=title, content=content, url=url, published_at=utcnow())


@pytest.mark.parametrize('source_id, title, url, topic, basis', [
    # a causal keyword rule names the topic
    ('treasury', 'Tanker attacked in Strait of Hormuz near shipping lane', 'https://home.treasury.gov/news/1',
     'IRAN_HORMUZ', 'rule'),
    # the source rule of the causal configuration (fed -> FED_POLICY) is a configured rule too
    ('fed', 'Federal Reserve Board announces a staff appointment', 'https://www.federalreserve.gov/news/2.htm',
     'FED_POLICY', 'rule'),
    # no rule: two extracted entities joined, a heuristic the UI does not use as a title
    ('treasury', 'Boeing and Airbus sign aircraft deal in Paris', 'https://home.treasury.gov/news/3',
     'AIRBUS_BOEING', 'entities'),
    # nothing to name the topic with
    ('treasury', 'shares were little changed on quiet trading', 'https://home.treasury.gov/news/4', None, None)])
def test_new_events_say_where_their_topic_came_from(hub, source_id, title, url, topic, basis):
    event = hub.ingest_document(doc(source_id, title, url), trusted_transport=True)
    assert event['topic'] == topic and event['topic_basis'] == basis
    assert hub.event(event['id'])['topic_basis'] == basis and hub.events()[0]['topic_basis'] == basis
